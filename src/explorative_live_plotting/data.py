"""Lazy Polars data-source catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import glob
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
from threading import RLock
from types import ModuleType
from typing import Any

import polars as pl

from .errors import ConfigurationError

FORMATS = {"auto", "csv", "parquet", "ndjson", "ipc"}
MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
PATH_VARIABLE = re.compile(r"\$\{([A-Za-z_]\w*)\}")
SCHEMA_HEAD_ROWS = 100


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: str
    format: str = "auto"
    options: dict[str, Any] | None = None


class DataCatalog:
    """Thread-safe catalog that stores source specifications, never eager frames."""

    def __init__(self, config_module: str | None = None) -> None:
        self._sources: dict[str, SourceSpec] = {}
        self._schemas: dict[str, pl.Schema] = {}
        self._lock = RLock()
        self._config_module_name: str | None = None
        self._config_module: ModuleType | None = None
        if config_module:
            self.set_config_module(config_module)

    @property
    def config_module(self) -> str | None:
        with self._lock:
            return self._config_module_name

    def set_config_module(self, name: str | None) -> list[dict[str, Any]]:
        """Change the path-variable module after validating every existing source."""
        normalized = str(name or "").strip() or None
        module = self._import_config_module(normalized)
        with self._lock:
            specs = list(self._sources.values())
            schemas: dict[str, pl.Schema] = {}
            for spec in specs:
                self._validate_resolved_path(self._resolve_path(spec.path, module, normalized))
                schemas[spec.name] = self._infer_schema(spec, module, normalized)
            self._config_module_name = normalized
            self._config_module = module
            self._schemas = schemas
        return self.all_metadata()

    def replace_sources(
        self,
        specs: list[SourceSpec],
        config_module: str | None,
    ) -> list[dict[str, Any]]:
        """Atomically replace the catalog and module when loading a configuration."""
        normalized = str(config_module or "").strip() or None
        module = self._import_config_module(normalized)
        candidates: dict[str, SourceSpec] = {}
        schemas: dict[str, pl.Schema] = {}
        for spec in specs:
            self._validate_spec(spec)
            resolved = self._resolve_path(spec.path, module, normalized)
            self._validate_resolved_path(resolved)
            schemas[spec.name] = self._infer_schema(spec, module, normalized)
            candidates[spec.name] = spec
        with self._lock:
            self._sources = candidates
            self._schemas = schemas
            self._config_module_name = normalized
            self._config_module = module
        return self.all_metadata()

    def add(self, spec: SourceSpec) -> dict[str, Any]:
        self._validate_spec(spec)
        self._validate_path(spec.path)
        with self._lock:
            module = self._config_module
            module_name = self._config_module_name
        schema = self._infer_schema(spec, module, module_name)
        with self._lock:
            self._sources[spec.name] = spec
            self._schemas[spec.name] = schema
        return self.metadata(spec.name)

    def remove(self, name: str) -> None:
        with self._lock:
            if name not in self._sources:
                raise ConfigurationError(f"unknown source: {name}")
            del self._sources[name]
            self._schemas.pop(name, None)

    def get(self, name: str) -> SourceSpec:
        with self._lock:
            try:
                return self._sources[name]
            except KeyError as error:
                raise ConfigurationError(f"unknown source: {name}") from error

    def specs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(spec) for spec in self._sources.values()]

    def lazy(self, source: str | SourceSpec) -> pl.LazyFrame:
        spec = self.get(source) if isinstance(source, str) else source
        with self._lock:
            module = self._config_module
            module_name = self._config_module_name
        return self._lazy(spec, module, module_name)

    def _lazy(
        self,
        spec: SourceSpec,
        module: ModuleType | None,
        module_name: str | None,
    ) -> pl.LazyFrame:
        options = dict(spec.options or {})
        path = self._resolve_path(spec.path, module, module_name)
        data_format = self._format(spec, path)
        separator = options.get("separator")
        if separator is not None:
            if not isinstance(separator, str) or len(separator.encode()) != 1:
                raise ConfigurationError("CSV separator must be exactly one single-byte character")
            if data_format != "csv":
                raise ConfigurationError("separator can only be used with CSV sources")
        if data_format == "csv":
            return pl.scan_csv(path, **options)
        if data_format == "parquet":
            return pl.scan_parquet(path, **options)
        if data_format == "ndjson":
            return pl.scan_ndjson(path, **options)
        if data_format == "ipc":
            return pl.scan_ipc(path, **options)
        raise AssertionError(data_format)

    def metadata(self, name: str) -> dict[str, Any]:
        spec = self.get(name)
        schema = self.schema(name)
        resolved_path = self.resolve_path(spec.path)
        return {
            **asdict(spec),
            "format": self._format(spec, resolved_path),
            "resolved_path": resolved_path,
            "columns": [{"name": key, "dtype": str(value)} for key, value in schema.items()],
            "fingerprint": self.fingerprint(name),
        }

    def schema(self, name: str) -> pl.Schema:
        spec = self.get(name)
        with self._lock:
            schema = self._schemas.get(name)
        if schema is None:
            with self._lock:
                module = self._config_module
                module_name = self._config_module_name
            schema = self._infer_schema(spec, module, module_name)
            with self._lock:
                if self._sources.get(name) == spec:
                    self._schemas[name] = schema
        return schema

    def all_metadata(self) -> list[dict[str, Any]]:
        return [self.metadata(item["name"]) for item in self.specs()]

    def fingerprint(self, name: str) -> str:
        spec = self.get(name)
        resolved_path = self.resolve_path(spec.path)
        files = self._matched_paths(resolved_path)
        payload = {
            "spec": asdict(spec),
            "config_module": self.config_module,
            "resolved_path": resolved_path,
            "files": [
                {
                    "path": str(path),
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                }
                for path in files
            ],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _infer_schema(
        self,
        spec: SourceSpec,
        module: ModuleType | None,
        module_name: str | None,
    ) -> pl.Schema:
        head = self._lazy(spec, module, module_name).head(SCHEMA_HEAD_ROWS)
        return head.collect(engine="streaming").schema

    def resolve_path(self, value: str) -> str:
        with self._lock:
            module = self._config_module
            module_name = self._config_module_name
        return self._resolve_path(value, module, module_name)

    def path_variables(self) -> list[dict[str, str]]:
        """Return public string/path values exposed by the active config module."""
        with self._lock:
            module = self._config_module
        if module is None:
            return []
        return [
            {
                "name": name,
                "placeholder": f"${{{name}}}",
                "value": os.fspath(value),
            }
            for name, value in sorted(vars(module).items())
            if not name.startswith("_") and isinstance(value, (str, os.PathLike))
        ]

    def complete_path(self, value: str, limit: int = 50) -> dict[str, Any]:
        """Resolve a path template and suggest matching variables and filesystem entries."""
        raw = str(value)
        variables = self.path_variables()
        variable_start = raw.rfind("${")
        bare_dollar = raw.endswith("$") and variable_start < 0
        if bare_dollar or (variable_start >= 0 and "}" not in raw[variable_start:]):
            if bare_dollar:
                variable_start = len(raw) - 1
                prefix = ""
            else:
                prefix = raw[variable_start + 2 :]
            suggestions = [
                {
                    "value": (
                        raw[:variable_start]
                        + item["placeholder"]
                        + ("/" if Path(item["value"]).expanduser().is_dir() else "")
                    ),
                    "label": f"{item['placeholder']} → {item['value']}",
                    "kind": "variable",
                }
                for item in variables
                if item["name"].startswith(prefix)
            ]
            return {
                "resolved_path": None,
                "suggestions": suggestions[:limit],
                "variables": variables,
            }

        try:
            resolved = self.resolve_path(raw)
        except ConfigurationError as error:
            return {
                "resolved_path": None,
                "suggestions": [],
                "variables": variables,
                "error": str(error),
            }

        suggestions: list[dict[str, str]] = []
        if raw and not glob.has_magic(resolved):
            path = Path(resolved)
            if path.is_file():
                pass
            elif path.is_dir() and not raw.endswith(("/", os.sep)):
                suggestions.append(
                    {
                        "value": f"{raw}/",
                        "label": f"{path.name}/",
                        "kind": "directory",
                    }
                )
            else:
                raw_separator = raw.rfind("/")
                raw_directory = raw[: raw_separator + 1] if raw_separator >= 0 else ""
                parent = path if raw.endswith(("/", os.sep)) else path.parent
                prefix = "" if raw.endswith(("/", os.sep)) else path.name
                try:
                    matches = sorted(
                        (item for item in parent.iterdir() if item.name.startswith(prefix)),
                        key=lambda item: (not item.is_dir(), item.name.casefold()),
                    )
                except OSError:
                    matches = []
                for item in matches[:limit]:
                    suffix = "/" if item.is_dir() else ""
                    suggestions.append(
                        {
                            "value": f"{raw_directory}{item.name}{suffix}",
                            "label": f"{item.name}{suffix}",
                            "kind": "directory" if item.is_dir() else "file",
                        }
                    )
        return {
            "resolved_path": resolved,
            "suggestions": suggestions,
            "variables": variables,
        }

    @staticmethod
    def _import_config_module(name: str | None) -> ModuleType | None:
        if name is None:
            return None
        if MODULE_NAME.fullmatch(name) is None:
            raise ConfigurationError(f"invalid config module name: {name}")
        try:
            importlib.invalidate_caches()
            working_directory = str(Path.cwd())
            if working_directory not in sys.path:
                sys.path.insert(0, working_directory)
            return importlib.import_module(name)
        except Exception as error:
            raise ConfigurationError(f"cannot import config module {name}: {error}") from error

    @staticmethod
    def _resolve_path(value: str, module: ModuleType | None, module_name: str | None) -> str:
        names = PATH_VARIABLE.findall(value)
        if names and module is None:
            raise ConfigurationError(
                f"source path uses ${{{names[0]}}} but no config module is configured"
            )

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if not hasattr(module, name):
                raise ConfigurationError(
                    f"config module {module_name} does not define {name}"
                )
            resolved = getattr(module, name)
            if not isinstance(resolved, (str, os.PathLike)):
                raise ConfigurationError(
                    f"config value {module_name}.{name} must be a string or path"
                )
            return os.fspath(resolved)

        return str(Path(PATH_VARIABLE.sub(replace, value)).expanduser().absolute())

    @staticmethod
    def _matched_paths(value: str) -> list[Path]:
        path = Path(value).expanduser()
        if path.is_dir():
            matches = sorted(item for item in path.iterdir() if item.is_file())
        elif glob.has_magic(str(path)):
            matches = sorted(Path(item) for item in glob.glob(str(path)))
        else:
            matches = [path]
        return matches

    def _validate_path(self, value: str) -> None:
        resolved = self.resolve_path(value)
        self._validate_resolved_path(resolved)

    @classmethod
    def _validate_resolved_path(cls, resolved: str) -> None:
        matches = cls._matched_paths(resolved)
        if not matches or any(not item.is_file() for item in matches):
            raise ConfigurationError(f"source path or glob has no readable files: {resolved}")

    @staticmethod
    def _validate_spec(spec: SourceSpec) -> None:
        if not spec.name or not spec.name.replace("_", "").replace("-", "").isalnum():
            raise ConfigurationError("source name must contain letters, numbers, _ or -")
        if spec.format not in FORMATS:
            raise ConfigurationError(f"unsupported source format: {spec.format}")

    def _format(self, spec: SourceSpec, resolved_path: str | None = None) -> str:
        if spec.format != "auto":
            return spec.format
        normalized = (resolved_path or self.resolve_path(spec.path)).replace("*", "sample").lower()
        if normalized.endswith(".csv.gz"):
            return "csv"
        suffix = Path(normalized).suffix
        formats = {
            ".csv": "csv",
            ".csv.gz": "csv",
            ".parquet": "parquet",
            ".pq": "parquet",
            ".ndjson": "ndjson",
            ".jsonl": "ndjson",
            ".ipc": "ipc",
            ".feather": "ipc",
            ".arrow": "ipc",
        }
        if suffix not in formats:
            raise ConfigurationError(
                f"cannot infer format from {spec.path}; select csv, parquet, ndjson, or ipc"
            )
        return formats[suffix]
