"""Generic Matplotlib composition and artifact export."""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any

import matplotlib as mpl
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import polars as pl

from .errors import ConfigurationError
from .query import QueryEngine
from .registry import PlotContext, Registry

mpl.rcParams["pdf.fonttype"] = 42
mpl.rcParams["ps.fonttype"] = 42

SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
DEFAULT_STYLE = {
    "color": "#0072b2",
    "alpha": 1.0,
    "linewidth": 1.5,
    "marker": "none",
    "markersize": 4,
}


def default_config(config_module: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "config_module": config_module,
        "filename": "explorative-plot",
        "sources": [],
        "figure": {"width": 8.0, "height": 4.5, "dpi": 150, "font_family": "default"},
        "axes": {
            "xlabel": "",
            "ylabel": "",
            "secondary_ylabel": "",
            "xscale": "linear",
            "yscale": "linear",
            "secondary_yscale": "linear",
            "xmin": None,
            "xmax": None,
            "ymin": None,
            "ymax": None,
            "secondary_ymin": None,
            "secondary_ymax": None,
            "x_grid": False,
            "y_grid": True,
            "secondary_y_grid": False,
            "major_x_ticks": True,
            "minor_x_ticks": False,
            "custom_x_ticks": [],
            "custom_x_tick_labels": [],
            "x_tick_rotation": 0,
            "x_tick_horizontal_alignment": "center",
            "x_tick_vertical_alignment": "top",
        },
        "legend": {"enabled": True, "loc": "best", "ncols": 1},
        "layers": [],
        "annotations": [],
        "export_formats": ["png", "pdf", "json"],
    }


def validate_config(raw: Any, registry: Registry) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration must be an object")
    schema_version = raw.get("schema_version", 1)
    if schema_version not in {1, 2}:
        raise ConfigurationError(f"unsupported configuration schema version: {schema_version}")
    config = default_config()
    for section in ("figure", "axes", "legend"):
        if section in raw:
            if not isinstance(raw[section], dict):
                raise ConfigurationError(f"{section} must be an object")
            config[section].update(raw[section])
    config.update(
        {
            key: raw[key]
            for key in (
                "config_module",
                "filename",
                "sources",
                "layers",
                "annotations",
                "export_formats",
            )
            if key in raw
        }
    )
    config["schema_version"] = 2
    module = config.get("config_module")
    if module is not None and not isinstance(module, str):
        raise ConfigurationError("config_module must be a string or null")
    config["config_module"] = str(module or "").strip() or None
    if SAFE_FILENAME.fullmatch(str(config["filename"])) is None:
        raise ConfigurationError("filename must be a safe basename")
    figure = config["figure"]
    if not 0 < float(figure["width"]) <= 100 or not 0 < float(figure["height"]) <= 100:
        raise ConfigurationError("figure dimensions must be between 0 and 100 inches")
    if figure["font_family"] not in {"default", "monospace"}:
        raise ConfigurationError("font family must be default or monospace")
    axes = config["axes"]
    for key in ("xscale", "yscale", "secondary_yscale"):
        if axes[key] not in {"linear", "log", "symlog", "logit"}:
            raise ConfigurationError(f"invalid {key}: {axes[key]}")
    custom_ticks = axes.get("custom_x_ticks", [])
    labels = axes.get("custom_x_tick_labels", [])
    if not isinstance(custom_ticks, list) or not isinstance(labels, list):
        raise ConfigurationError("custom ticks and labels must be arrays")
    if labels and len(labels) != len(custom_ticks):
        raise ConfigurationError("custom tick labels must match custom tick positions")
    if not isinstance(config["layers"], list) or not config["layers"]:
        raise ConfigurationError("at least one plot layer is required")
    if not any(bool(layer.get("enabled", True)) for layer in config["layers"]):
        raise ConfigurationError("at least one plot layer must be enabled")
    if any(layer.get("plot_type", "line") not in registry.plots for layer in config["layers"]):
        raise ConfigurationError("configuration contains an unknown plot type")
    formats = config["export_formats"]
    if (
        not isinstance(formats, list)
        or not formats
        or any(x not in {"png", "pdf", "json"} for x in formats)
    ):
        raise ConfigurationError("export formats must contain png, pdf, and/or json")
    return config


def _groups(frame: pl.DataFrame) -> list[tuple[str | None, pl.DataFrame]]:
    if "_group" not in frame.columns:
        return [(None, frame)]
    values = frame.get_column("_group").drop_nulls().unique(maintain_order=True).to_list()
    return [(str(value), frame.filter(pl.col("_group") == value)) for value in values]


def build_figure(config: dict[str, Any], engine: QueryEngine, registry: Registry):
    figure = config["figure"]
    fig, primary = plt.subplots(figsize=(float(figure["width"]), float(figure["height"])))
    secondary = None
    state: dict[str, Any] = {}
    cache_states: list[dict[str, str]] = []
    for raw_layer in config["layers"]:
        if not raw_layer.get("enabled", True):
            continue
        frame, layer, cache_state = engine.execute(raw_layer)
        cache_states.append({"layer": layer["id"], "cache": cache_state})
        axis = primary
        if layer["secondary_y"]:
            secondary = secondary or primary.twinx()
            axis = secondary
        for group, group_frame in _groups(frame):
            label = layer["label"] if group is None else f"{layer['label']}: {group}"
            style = {**DEFAULT_STYLE, **layer["style"]}
            if style.get("marker") == "none":
                style["marker"] = None
            context = PlotContext(
                ax=axis,
                frame=group_frame,
                layer=layer,
                x=group_frame.get_column("_x").to_list(),
                y=group_frame.get_column("_y").drop_nulls().to_list()
                if layer["plot_type"] in {"histogram", "box", "violin"}
                else group_frame.get_column("_y").to_list(),
                label=label,
                style=style,
                state=state,
            )
            registry.plots[layer["plot_type"]](context)
    axes = config["axes"]
    primary.set_xscale(axes["xscale"])
    primary.set_yscale(axes["yscale"])
    if secondary is not None:
        secondary.set_yscale(axes["secondary_yscale"])
    primary.set_xlabel(str(axes["xlabel"]).replace("\\n", "\n"))
    primary.set_ylabel(str(axes["ylabel"]).replace("\\n", "\n"))
    if secondary is not None:
        secondary.set_ylabel(str(axes["secondary_ylabel"]).replace("\\n", "\n"))
    _limits(primary, axes["xmin"], axes["xmax"], "x")
    _limits(primary, axes["ymin"], axes["ymax"], "y")
    if secondary is not None:
        _limits(secondary, axes["secondary_ymin"], axes["secondary_ymax"], "y")
    _ticks(primary, axes)
    primary.grid(bool(axes["x_grid"]), axis="x", which="major")
    primary.grid(bool(axes["y_grid"]), axis="y", which="major")
    if secondary is not None:
        secondary.grid(bool(axes["secondary_y_grid"]), axis="y", which="major")
    for annotation in config["annotations"]:
        _annotation(primary, annotation)
    if config["legend"]["enabled"]:
        handles, labels = [], []
        for axis in (primary, secondary):
            if axis is not None:
                current_handles, current_labels = axis.get_legend_handles_labels()
                handles.extend(current_handles)
                labels.extend(current_labels)
        if handles:
            primary.legend(
                handles,
                labels,
                loc=config["legend"]["loc"],
                ncols=int(config["legend"]["ncols"]),
            )
    if figure["font_family"] == "monospace":
        for text in fig.findobj(match=plt.Text):
            text.set_fontfamily("monospace")
    fig.tight_layout()
    return fig, cache_states


def _limits(ax, lower: Any, upper: Any, axis: str) -> None:
    if (lower is None) != (upper is None):
        raise ConfigurationError(f"both {axis} limits must be supplied")
    if lower is not None:
        getattr(ax, f"set_{axis}lim")(lower, upper)


def _ticks(ax, axes: dict[str, Any]) -> None:
    if not axes["major_x_ticks"]:
        ax.xaxis.set_major_locator(mticker.NullLocator())
        ax.xaxis.set_minor_locator(mticker.NullLocator())
        return
    ticks = axes["custom_x_ticks"]
    if ticks:
        labels = axes["custom_x_tick_labels"] or [str(value) for value in ticks]
        ax.set_xticks(ticks, labels)
    if axes["minor_x_ticks"]:
        locator = (
            mticker.LogLocator(base=10, subs=tuple(range(2, 10)))
            if axes["xscale"] == "log"
            else mticker.AutoMinorLocator()
        )
        ax.xaxis.set_minor_locator(locator)
        ax.xaxis.set_minor_formatter(mticker.NullFormatter())
    else:
        ax.xaxis.set_minor_locator(mticker.NullLocator())
    plt.setp(
        ax.get_xticklabels(),
        rotation=float(axes["x_tick_rotation"]),
        horizontalalignment=axes["x_tick_horizontal_alignment"],
        verticalalignment=axes["x_tick_vertical_alignment"],
    )


def _annotation(ax, item: dict[str, Any]) -> None:
    kind = item.get("kind")
    color = item.get("color", "#666666")
    alpha = float(item.get("alpha", 0.6))
    if kind == "vline":
        ax.axvline(item["x"], color=color, alpha=alpha, linestyle=item.get("linestyle", "--"))
    elif kind == "hline":
        ax.axhline(item["y"], color=color, alpha=alpha, linestyle=item.get("linestyle", "--"))
    elif kind == "vspan":
        ax.axvspan(item["x1"], item["x2"], color=color, alpha=alpha)
    elif kind == "hspan":
        ax.axhspan(item["y1"], item["y2"], color=color, alpha=alpha)
    elif kind == "text":
        ax.text(item["x"], item["y"], str(item.get("text", "")).replace("\\n", "\n"))
    else:
        raise ConfigurationError(f"unsupported annotation kind: {kind}")


def render_artifacts(
    config: dict[str, Any],
    engine: QueryEngine,
    registry: Registry,
    formats: list[str] | None = None,
) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    formats = formats or config["export_formats"]
    fig, cache_states = build_figure(config, engine, registry)
    artifacts: dict[str, bytes] = {}
    try:
        for output_format in formats:
            if output_format == "json":
                artifacts[f"{config['filename']}.json"] = (
                    json.dumps(config, indent=2) + "\n"
                ).encode()
                continue
            buffer = BytesIO()
            fig.savefig(
                buffer,
                format=output_format,
                dpi=int(config["figure"]["dpi"]),
                bbox_inches="tight",
            )
            artifacts[f"{config['filename']}.{output_format}"] = buffer.getvalue()
    finally:
        plt.close(fig)
    return artifacts, cache_states


def preview(artifacts: dict[str, bytes]) -> list[dict[str, str]]:
    return [
        {"filename": name, "image": "data:image/png;base64," + base64.b64encode(content).decode()}
        for name, content in artifacts.items()
        if Path(name).suffix == ".png"
    ]
