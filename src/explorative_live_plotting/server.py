"""Flask server for the local plotting workbench."""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
from threading import Lock
import time
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from .cache import QueryCache
from .codegen import generate_script
from .data import DataCatalog, SourceSpec
from .errors import ConfigurationError
from .logging import log
from .plotting import (
    default_config,
    migrate_legacy_config,
    preview,
    render_artifacts,
    validate_config,
)
from .query import QueryEngine
from .registry import Registry


class ApplicationState:
    def __init__(
        self,
        catalog: DataCatalog,
        registry: Registry,
        cache: QueryCache,
        output_dir: Path,
    ) -> None:
        self.catalog = catalog
        self.registry = registry
        self.cache = cache
        self.engine = QueryEngine(catalog, registry, cache)
        self.output_dir = output_dir
        self.render_lock = Lock()
        self.system_metrics = SystemMetrics()


class SystemMetrics:
    """Read lightweight whole-system metrics without an optional runtime dependency."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._previous_cpu: tuple[int, int] | None = self._cpu_times()

    @staticmethod
    def _cpu_times() -> tuple[int, int] | None:
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
        except (OSError, ValueError, IndexError):
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        non_idle = sum(values[:3]) + sum(values[5:8])
        return idle + non_idle, idle

    @staticmethod
    def _memory() -> tuple[int, int] | None:
        try:
            entries = {
                key.rstrip(":"): int(value) * 1024
                for key, value, *_ in (
                    line.split() for line in Path("/proc/meminfo").read_text().splitlines()
                )
            }
            total = entries["MemTotal"]
            available = entries["MemAvailable"]
            return total - available, total
        except (OSError, ValueError, KeyError):
            return None

    def sample(self) -> dict[str, Any]:
        with self._lock:
            current = self._cpu_times()
            cpu_percent = None
            if current is not None:
                if self._previous_cpu is None:
                    total_delta, idle_delta = current
                else:
                    total_delta = current[0] - self._previous_cpu[0]
                    idle_delta = current[1] - self._previous_cpu[1]
                if total_delta > 0:
                    cpu_percent = 100.0 * (total_delta - idle_delta) / total_delta
                self._previous_cpu = current
        memory = self._memory()
        used, total = memory if memory is not None else (None, None)
        ram_percent = 100.0 * used / total if used is not None and total else None
        try:
            load_average = os.getloadavg()[0]
        except (AttributeError, OSError):
            load_average = None
        return {
            "cpu_percent": cpu_percent,
            "load_average_1m": load_average,
            "ram_used_bytes": used,
            "ram_total_bytes": total,
            "ram_percent": ram_percent,
            "sampled_at": time.time(),
        }


def _download(artifacts: dict[str, bytes], basename: str) -> Response:
    if len(artifacts) == 1:
        filename, content = next(iter(artifacts.items()))
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=filename,
            mimetype={".png": "image/png", ".pdf": "application/pdf", ".json": "application/json"}[
                Path(filename).suffix
            ],
        )
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for filename, content in artifacts.items():
            output.writestr(filename, content)
    archive.seek(0)
    return send_file(
        archive,
        as_attachment=True,
        download_name=f"{basename}.zip",
        mimetype="application/zip",
    )


def _save(output_dir: Path, artifacts: dict[str, bytes]) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, content in artifacts.items():
        destination = output_dir / filename
        temporary = output_dir / f".{filename}.{os.getpid()}.tmp"
        try:
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        written.append(str(destination))
    return written


def _source_spec(raw: Any) -> SourceSpec:
    if not isinstance(raw, dict):
        raise ConfigurationError("source must be an object")
    options = raw.get("options") or {}
    if not isinstance(options, dict):
        raise ConfigurationError("source options must be an object")
    options = dict(options)
    separator = raw.get("separator")
    if separator is not None and str(separator) != "":
        separator = str(separator)
        if separator == r"\t":
            separator = "\t"
        options["separator"] = separator
    return SourceSpec(
        name=str(raw.get("name", "")).strip(),
        path=str(raw.get("path", "")).strip(),
        format=str(raw.get("format", "auto")),
        options=options,
    )


def create_app(state: ApplicationState) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["ELP_STATE"] = state

    @app.errorhandler(ConfigurationError)
    def configuration_error(error: ConfigurationError):
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(Exception)
    def unexpected_error(error: Exception):
        if isinstance(error, HTTPException):
            return error
        log(f"ERROR: {error}")
        return jsonify({"error": str(error)}), 500

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/bootstrap")
    def bootstrap():
        return jsonify(
            {
                "sources": state.catalog.all_metadata(),
                "registry": state.registry.metadata(),
                "config_module": state.catalog.config_module,
                "default_config": default_config(state.catalog.config_module),
                "output_dir": str(state.output_dir),
            }
        )

    @app.get("/api/path-suggestions")
    def path_suggestions():
        value = request.args.get("value", "")
        if len(value) > 4096:
            raise ConfigurationError("source path is too long")
        return jsonify(state.catalog.complete_path(value))

    @app.get("/api/system-stats")
    def system_stats():
        return jsonify(state.system_metrics.sample())

    @app.post("/api/migrate-config")
    def migrate_config():
        config, warnings = migrate_legacy_config(request.get_json(silent=False))
        return jsonify({"config": config, "warnings": warnings})

    @app.post("/api/config-module")
    def set_config_module():
        raw = request.get_json(silent=False)
        if not isinstance(raw, dict):
            raise ConfigurationError("config module request must be an object")
        module = str(raw.get("module") or "").strip() or None
        raw_sources = raw.get("sources")
        with state.render_lock:
            if raw_sources is not None:
                if not isinstance(raw_sources, list):
                    raise ConfigurationError("sources must be an array")
                sources = state.catalog.replace_sources(
                    [_source_spec(item) for item in raw_sources], module
                )
            else:
                sources = state.catalog.set_config_module(module)
        log(f"Config module set to: {module or '(none)'}")
        return jsonify({"config_module": module, "sources": sources})

    @app.post("/api/sources")
    def add_source():
        metadata = state.catalog.add(_source_spec(request.get_json(silent=False)))
        log(f"Registered lazy source {metadata['name']}: {metadata['path']}")
        return jsonify(metadata), 201

    @app.delete("/api/sources/<name>")
    def remove_source(name: str):
        state.catalog.remove(name)
        return jsonify({"removed": name})

    @app.get("/api/sources/<name>/preview")
    def source_preview(name: str):
        rows = max(1, min(100, int(request.args.get("rows", "10"))))
        frame = state.catalog.lazy(name).head(rows).collect(engine="streaming")
        return jsonify({"columns": frame.columns, "rows": frame.to_dicts()})

    def request_config() -> dict[str, Any]:
        config = validate_config(request.get_json(silent=False), state.registry)
        if config["config_module"] != state.catalog.config_module:
            raise ConfigurationError(
                "configuration module has not been applied; apply it before rendering or exporting"
            )
        return config

    @app.post("/api/validate")
    def validate():
        config = request_config()
        for layer in config["layers"]:
            if layer.get("enabled", True):
                state.engine.execute(layer)
        return jsonify(config)

    def render(formats: list[str] | None = None):
        config = request_config()
        with state.render_lock:
            return config, render_artifacts(config, state.engine, state.registry, formats)

    @app.post("/api/render")
    def render_preview():
        config, (artifacts, cache_states) = render(["png"])
        return jsonify({"previews": preview(artifacts), "cache": cache_states, "config": config})

    @app.post("/api/download")
    def download():
        config, (artifacts, _) = render()
        return _download(artifacts, config["filename"])

    @app.post("/api/export-code")
    def export_code():
        config = request_config()
        with state.render_lock:
            script = generate_script(config, state.catalog, state.registry)
        return send_file(
            BytesIO(script.encode()),
            as_attachment=True,
            download_name=f"{config['filename']}.py",
            mimetype="text/x-python",
        )

    @app.post("/api/save")
    def save():
        config, (artifacts, cache_states) = render()
        files = _save(state.output_dir, artifacts)
        log(f"Saved {len(files)} artifact(s) under {state.output_dir}")
        return jsonify(
            {
                "files": files,
                "previews": preview(artifacts),
                "cache": cache_states,
                "config": config,
            }
        )

    @app.post("/api/cache/clear")
    def clear_cache():
        state.cache.clear()
        log("Cleared query cache")
        return jsonify({"cleared": True})

    return app
