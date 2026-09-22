"""Flask server for the local plotting workbench."""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import re
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

WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class WorkspaceState:
    """Isolated data/query state belonging to one browser workspace tab."""

    def __init__(self, catalog: DataCatalog, registry: Registry, cache: QueryCache) -> None:
        self.catalog = catalog
        self.engine = QueryEngine(catalog, registry, cache)


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
        self._workspace_lock = Lock()
        self._workspaces: dict[str, WorkspaceState] = {}

    def workspace(self, identifier: str | None) -> WorkspaceState:
        normalized = str(identifier or "default").strip()
        if WORKSPACE_ID.fullmatch(normalized) is None:
            raise ConfigurationError(
                "workspace id must contain 1-64 letters, numbers, underscores, or hyphens"
            )
        with self._workspace_lock:
            workspace = self._workspaces.get(normalized)
            if workspace is None:
                workspace = WorkspaceState(self.catalog.clone(), self.registry, self.cache)
                self._workspaces[normalized] = workspace
            return workspace

    def remove_workspace(self, identifier: str) -> bool:
        if WORKSPACE_ID.fullmatch(identifier) is None:
            raise ConfigurationError("invalid workspace id")
        with self._workspace_lock:
            return self._workspaces.pop(identifier, None) is not None

    def clone_workspace(self, source_identifier: str | None, target_identifier: str) -> None:
        source_name = str(source_identifier or "default").strip()
        if WORKSPACE_ID.fullmatch(source_name) is None:
            raise ConfigurationError("invalid source workspace id")
        if WORKSPACE_ID.fullmatch(target_identifier) is None:
            raise ConfigurationError("invalid target workspace id")
        source = self.workspace(source_name)
        with self._workspace_lock:
            if target_identifier in self._workspaces:
                raise ConfigurationError("target workspace already exists")
            self._workspaces[target_identifier] = WorkspaceState(
                source.catalog.clone(), self.registry, self.cache
            )


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
    filter_expression = raw.get("filter_expression")
    if filter_expression is not None and not isinstance(filter_expression, str):
        raise ConfigurationError("source filter expression must be a string or null")
    return SourceSpec(
        name=str(raw.get("name", "")).strip(),
        path=str(raw.get("path", "")).strip(),
        format=str(raw.get("format", "auto")),
        options=options,
        filter_expression=str(filter_expression or "").strip() or None,
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

    def current_workspace() -> WorkspaceState:
        return state.workspace(request.headers.get("X-ELP-Workspace"))

    @app.get("/api/bootstrap")
    def bootstrap():
        workspace = current_workspace()
        initial_config = default_config(workspace.catalog.config_module)
        initial_config["output_dir"] = str(state.output_dir)
        return jsonify(
            {
                "sources": workspace.catalog.all_metadata(),
                "registry": state.registry.metadata(),
                "config_module": workspace.catalog.config_module,
                "expression_variables": workspace.catalog.expression_variables(),
                "default_config": initial_config,
                "output_dir": str(state.output_dir),
            }
        )

    @app.delete("/api/workspaces/<identifier>")
    def delete_workspace(identifier: str):
        return jsonify({"removed": state.remove_workspace(identifier)})

    @app.post("/api/workspaces/<identifier>/clone")
    def clone_workspace(identifier: str):
        state.clone_workspace(request.headers.get("X-ELP-Workspace"), identifier)
        return jsonify({"workspace": identifier}), 201

    @app.get("/api/path-suggestions")
    def path_suggestions():
        value = request.args.get("value", "")
        if len(value) > 4096:
            raise ConfigurationError("path is too long")
        return jsonify(current_workspace().catalog.complete_path(value))

    def resolved_path(value: str, workspace: WorkspaceState) -> Path:
        if len(value) > 4096:
            raise ConfigurationError("path is too long")
        return Path(workspace.catalog.resolve_path(value)).expanduser().resolve()

    @app.get("/api/config-browser")
    def config_browser():
        workspace = current_workspace()
        raw_directory = str(request.args.get("directory") or state.output_dir)
        directory = resolved_path(raw_directory, workspace)
        if directory.exists() and not directory.is_dir():
            raise ConfigurationError(f"configuration browser path is not a directory: {directory}")
        entries = []
        if directory.is_dir():
            try:
                children = sorted(
                    directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())
                )
            except OSError as error:
                raise ConfigurationError(
                    f"cannot browse configuration directory {directory}: {error}"
                ) from error
            for item in children:
                if item.is_dir() or (item.is_file() and item.suffix.lower() == ".json"):
                    entries.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "kind": "directory" if item.is_dir() else "config",
                        }
                    )
                if len(entries) >= 500:
                    break
        return jsonify(
            {
                "directory": str(directory),
                "parent": str(directory.parent),
                "exists": directory.is_dir(),
                "entries": entries,
            }
        )

    @app.get("/api/config-file")
    def config_file():
        workspace = current_workspace()
        path = resolved_path(str(request.args.get("path") or ""), workspace)
        if path.suffix.lower() != ".json" or not path.is_file():
            raise ConfigurationError(f"configuration file does not exist: {path}")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ConfigurationError("configuration file must not exceed 10 MiB")
        try:
            config = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"cannot read configuration {path}: {error}") from error
        return jsonify({"path": str(path), "config": config})

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
        workspace = current_workspace()
        with state.render_lock:
            if raw_sources is not None:
                if not isinstance(raw_sources, list):
                    raise ConfigurationError("sources must be an array")
                sources = workspace.catalog.replace_sources(
                    [_source_spec(item) for item in raw_sources], module
                )
            else:
                sources = workspace.catalog.set_config_module(module)
        log(f"Config module set to: {module or '(none)'}")
        return jsonify(
            {
                "config_module": module,
                "sources": sources,
                "expression_variables": workspace.catalog.expression_variables(),
            }
        )

    @app.post("/api/sources")
    def add_source():
        metadata = current_workspace().catalog.add(
            _source_spec(request.get_json(silent=False))
        )
        log(f"Registered lazy source {metadata['name']}: {metadata['path']}")
        return jsonify(metadata), 201

    @app.put("/api/sources/<name>")
    def update_source(name: str):
        metadata = current_workspace().catalog.update(
            name, _source_spec(request.get_json(silent=False))
        )
        log(f"Updated lazy source {name} as {metadata['name']}: {metadata['path']}")
        return jsonify(metadata)

    @app.delete("/api/sources/<name>")
    def remove_source(name: str):
        current_workspace().catalog.remove(name)
        return jsonify({"removed": name})

    @app.get("/api/sources/<name>/preview")
    def source_preview(name: str):
        rows = max(1, min(100, int(request.args.get("rows", "10"))))
        frame = current_workspace().catalog.lazy(name).head(rows).collect(engine="streaming")
        return jsonify({"columns": frame.columns, "rows": frame.to_dicts()})

    @app.get("/api/sources/<name>/column-excerpt")
    def source_column_excerpt(name: str):
        column = str(request.args.get("column") or "").strip()
        if not column:
            raise ConfigurationError("column is required")
        try:
            intermediate = int(request.args.get("intermediate", "3"))
        except ValueError as error:
            raise ConfigurationError("intermediate value count must be an integer") from error
        return jsonify(current_workspace().catalog.column_excerpt(name, column, intermediate))

    @app.get("/api/sources/<name>/column-values")
    def source_column_values(name: str):
        column = str(request.args.get("column") or "").strip()
        if not column:
            raise ConfigurationError("column is required")
        try:
            offset = int(request.args.get("offset", "0"))
            limit = int(request.args.get("limit", "250"))
        except ValueError as error:
            raise ConfigurationError("column-value offset and limit must be integers") from error
        return jsonify(current_workspace().catalog.column_values(name, column, offset, limit))

    def request_config(workspace: WorkspaceState) -> dict[str, Any]:
        config = validate_config(request.get_json(silent=False), state.registry)
        if config["config_module"] != workspace.catalog.config_module:
            raise ConfigurationError(
                "configuration module has not been applied; apply it before rendering or exporting"
            )
        return config

    @app.post("/api/validate")
    def validate():
        workspace = current_workspace()
        config = request_config(workspace)
        for layer in config["layers"]:
            if layer.get("enabled", True):
                workspace.engine.execute(layer)
        return jsonify(config)

    def render(formats: list[str] | None = None):
        workspace = current_workspace()
        config = request_config(workspace)
        with state.render_lock:
            return config, render_artifacts(config, workspace.engine, state.registry, formats)

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
        workspace = current_workspace()
        config = request_config(workspace)
        with state.render_lock:
            script = generate_script(config, workspace.catalog, state.registry)
        return send_file(
            BytesIO(script.encode()),
            as_attachment=True,
            download_name=f"{config['filename']}.py",
            mimetype="text/x-python",
        )

    @app.post("/api/save")
    def save():
        workspace = current_workspace()
        config, (artifacts, cache_states) = render()
        output_dir = resolved_path(config["output_dir"], workspace)
        files = _save(output_dir, artifacts)
        log(f"Saved {len(files)} artifact(s) under {output_dir}")
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
