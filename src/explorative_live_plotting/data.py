"""Lazy Polars data-source catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
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
from .expressions import parse_filter_expression

FORMATS = {"auto", "csv", "parquet", "ndjson", "ipc"}
MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
PATH_VARIABLE = re.compile(r"\$\{([A-Za-z_]\w*)\}")
SCHEMA_HEAD_ROWS = 100

_SIMPLE_POLARS_DTYPES = {
    name.casefold(): getattr(pl, name)
    for name in (
        "Binary",
        "Boolean",
        "Categorical",
        "Date",
        "Datetime",
        "Duration",
        "Float32",
        "Float64",
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Int128",
        "Null",
        "String",
        "Time",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
    )
    if hasattr(pl, name)
}
_SIMPLE_POLARS_DTYPES.update(
    {
        "bool": pl.Boolean,
        "str": pl.String,
        "utf8": pl.String,
    }
)


def _reader_dtype(value: Any, option: str, column: str) -> pl.DataType:
    """Convert a JSON-safe reader dtype description into a Polars dtype."""
    if isinstance(value, str):
        name = value.strip().removeprefix("pl.")
        dtype = _SIMPLE_POLARS_DTYPES.get(name.casefold())
        if dtype is not None:
            return dtype
        supported = ", ".join(sorted({item.__name__ for item in _SIMPLE_POLARS_DTYPES.values()}))
        raise ConfigurationError(
            f"{option}.{column} has unknown Polars type {value!r}; supported types: {supported}"
        )
    if isinstance(value, dict):
        raw_name = value.get("type")
        if not isinstance(raw_name, str):
            raise ConfigurationError(f"{option}.{column}.type must be a Polars type name")
        name = raw_name.removeprefix("pl.").casefold()
        if name == "datetime":
            allowed = {"type", "time_unit", "time_zone"}
            unexpected = sorted(set(value) - allowed)
            if unexpected:
                raise ConfigurationError(
                    f"{option}.{column} has unsupported Datetime fields: {', '.join(unexpected)}"
                )
            time_unit = value.get("time_unit", "us")
            time_zone = value.get("time_zone")
            if time_unit not in {"ns", "us", "ms"}:
                raise ConfigurationError(
                    f"{option}.{column}.time_unit must be one of ns, us, or ms"
                )
            if time_zone is not None and not isinstance(time_zone, str):
                raise ConfigurationError(f"{option}.{column}.time_zone must be a string or null")
            return pl.Datetime(time_unit=time_unit, time_zone=time_zone)
        if name == "duration":
            allowed = {"type", "time_unit"}
            unexpected = sorted(set(value) - allowed)
            if unexpected:
                raise ConfigurationError(
                    f"{option}.{column} has unsupported Duration fields: {', '.join(unexpected)}"
                )
            time_unit = value.get("time_unit", "us")
            if time_unit not in {"ns", "us", "ms"}:
                raise ConfigurationError(
                    f"{option}.{column}.time_unit must be one of ns, us, or ms"
                )
            return pl.Duration(time_unit=time_unit)
        if set(value) == {"type"}:
            return _reader_dtype(raw_name, option, column)
        raise ConfigurationError(
            f"{option}.{column} only supports parameter objects for Datetime and Duration"
        )
    raise ConfigurationError(
        f"{option}.{column} must be a Polars type name or a type options object"
    )


def normalize_reader_options(options: dict[str, Any]) -> dict[str, Any]:
    """Resolve JSON-safe schema declarations before passing options to Polars."""
    normalized = dict(options)
    for option in ("schema", "schema_overrides", "hive_schema"):
        if option not in normalized or normalized[option] is None:
            continue
        declaration = normalized[option]
        if not isinstance(declaration, dict):
            hint = f'{{"{option}": {{"timestamp": "Datetime"}}}}'
            raise ConfigurationError(
                f"{option} must be a JSON object mapping column names to types; use {hint}"
            )
        normalized[option] = {
            str(column): _reader_dtype(dtype, option, str(column))
            for column, dtype in declaration.items()
        }
    return normalized


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: str
    format: str = "auto"
    options: dict[str, Any] | None = None
    filter_expression: str | None = None


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
        options = normalize_reader_options(dict(spec.options or {}))
        path = self._resolve_path(spec.path, module, module_name)
        data_format = self._format(spec, path)
        separator = options.get("separator")
        if separator is not None:
            if not isinstance(separator, str) or len(separator.encode()) != 1:
                raise ConfigurationError("CSV separator must be exactly one single-byte character")
            if data_format != "csv":
                raise ConfigurationError("separator can only be used with CSV sources")
        if data_format == "csv":
            lazy = pl.scan_csv(path, **options)
        elif data_format == "parquet":
            lazy = pl.scan_parquet(path, **options)
        elif data_format == "ndjson":
            lazy = pl.scan_ndjson(path, **options)
        elif data_format == "ipc":
            lazy = pl.scan_ipc(path, **options)
        else:
            raise AssertionError(data_format)
        expression = str(spec.filter_expression or "").strip()
        if expression:
            schema = lazy.collect_schema()
            lazy = lazy.filter(
                parse_filter_expression(expression, schema, module, module_name)
            )
        return lazy

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

    def column_excerpt(
        self, name: str, column: str, intermediate: int = 3
    ) -> dict[str, Any]:
        """Return min/max and a few ordered interior values without collecting the column."""
        schema = self.schema(name)
        if column not in schema:
            raise ConfigurationError(f"column does not exist in {name}: {column}")
        if not 0 <= intermediate <= 18:
            raise ConfigurationError("intermediate value count must be between 0 and 18")
        dtype = schema[column]
        desired = intermediate + 2
        projected = self.lazy(name).select(column).drop_nulls()
        if dtype.is_numeric() or dtype == pl.Date or isinstance(dtype, pl.Datetime):
            expressions = [pl.col(column).min().alias("_value_0")]
            expressions.extend(
                pl.col(column)
                .quantile(index / (intermediate + 1), interpolation="nearest")
                .alias(f"_value_{index}")
                for index in range(1, intermediate + 1)
            )
            expressions.append(pl.col(column).max().alias(f"_value_{desired - 1}"))
            row = projected.select(expressions).collect(engine="streaming").row(0)
            values = [self._excerpt_scalar(value, dtype) for value in row if value is not None]
        else:
            count = projected.select(pl.col(column).n_unique().alias("_count")).collect(
                engine="streaming"
            )[0, "_count"]
            if not count:
                values = []
            else:
                positions = sorted(
                    {round(index * (count - 1) / (desired - 1)) for index in range(desired)}
                )
                frame = (
                    projected.unique()
                    .sort(column)
                    .with_row_index("_position")
                    .filter(pl.col("_position").is_in(positions))
                    .select(column)
                    .collect(engine="streaming")
                )
                values = [self._excerpt_scalar(value, dtype) for value in frame[column]]
        # Quantiles can coincide in low-cardinality columns. Preserve order while
        # avoiding repeated suggestions in the browser.
        unique_values = []
        for value in values:
            if value not in unique_values:
                unique_values.append(value)
        return {"source": name, "column": column, "dtype": str(dtype), "values": unique_values}

    def column_values(
        self, name: str, column: str, offset: int = 0, limit: int = 250
    ) -> dict[str, Any]:
        """Return one ordered page of distinct values from a projected column."""
        schema = self.schema(name)
        if column not in schema:
            raise ConfigurationError(f"column does not exist in {name}: {column}")
        dtype = schema[column]
        if dtype != pl.Date and not isinstance(dtype, pl.Datetime):
            raise ConfigurationError("the time-value picker requires a Date or Datetime column")
        if offset < 0:
            raise ConfigurationError("column-value offset must be zero or greater")
        if not 1 <= limit <= 1000:
            raise ConfigurationError("column-value page size must be between 1 and 1000")
        frame = (
            self.lazy(name)
            .select(column)
            .drop_nulls()
            .unique()
            .sort(column)
            .slice(offset, limit + 1)
            .collect(engine="streaming")
        )
        has_more = frame.height > limit
        values = [self._excerpt_scalar(value, dtype) for value in frame[column].head(limit)]
        return {
            "source": name,
            "column": column,
            "dtype": str(dtype),
            "values": values,
            "offset": offset,
            "next_offset": offset + len(values) if has_more else None,
            "has_more": has_more,
        }

    @staticmethod
    def _excerpt_scalar(value: Any, dtype: pl.DataType) -> Any:
        if dtype == pl.Date:
            if isinstance(value, datetime):
                value = value.date()
            return value.isoformat() if isinstance(value, date) else str(value)
        if isinstance(dtype, pl.Datetime):
            return value.isoformat() if isinstance(value, datetime) else str(value)
        if dtype.is_integer():
            return int(value)
        if dtype.is_float() or dtype == pl.Decimal:
            return float(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

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

    def expression_variables(self) -> list[str]:
        """Return public Polars expressions exported by the active module or its modules."""
        with self._lock:
            module = self._config_module
        if module is None:
            return []
        expressions = []
        for name, value in sorted(vars(module).items()):
            if name.startswith("_"):
                continue
            if isinstance(value, pl.Expr):
                expressions.append(name)
            elif isinstance(value, ModuleType):
                expressions.extend(
                    f"{name}.{child_name}"
                    for child_name, child in sorted(vars(value).items())
                    if not child_name.startswith("_") and isinstance(child, pl.Expr)
                )
        return expressions

    def filter_expression(self, expression: str, schema: pl.Schema) -> pl.Expr:
        """Resolve an inline expression against the current config module."""
        with self._lock:
            module = self._config_module
            module_name = self._config_module_name
        return parse_filter_expression(expression, schema, module, module_name)

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
        if spec.filter_expression is not None and not isinstance(spec.filter_expression, str):
            raise ConfigurationError("source filter expression must be a string or null")

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
