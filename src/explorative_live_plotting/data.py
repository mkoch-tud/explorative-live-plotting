"""Lazy Polars data-source catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import glob
import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any

import polars as pl

from .errors import ConfigurationError

FORMATS = {"auto", "csv", "parquet", "ndjson", "ipc"}


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: str
    format: str = "auto"
    options: dict[str, Any] | None = None


class DataCatalog:
    """Thread-safe catalog that stores source specifications, never eager frames."""

    def __init__(self) -> None:
        self._sources: dict[str, SourceSpec] = {}
        self._lock = RLock()

    def add(self, spec: SourceSpec) -> dict[str, Any]:
        if not spec.name or not spec.name.replace("_", "").replace("-", "").isalnum():
            raise ConfigurationError("source name must contain letters, numbers, _ or -")
        if spec.format not in FORMATS:
            raise ConfigurationError(f"unsupported source format: {spec.format}")
        self._validate_path(spec.path)
        self.lazy(spec)
        with self._lock:
            self._sources[spec.name] = spec
        return self.metadata(spec.name)

    def remove(self, name: str) -> None:
        with self._lock:
            if name not in self._sources:
                raise ConfigurationError(f"unknown source: {name}")
            del self._sources[name]

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
        data_format = self._format(spec)
        options = dict(spec.options or {})
        if data_format == "csv":
            return pl.scan_csv(spec.path, **options)
        if data_format == "parquet":
            return pl.scan_parquet(spec.path, **options)
        if data_format == "ndjson":
            return pl.scan_ndjson(spec.path, **options)
        if data_format == "ipc":
            return pl.scan_ipc(spec.path, **options)
        raise AssertionError(data_format)

    def metadata(self, name: str) -> dict[str, Any]:
        spec = self.get(name)
        schema = self.lazy(spec).collect_schema()
        return {
            **asdict(spec),
            "format": self._format(spec),
            "columns": [{"name": key, "dtype": str(value)} for key, value in schema.items()],
            "fingerprint": self.fingerprint(name),
        }

    def all_metadata(self) -> list[dict[str, Any]]:
        return [self.metadata(item["name"]) for item in self.specs()]

    def fingerprint(self, name: str) -> str:
        spec = self.get(name)
        files = self._matched_paths(spec.path)
        payload = {
            "spec": asdict(spec),
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
        matches = self._matched_paths(value)
        if not matches or any(not item.is_file() for item in matches):
            raise ConfigurationError(f"source path or glob has no readable files: {value}")

    @staticmethod
    def _format(spec: SourceSpec) -> str:
        if spec.format != "auto":
            return spec.format
        normalized = spec.path.replace("*", "sample").lower()
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
