"""Command-line entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path

from .cache import QueryCache
from .data import DataCatalog, SourceSpec, discover_local_config_modules
from .logging import log
from .registry import builtins, load_plugin
from .server import ApplicationState, create_app


def _source(value: str) -> SourceSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("source must use NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not path:
        raise argparse.ArgumentTypeError("source must use nonempty NAME=PATH")
    return SourceSpec(name=name, path=path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local lazy explorative plotting workbench."
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        type=_source,
        metavar="NAME=PATH",
        help="Register a CSV, Parquet, NDJSON, or IPC source; repeat as needed.",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        type=Path,
        help="Load a Python plugin defining register(registry); repeat as needed.",
    )
    parser.add_argument(
        "--config-module",
        help=(
            "Import portable source-path variables from a dotted Python module. "
            "If omitted, a single local <package>.config module is auto-detected."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("plots"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".elp-cache"))
    parser.add_argument("--memory-cache-entries", type=int, default=16)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.memory_cache_entries < 0:
        raise SystemExit("--memory-cache-entries must be nonnegative")
    registry = builtins()
    for path in args.plugin:
        load_plugin(path, registry)
        log(f"Loaded plugin: {path.resolve()}")
    config_module = args.config_module
    if config_module is None:
        detected_modules = discover_local_config_modules()
        if len(detected_modules) == 1:
            config_module = detected_modules[0]
            log(f"Auto-detected config module: {config_module}")
        elif len(detected_modules) > 1:
            log(
                "Multiple local config modules found; select one with "
                f"--config-module: {', '.join(detected_modules)}"
            )

    catalog = DataCatalog(config_module)
    if args.config_module:
        log(f"Loaded config module: {config_module}")
    for spec in args.source:
        metadata = catalog.add(spec)
        log(f"Registered lazy source {metadata['name']}: {metadata['path']}")
    cache = QueryCache(args.cache_dir, args.memory_cache_entries)
    state = ApplicationState(catalog, registry, cache, args.output_dir.resolve())
    app = create_app(state)
    log(f"Explorative Live Plotting available at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
