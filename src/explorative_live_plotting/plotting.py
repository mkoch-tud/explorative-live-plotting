"""Generic Matplotlib composition and artifact export."""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta
from io import BytesIO
import json
import math
from pathlib import Path
import re
from typing import Any

import matplotlib as mpl
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import polars as pl

from .errors import ConfigurationError
from .query import QueryEngine
from .registry import PlotContext, Registry

mpl.rcParams["pdf.fonttype"] = 42
mpl.rcParams["ps.fonttype"] = 42

SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ANNOTATION_KINDS = {"vline", "hline", "vspan", "hspan", "text"}
ANNOTATION_X_INFERENCE = {"manual", "min_x", "max_x", "x_at_min_y", "x_at_max_y"}
ANNOTATION_Y_INFERENCE = {"manual", "min_y", "max_y", "y_at_min_x", "y_at_max_x"}
STD_COLORS = ["#375E97", "#FB6542", "#c1195c", "#37975e"]
DEFAULT_STYLE = {
    "color": STD_COLORS[0],
    "alpha": 1.0,
    "linewidth": 1.5,
    "marker": "none",
    "markersize": 4,
}

LEGACY_CONFIG_KEYS = {"data_sources", "lines", "x_axis", "y_axis", "chart"}


def default_config(config_module: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "config_module": config_module,
        "filename": "explorative-plot",
        "sources": [],
        "figure": {
            "width": 5.6,
            "height": 2.8,
            "dpi": 150,
            "font_family": "default",
            "font_size": 12.0,
        },
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
            "grid_alpha": 0.5,
            "major_x_ticks": True,
            "minor_x_ticks": False,
            "minor_y_ticks": False,
            "secondary_minor_y_ticks": False,
            "custom_x_ticks": [],
            "custom_x_tick_labels": [],
            "custom_y_ticks": [],
            "custom_y_tick_labels": [],
            "y_tick_min": None,
            "y_tick_max": None,
            "y_tick_step": None,
            "custom_secondary_y_ticks": [],
            "custom_secondary_y_tick_labels": [],
            "secondary_y_tick_min": None,
            "secondary_y_tick_max": None,
            "secondary_y_tick_step": None,
            "x_value_ticks": False,
            "x_value_tick_interval": 1,
            "x_datetime_format": "",
            "x_tick_rotation": 0,
            "x_tick_horizontal_alignment": "center",
            "x_tick_vertical_alignment": "top",
            "x_engineering": False,
            "y_engineering": False,
            "secondary_y_engineering": False,
            "label_font_size_override": False,
            "label_font_size": 12.0,
            "tick_font_size_override": False,
            "tick_font_size": 12.0,
        },
        "legend": {
            "enabled": True,
            "loc": "upper left",
            "ncols": 1,
            "bbox_enabled": False,
            "bbox_x": 0.115,
            "bbox_y": 1.0,
            "handlelength": 1.5,
            "columnspacing": 0.8,
            "handletextpad": 0.5,
            "opacity": 0.8,
            "font_size_override": False,
            "font_size": 12.0,
        },
        "broken_y_axis": {"enabled": False, "gap": 0.1, "ranges": []},
        "stages": {"enabled": False, "overlay_alpha": 0.25, "steps": []},
        "layers": [],
        "annotations": [],
        "export_formats": ["png", "pdf", "json"],
    }


def is_legacy_config(raw: Any) -> bool:
    """Detect the former workbench schema without relying on its reused version number."""
    return isinstance(raw, dict) and "data_sources" in raw and "lines" in raw and bool(
        LEGACY_CONFIG_KEYS.intersection(raw)
    )


def migrate_legacy_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Map a former-schema configuration to the current editable representation."""
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration must be an object")
    if not is_legacy_config(raw):
        return deepcopy(raw), []

    config = default_config(str(raw.get("config_module") or "").strip() or None)
    warnings: list[str] = []
    config["filename"] = raw.get("filename", config["filename"])

    figure = raw.get("figure", {})
    if not isinstance(figure, dict):
        warnings.append("figure: expected an object; defaults were used")
        figure = {}
    for field in ("width", "height", "dpi", "font_family", "font_size"):
        if field in figure:
            config["figure"][field] = figure[field]

    data_sources = raw.get("data_sources", {})
    if not isinstance(data_sources, dict):
        warnings.append("data_sources: expected an object; no sources were imported")
        data_sources = {}
    for name, value in data_sources.items():
        try:
            config["sources"].append(_migrate_legacy_source(name, value))
        except ConfigurationError as error:
            warnings.append(f"data_sources.{name}: {error}")

    chart = raw.get("chart", {})
    if not isinstance(chart, dict):
        warnings.append("chart: expected an object; time-chart defaults were used")
        chart = {}
    chart_mode = str(chart.get("mode", "time"))
    if chart_mode not in {"time", "ranked"}:
        warnings.append(
            f"chart.mode: unsupported value {chart_mode!r}; layers were mapped as a time chart"
        )
        chart_mode = "time"
    if chart_mode == "ranked":
        chart = dict(chart)
        top_n = chart.get("top_n")
        if top_n is not None:
            try:
                top_n = int(top_n)
                if top_n < 1:
                    raise ValueError
            except (TypeError, ValueError):
                warnings.append("chart.top_n: expected a positive integer or null; it was ignored")
                top_n = None
            chart["top_n"] = top_n
        for field in ("value_min", "value_max"):
            value = chart.get(field)
            if value is None:
                continue
            try:
                chart[field] = float(value)
                if not math.isfinite(chart[field]):
                    raise ValueError
            except (TypeError, ValueError):
                warnings.append(f"chart.{field}: expected a number or null; it was ignored")
                chart[field] = None
        if chart.get("sort") not in {None, "ascending", "descending"}:
            warnings.append(
                f"chart.sort: unsupported value {chart.get('sort')!r}; source order was retained"
            )
            chart["sort"] = None
        if chart.get("bar_values", "absolute") != "absolute":
            warnings.append(
                "chart.bar_values: only absolute ranked values are available; "
                "values were not rescaled"
            )

    lines = raw.get("lines", [])
    if not isinstance(lines, list):
        warnings.append("lines: expected an array; no layers were imported")
        lines = []
    for index, legacy_line in enumerate(lines):
        try:
            config["layers"].append(
                _migrate_legacy_line(legacy_line, index, chart_mode, chart)
            )
        except ConfigurationError as error:
            warnings.append(f"lines[{index}]: {error}")

    x_axis = _legacy_section(raw, "x_axis", warnings)
    y_axis = _legacy_section(raw, "y_axis", warnings)
    secondary_y_axis = _legacy_section(raw, "secondary_y_axis", warnings)
    config["axes"].update(
        {
            "xlabel": x_axis.get("label", ""),
            "ylabel": y_axis.get("label", ""),
            "secondary_ylabel": secondary_y_axis.get("label", ""),
            "xscale": chart.get("xscale", "linear"),
            "yscale": "log" if y_axis.get("logscale", False) else "linear",
            "secondary_yscale": (
                "log" if secondary_y_axis.get("logscale", False) else "linear"
            ),
            "xmin": x_axis.get("min"),
            "xmax": x_axis.get("max"),
            "ymin": y_axis.get("min"),
            "ymax": y_axis.get("max"),
            "secondary_ymin": secondary_y_axis.get("min"),
            "secondary_ymax": secondary_y_axis.get("max"),
            "x_grid": bool(x_axis.get("grid", False)),
            "y_grid": bool(y_axis.get("grid", True)),
            "secondary_y_grid": bool(secondary_y_axis.get("grid", False)),
            "major_x_ticks": bool(x_axis.get("ticks_enabled", True)),
            "minor_x_ticks": bool(x_axis.get("minor_ticks", False)),
            "minor_y_ticks": bool(y_axis.get("minor_ticks", False)),
            "secondary_minor_y_ticks": bool(
                secondary_y_axis.get("minor_ticks", False)
            ),
            "custom_x_ticks": (
                [] if chart_mode == "ranked" else deepcopy(chart.get("x_ticks") or [])
            ),
            "x_value_ticks": chart_mode == "ranked",
            "x_value_tick_interval": (
                chart.get("rank_tick_interval", 1)
                if chart_mode == "ranked" and chart.get("rank_ticks") == "interval"
                else 1
            ),
            "x_tick_rotation": x_axis.get("tick_rotation", 0),
            "x_tick_horizontal_alignment": x_axis.get("tick_alignment", "center"),
            "x_tick_vertical_alignment": x_axis.get("tick_vertical_alignment", "top"),
            "y_engineering": bool(y_axis.get("engineering", False)),
            "secondary_y_engineering": bool(secondary_y_axis.get("engineering", False)),
        }
    )

    legend = _legacy_section(raw, "legend", warnings)
    for field in (
        "enabled",
        "loc",
        "ncols",
        "handlelength",
        "columnspacing",
        "handletextpad",
        "opacity",
    ):
        if field in legend:
            config["legend"][field] = legend[field]
    bbox = legend.get("bbox_to_anchor")
    if bbox is not None:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 2:
            warnings.append("legend.bbox_to_anchor: expected [x, y]; the anchor was ignored")
        else:
            config["legend"].update(bbox_enabled=True, bbox_x=bbox[0], bbox_y=bbox[1])

    annotations = raw.get("annotations", [])
    if not isinstance(annotations, list):
        warnings.append("annotations: expected an array; no annotations were imported")
        annotations = []
    config["annotations"] = []
    for index, annotation in enumerate(annotations):
        try:
            config["annotations"].append(_migrate_legacy_annotation(annotation, index))
        except ConfigurationError as error:
            warnings.append(f"annotations[{index}]: {error}")
    config["stages"] = _migrate_legacy_stages(
        raw, config["layers"], annotations, warnings
    )

    formats = raw.get("export_formats", config["export_formats"])
    if not isinstance(formats, list):
        warnings.append("export_formats: expected an array; defaults were used")
    else:
        supported_formats = [item for item in formats if item in {"png", "pdf", "json"}]
        unsupported_formats = [item for item in formats if item not in {"png", "pdf", "json"}]
        if unsupported_formats:
            warnings.append(
                f"export_formats: ignored unsupported values {unsupported_formats!r}"
            )
        if supported_formats:
            config["export_formats"] = supported_formats

    broken = raw.get("broken_y_axis")
    if broken is not None:
        if not isinstance(broken, dict):
            warnings.append("broken_y_axis: expected an object; it was ignored")
        else:
            config["broken_y_axis"] = deepcopy(broken)

    dropout = raw.get("asn_loop_dropouts")
    if isinstance(dropout, dict) and dropout.get("continuous_only", False):
        warnings.append("Legacy ASN continuous-dropout preprocessing is not mapped.")
    cohorts = raw.get("asn_cohort_changes")
    if isinstance(cohorts, dict) and cohorts.get("mode", "absolute") != "absolute":
        warnings.append("Legacy non-absolute ASN cohort-change mode is not mapped.")
    return config, warnings


def _migrate_legacy_annotation(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("expected an object")
    if "x_start_mode" not in raw and "y_start_mode" not in raw:
        return deepcopy(raw)
    kind = str(raw.get("kind", "text"))
    item: dict[str, Any] = {
        "id": str(raw.get("id") or f"annotation-{index + 1}"),
        "enabled": bool(raw.get("enabled", True)),
        "kind": kind,
        "label": str(raw.get("label") or f"Annotation {index + 1}"),
        "text": str(raw.get("label") or ""),
        "color": raw.get("line_color", raw.get("text_color", "#666666")),
        "text_color": raw.get("text_color", raw.get("line_color", "#666666")),
        "alpha": raw.get("alpha", 0.6),
        "fontsize": raw.get("font_size", 10),
        "linewidth": raw.get("line_width", 1.0),
        "linestyle": raw.get("linestyle", "--"),
        "show_in_legend": bool(raw.get("show_in_legend", False)),
        "legend_label": str(raw.get("legend_label") or raw.get("label") or ""),
    }
    if kind == "vline":
        item["x"] = raw.get("x_start")
    elif kind == "hline":
        item["y"] = raw.get("y_start")
    elif kind == "vspan":
        item.update(x1=raw.get("x_start"), x2=raw.get("x_end"))
    elif kind == "hspan":
        item.update(y1=raw.get("y_start"), y2=raw.get("y_end"))
    elif kind == "text":
        item.update(x=raw.get("x_start"), y=raw.get("label_y", raw.get("y_start")))
    else:
        raise ConfigurationError(f"unsupported annotation kind: {kind}")
    if raw.get("label_y") is not None:
        item["text_y"] = raw["label_y"]
    if raw.get("x_start") is not None:
        item["text_x"] = raw["x_start"]
        try:
            offset = float(raw.get("x_offset_days", 0))
            if offset:
                item["text_x"] = (
                    datetime.fromisoformat(str(raw["x_start"]).replace("Z", "+00:00"))
                    + timedelta(days=offset)
                ).isoformat()
        except (TypeError, ValueError):
            pass
    return item


def _legacy_section(
    raw: dict[str, Any], name: str, warnings: list[str]
) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        warnings.append(f"{name}: expected an object; defaults were used")
        return {}
    return value


def _migrate_legacy_source(name: Any, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        options = {"try_parse_dates": True} if Path(value).suffix.lower() == ".csv" else {}
        return {"name": str(name), "path": value, "format": "auto", "options": options}
    if not isinstance(value, dict):
        raise ConfigurationError(f"legacy data source {name!r} must be a path or object")
    options = value.get("options") or {}
    if not isinstance(options, dict):
        raise ConfigurationError(f"legacy data source {name!r} options must be an object")
    path = str(value.get("path", ""))
    data_format = str(value.get("format", "auto"))
    if "try_parse_dates" not in options and (
        data_format == "csv" or (data_format == "auto" and Path(path).suffix.lower() == ".csv")
    ):
        options = {**options, "try_parse_dates": True}
    return {
        "name": str(name),
        "path": path,
        "format": data_format,
        "options": deepcopy(options),
    }


def _migrate_legacy_line(
    raw: Any, index: int, chart_mode: str, chart: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("each legacy line must be an object")
    style = deepcopy(raw.get("style") or {})
    if not isinstance(style, dict):
        raise ConfigurationError("legacy line style must be an object")
    for field in ("color", "alpha", "linewidth", "linestyle", "marker", "markersize"):
        if field in raw:
            style[field] = raw[field]
    filters = deepcopy(raw.get("filters") or [])
    if not isinstance(filters, list):
        raise ConfigurationError("legacy line filters must be an array")
    # The former region selector serialized registry keys in lowercase although
    # the historical summary CSV stores RIR names (ARIN, RIPE, ...) uppercase.
    for item in filters:
        if (
            isinstance(item, dict)
            and item.get("column") == "region"
            and item.get("operator", "eq") in {"eq", "ne", "in", "not_in"}
        ):
            value = item.get("value")
            item["value"] = (
                [str(part).upper() for part in value]
                if isinstance(value, list)
                else str(value).upper()
            )
    required_filters = deepcopy(raw.get("required_filters") or [])
    if not isinstance(required_filters, list):
        raise ConfigurationError("legacy line required_filters must be an array")
    if chart_mode == "ranked" and chart.get("scan_date") is not None and not any(
        isinstance(item, dict) and item.get("column") == "scan_date"
        for item in (*required_filters, *filters)
    ):
        required_filters.append(
            {"column": "scan_date", "operator": "eq", "value": chart["scan_date"]}
        )
    x_column = raw.get("x_column")
    if not x_column:
        x_column = "scan_date" if chart_mode == "time" else chart.get("rank_label")
    if not x_column:
        raise ConfigurationError("ranked chart requires chart.rank_label or line.x_column")
    sort = raw.get("sort")
    if sort is None:
        if chart_mode == "ranked":
            sort = {
                "ascending": "y_ascending",
                "descending": "y_descending",
            }.get(chart.get("sort"), "none")
        else:
            sort = "x_ascending"
    result_limit = chart.get("top_n") if chart_mode == "ranked" else raw.get("result_limit")
    if result_limit is not None:
        try:
            result_limit = int(result_limit)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("chart.top_n must be a positive integer or null") from error
        if result_limit < 1:
            raise ConfigurationError("chart.top_n must be a positive integer or null")
    return {
        "id": str(raw.get("id") or f"layer-{index + 1}"),
        "enabled": bool(raw.get("enabled", True)),
        "label": str(raw.get("label") or raw.get("value_column") or f"Line {index + 1}"),
        "source": raw.get("source"),
        "plot_type": raw.get("plot_type", "line"),
        "x_column": x_column,
        "y_column": raw.get("value_column") or raw.get("y_column"),
        "group_column": raw.get("group_column") or None,
        "aggregation": raw.get("aggregation", "none"),
        "aggregation_options": deepcopy(raw.get("aggregation_options") or {}),
        "time_bin": raw.get("time_bin") or None,
        "filter_logic": raw.get("filter_logic", "and"),
        "required_filters": required_filters,
        "filters": filters,
        "sort": sort,
        "limit": raw.get("limit"),
        "result_limit": result_limit,
        "result_y_min": chart.get("value_min") if chart_mode == "ranked" else None,
        "result_y_max": chart.get("value_max") if chart_mode == "ranked" else None,
        "stacked": bool(raw.get("stacked", False)),
        "secondary_y": bool(raw.get("secondary_y", False)),
        "style": style,
        "options": deepcopy(raw.get("options") or {}),
    }


def _migrate_legacy_stages(
    raw: dict[str, Any],
    layers: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    legacy_steps = raw.get("uncover_stages") or []
    if not isinstance(legacy_steps, list):
        warnings.append("uncover_stages: expected an array; stages were disabled")
        legacy_steps = []
    layer_ids = {item["id"] for item in layers}
    annotation_ids = {
        str(item.get("id")) for item in annotations if isinstance(item, dict) and item.get("id")
    }
    steps = []
    for index, step in enumerate(legacy_steps):
        if not isinstance(step, dict):
            warnings.append(f"uncover_stages[{index}]: expected an object; stage was skipped")
            continue
        elements = []
        for identifier in step.get("lines") or []:
            if identifier not in layer_ids:
                warnings.append(
                    f"uncover_stages[{index}].lines: unknown line {identifier!r} was ignored"
                )
                continue
            elements.append({"id": f"layer:{identifier}", "overlay": False})
        for identifier in step.get("annotations") or []:
            if str(identifier) not in annotation_ids:
                warnings.append(
                    f"uncover_stages[{index}].annotations: unknown annotation "
                    f"{identifier!r} was ignored"
                )
                continue
            elements.append({"id": f"annotation:{identifier}", "overlay": False})
        if not elements:
            warnings.append(f"uncover_stages[{index}]: no valid elements; stage was skipped")
            continue
        steps.append(
            {
                "id": str(step.get("id") or f"stage-{index + 1}"),
                "label": str(step.get("label") or f"Stage {index + 1}"),
                "elements": elements,
            }
        )
    return {
        "enabled": bool(raw.get("uncover", False)) and bool(steps),
        "overlay_alpha": 0.25,
        "steps": steps,
    }


def validate_config(raw: Any, registry: Registry) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration must be an object")
    raw, _ = migrate_legacy_config(raw)
    schema_version = raw.get("schema_version", 1)
    if schema_version not in {1, 2}:
        raise ConfigurationError(f"unsupported configuration schema version: {schema_version}")
    config = default_config()
    for section in ("figure", "axes", "legend", "broken_y_axis", "stages"):
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
    _font_size(figure.get("font_size"), "global font size")
    axes = config["axes"]
    for key in ("xscale", "yscale", "secondary_yscale"):
        if axes[key] not in {"linear", "log", "symlog", "logit"}:
            raise ConfigurationError(f"invalid {key}: {axes[key]}")
    _validate_x_limits(axes)
    custom_ticks = axes.get("custom_x_ticks", [])
    labels = axes.get("custom_x_tick_labels", [])
    if not isinstance(custom_ticks, list) or not isinstance(labels, list):
        raise ConfigurationError("custom ticks and labels must be arrays")
    if labels and len(labels) != len(custom_ticks):
        raise ConfigurationError("custom tick labels must match custom tick positions")
    _validate_y_tick_config(axes)
    _validate_y_tick_config(axes, secondary=True)
    try:
        value_tick_interval = int(axes.get("x_value_tick_interval", 1))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("X-value tick interval must be a positive integer") from error
    if isinstance(axes.get("x_value_tick_interval"), bool) or value_tick_interval < 1:
        raise ConfigurationError("X-value tick interval must be a positive integer")
    axes["x_value_ticks"] = bool(axes.get("x_value_ticks", False))
    axes["x_value_tick_interval"] = value_tick_interval
    axes["minor_y_ticks"] = bool(axes.get("minor_y_ticks", False))
    axes["secondary_minor_y_ticks"] = bool(
        axes.get("secondary_minor_y_ticks", False)
    )
    datetime_format = axes.get("x_datetime_format", "")
    if not isinstance(datetime_format, str):
        raise ConfigurationError("datetime tick format must be a string")
    if len(datetime_format) > 100:
        raise ConfigurationError("datetime tick format must be at most 100 characters")
    axes["x_datetime_format"] = datetime_format
    try:
        grid_alpha = float(axes.get("grid_alpha", 0.5))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("grid opacity must be a number") from error
    if not 0 <= grid_alpha <= 1:
        raise ConfigurationError("grid opacity must be between 0 and 1")
    axes["grid_alpha"] = grid_alpha
    _font_size(axes.get("label_font_size"), "label font size")
    _font_size(axes.get("tick_font_size"), "tick font size")
    _font_size(config["legend"].get("font_size"), "legend font size")
    legend = config["legend"]
    valid_legend_locations = {
        "best",
        "upper right",
        "upper left",
        "lower left",
        "lower right",
        "right",
        "center left",
        "center right",
        "lower center",
        "upper center",
        "center",
    }
    if legend["loc"] not in valid_legend_locations:
        raise ConfigurationError(f"invalid legend location: {legend['loc']}")
    try:
        ncols = int(legend["ncols"])
    except (TypeError, ValueError) as error:
        raise ConfigurationError("legend columns must be a positive integer") from error
    if isinstance(legend["ncols"], bool) or ncols < 1:
        raise ConfigurationError("legend columns must be a positive integer")
    legend["ncols"] = ncols
    legend["enabled"] = bool(legend["enabled"])
    legend["bbox_enabled"] = bool(legend["bbox_enabled"])
    for field in (
        "bbox_x",
        "bbox_y",
        "handlelength",
        "columnspacing",
        "handletextpad",
        "opacity",
    ):
        try:
            legend[field] = float(legend[field])
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"legend {field} must be a number") from error
        if not math.isfinite(legend[field]):
            raise ConfigurationError(f"legend {field} must be a finite number")
    if legend["handlelength"] < 0:
        raise ConfigurationError("legend handle length must be nonnegative")
    if legend["columnspacing"] < 0:
        raise ConfigurationError("legend column spacing must be nonnegative")
    if legend["handletextpad"] < 0:
        raise ConfigurationError("legend handle-to-label spacing must be nonnegative")
    if not 0 <= legend["opacity"] <= 1:
        raise ConfigurationError("legend opacity must be between 0 and 1")
    if not isinstance(config["layers"], list) or not config["layers"]:
        raise ConfigurationError("at least one plot layer is required")
    if not any(bool(layer.get("enabled", True)) for layer in config["layers"]):
        raise ConfigurationError("at least one plot layer must be enabled")
    if any(layer.get("plot_type", "line") not in registry.plots for layer in config["layers"]):
        raise ConfigurationError("configuration contains an unknown plot type")
    for index, layer in enumerate(config["layers"]):
        layer.setdefault("id", f"layer-{index + 1}")
        layer["fix_x_values"] = bool(layer.get("fix_x_values", False))
    x_anchors = [layer for layer in config["layers"] if layer["fix_x_values"]]
    if len(x_anchors) > 1:
        raise ConfigurationError("only one layer can fix the shared X values")
    if x_anchors:
        anchor = x_anchors[0]
        if not anchor.get("enabled", True):
            raise ConfigurationError("the layer fixing shared X values must be enabled")
        if anchor.get("plot_type", "line") in {"histogram", "box", "violin"}:
            raise ConfigurationError(
                "histogram, box, and violin layers cannot fix shared X values"
            )
        incompatible = [
            layer.get("label") or layer.get("id") or "unnamed layer"
            for layer in config["layers"]
            if layer.get("enabled", True)
            and layer.get("plot_type", "line") in {"histogram", "box", "violin"}
        ]
        if incompatible:
            raise ConfigurationError(
                "fixed shared X values cannot be combined with histogram, box, or "
                f"violin layers: {', '.join(incompatible)}"
            )
        if config["axes"]["xscale"] != "linear":
            raise ConfigurationError("fixed shared X values require a linear X scale")
    config["annotations"] = _validate_annotations(config["annotations"], config["layers"])
    config["broken_y_axis"] = _validate_broken_y_axis(config["broken_y_axis"], config)
    element_ids = [
        *(f"layer:{item['id']}" for item in config["layers"]),
        *(f"annotation:{item['id']}" for item in config["annotations"]),
    ]
    if len(element_ids) != len(set(element_ids)):
        raise ConfigurationError("layer and annotation IDs must be unique")
    config["stages"] = _validate_stages(config["stages"], config)
    formats = config["export_formats"]
    if (
        not isinstance(formats, list)
        or not formats
        or any(x not in {"png", "pdf", "json"} for x in formats)
    ):
        raise ConfigurationError("export formats must contain png, pdf, and/or json")
    return config


def _validate_broken_y_axis(raw: Any, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("broken_y_axis must be an object")
    enabled = bool(raw.get("enabled", False))
    try:
        gap = float(raw.get("gap", 0.1))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("broken Y-axis gap must be a number") from error
    if not 0 <= gap <= 5:
        raise ConfigurationError("broken Y-axis gap must be between 0 and 5")
    ranges = raw.get("ranges") or []
    if not isinstance(ranges, list):
        raise ConfigurationError("broken Y-axis ranges must be an array")
    normalized = []
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise ConfigurationError("each broken Y-axis range must be an object")
        try:
            lower = float(item["min"])
            upper = float(item["max"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigurationError(
                f"broken Y-axis range {index + 1} requires numeric min and max"
            ) from error
        if lower >= upper:
            raise ConfigurationError(
                f"broken Y-axis range {index + 1} min must be smaller than max"
            )
        if normalized and lower <= normalized[-1]["max"]:
            raise ConfigurationError(
                "broken Y-axis ranges must be non-overlapping and ordered from low to high"
            )
        normalized.append({"min": lower, "max": upper})
    if enabled:
        if len(normalized) < 2:
            raise ConfigurationError("an enabled broken Y axis requires at least two ranges")
        axes = config["axes"]
        if axes.get("ymin") is not None or axes.get("ymax") is not None:
            raise ConfigurationError(
                "use broken Y-axis ranges instead of the regular Y min/max controls"
            )
        if any(layer.get("secondary_y", False) for layer in config["layers"]):
            raise ConfigurationError(
                "broken Y axes and secondary-Y layers cannot be combined in one plot"
            )
    return {"enabled": enabled, "gap": gap, "ranges": normalized}


def _validate_y_tick_config(axes: dict[str, Any], secondary: bool = False) -> None:
    prefix = "secondary_" if secondary else ""
    tick_key = "custom_secondary_y_ticks" if secondary else "custom_y_ticks"
    label_key = (
        "custom_secondary_y_tick_labels" if secondary else "custom_y_tick_labels"
    )
    range_keys = tuple(f"{prefix}y_tick_{field}" for field in ("min", "max", "step"))
    name = "secondary Y" if secondary else "Y"
    ticks = axes.get(tick_key, [])
    labels = axes.get(label_key, [])
    if not isinstance(ticks, list) or not isinstance(labels, list):
        raise ConfigurationError(f"custom {name} ticks and labels must be arrays")
    try:
        ticks = [float(value) for value in ticks]
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"custom {name} ticks must be finite numbers") from error
    if any(not math.isfinite(value) for value in ticks):
        raise ConfigurationError(f"custom {name} ticks must be finite numbers")
    if labels and len(labels) != len(ticks):
        raise ConfigurationError(
            f"custom {name} tick labels must match custom {name} tick positions"
        )
    axes[tick_key] = ticks
    axes[label_key] = [str(value) for value in labels]
    raw_range = [axes.get(field) for field in range_keys]
    if not any(value is not None for value in raw_range):
        for field in range_keys:
            axes[field] = None
        return
    if any(value is None for value in raw_range):
        raise ConfigurationError(f"{name} tick range requires minimum, maximum, and step")
    try:
        lower, upper, step = map(float, raw_range)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{name} tick range values must be finite numbers") from error
    if not all(math.isfinite(value) for value in (lower, upper, step)):
        raise ConfigurationError(f"{name} tick range values must be finite numbers")
    if step <= 0:
        raise ConfigurationError(f"{name} tick step must be greater than zero")
    if lower > upper:
        raise ConfigurationError(f"{name} tick minimum cannot be greater than its maximum")
    if (upper - lower) / step > 10_000:
        raise ConfigurationError(f"{name} tick range cannot produce more than 10,001 ticks")
    axes[range_keys[0]], axes[range_keys[1]], axes[range_keys[2]] = lower, upper, step


def _font_size(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a number") from error
    if not 1 <= result <= 200:
        raise ConfigurationError(f"{field} must be between 1 and 200")
    return result


def _validate_x_limits(axes: dict[str, Any]) -> None:
    """Keep numeric bounds numeric while permitting ISO date/time or category values."""
    for key in ("xmin", "xmax"):
        value = axes.get(key)
        if value is None or value == "":
            axes[key] = None
            continue
        if isinstance(value, bool):
            raise ConfigurationError(f"{key} must be a number, date/time, or X value")
        if isinstance(value, (date, datetime)):
            continue
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ConfigurationError(f"{key} must be finite")
            continue
        if not isinstance(value, str):
            raise ConfigurationError(f"{key} must be a number, date/time, or X value")
        value = value.strip()
        if not value or len(value) > 200:
            raise ConfigurationError(f"{key} must be 1 to 200 characters")
        try:
            numeric = float(value)
        except ValueError:
            axes[key] = value
        else:
            if not math.isfinite(numeric):
                raise ConfigurationError(f"{key} must be finite")
            axes[key] = numeric


def _validate_annotations(raw: Any, layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ConfigurationError("annotations must be an array")
    result = []
    required = {
        "vline": ("x",),
        "hline": ("y",),
        "vspan": ("x1", "x2"),
        "hspan": ("y1", "y2"),
        "text": ("x", "y"),
    }
    valid_layer_ids = {str(layer.get("id")) for layer in layers}
    for index, annotation in enumerate(raw):
        if not isinstance(annotation, dict):
            raise ConfigurationError("each annotation must be an object")
        item = dict(annotation)
        item["id"] = str(item.get("id") or f"annotation-{index + 1}")
        item["enabled"] = bool(item.get("enabled", True))
        kind = item.get("kind", "text")
        if kind not in ANNOTATION_KINDS:
            raise ConfigurationError(f"unsupported annotation kind: {kind}")
        inference = item.get("inference") or {}
        if not isinstance(inference, dict):
            raise ConfigurationError("annotation inference must be an object")
        x_mode = str(inference.get("x", "manual"))
        y_mode = str(inference.get("y", "manual"))
        if x_mode not in ANNOTATION_X_INFERENCE:
            raise ConfigurationError(f"unsupported annotation X inference: {x_mode}")
        if y_mode not in ANNOTATION_Y_INFERENCE:
            raise ConfigurationError(f"unsupported annotation Y inference: {y_mode}")
        layer_ids = inference.get("layer_ids") or []
        if not isinstance(layer_ids, list) or any(
            not isinstance(layer_id, str) for layer_id in layer_ids
        ):
            raise ConfigurationError("annotation inference layer IDs must be an array of strings")
        unknown_ids = [layer_id for layer_id in layer_ids if layer_id not in valid_layer_ids]
        if unknown_ids:
            raise ConfigurationError(
                f"annotation {item['id']} references unknown layers: {', '.join(unknown_ids)}"
            )
        if (x_mode != "manual" or y_mode != "manual") and not layer_ids:
            raise ConfigurationError(
                f"annotation {item['id']} inference requires at least one plot layer"
            )
        for field in required[kind]:
            if field == "x" and x_mode != "manual":
                continue
            if field == "y" and y_mode != "manual":
                continue
            if item.get(field) is None or item.get(field) == "":
                raise ConfigurationError(f"{kind} annotation requires {field}")
        item["kind"] = kind
        item["label"] = str(item.get("label") or item.get("text") or item["id"])
        item["text"] = str(item.get("text", ""))
        item["show_in_legend"] = bool(item.get("show_in_legend", False))
        item["text_background_enabled"] = bool(
            item.get("text_background_enabled", False)
        )
        item["text_background_color"] = str(
            item.get("text_background_color", "#ffffff")
        )
        item["legend_label"] = str(
            item.get("legend_label") or item["label"] or item["text"] or item["id"]
        )
        item["inference"] = {"x": x_mode, "y": y_mode, "layer_ids": layer_ids}
        item["fontsize"] = _font_size(item.get("fontsize", 10), "annotation font size")
        try:
            alpha = float(item.get("alpha", 0.6))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("annotation alpha must be a number") from error
        if not 0 <= alpha <= 1:
            raise ConfigurationError("annotation alpha must be between 0 and 1")
        item["alpha"] = alpha
        result.append(item)
    return result


def _validate_stages(raw: Any, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("stages must be an object")
    stages = dict(raw)
    stages["enabled"] = bool(stages.get("enabled", False))
    try:
        alpha = float(stages.get("overlay_alpha", 0.25))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("stage overlay alpha must be a number") from error
    if not 0 <= alpha <= 1:
        raise ConfigurationError("stage overlay alpha must be between 0 and 1")
    stages["overlay_alpha"] = alpha
    valid_ids = {
        *(f"layer:{item['id']}" for item in config["layers"]),
        *(f"annotation:{item['id']}" for item in config["annotations"]),
    }
    steps = stages.get("steps") or []
    if not isinstance(steps, list):
        raise ConfigurationError("stage steps must be an array")
    if stages["enabled"] and not steps:
        steps = _default_stage_steps(config)
    normalized = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ConfigurationError("each stage must be an object")
        elements = step.get("elements", [])
        if not isinstance(elements, list):
            raise ConfigurationError("stage elements must be an array")
        selected = []
        for element in elements:
            if not isinstance(element, (str, dict)):
                raise ConfigurationError("each stage element must be a string or object")
            item = {"id": element, "overlay": False} if isinstance(element, str) else dict(element)
            if item.get("id") not in valid_ids:
                raise ConfigurationError(f"unknown stage element: {item.get('id')}")
            selected.append({"id": item["id"], "overlay": bool(item.get("overlay", False))})
        if not selected:
            raise ConfigurationError("each enabled stage must contain at least one element")
        normalized.append(
            {
                "id": str(step.get("id") or f"stage-{index + 1}"),
                "label": str(step.get("label") or f"Stage {index + 1}"),
                "elements": selected,
            }
        )
    stages["steps"] = normalized
    return stages


def _default_stage_steps(config: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = [
        *(f"layer:{item['id']}" for item in config["layers"] if item.get("enabled", True)),
        *(
            f"annotation:{item['id']}"
            for item in config["annotations"]
            if item.get("enabled", True)
        ),
    ]
    steps = []
    for index, current in enumerate(ordered):
        elements = [
            {"id": item, "overlay": item.startswith("layer:") and item != current}
            for item in ordered[: index + 1]
        ]
        steps.append(
            {
                "id": f"stage-{index + 1}",
                "label": f"Stage {index + 1}",
                "elements": elements,
            }
        )
    return steps


def _groups(frame: pl.DataFrame) -> list[tuple[str | None, pl.DataFrame]]:
    if "_group" not in frame.columns:
        return [(None, frame)]
    values = frame.get_column("_group").drop_nulls().unique(maintain_order=True).to_list()
    return [(str(value), frame.filter(pl.col("_group") == value)) for value in values]


def _layer_legend_proxy(layer: dict[str, Any], color: str, alpha: float) -> Any:
    style = {**DEFAULT_STYLE, "color": color, **(layer.get("style") or {})}
    plot_type = layer.get("plot_type", "line")
    if plot_type in {"bar", "area", "histogram"}:
        return Patch(
            facecolor=style["color"],
            edgecolor=style["color"],
            alpha=alpha,
        )
    marker = style.get("marker")
    if marker == "none":
        marker = None
    if plot_type == "scatter" and marker is None:
        marker = "o"
    return Line2D(
        [],
        [],
        color=style["color"],
        alpha=alpha,
        linewidth=float(style.get("linewidth", 1.5)),
        linestyle="none" if plot_type == "scatter" else style.get("linestyle", "-"),
        marker=marker,
        markersize=float(style.get("markersize", 4)),
    )


def _annotation_legend_proxy(item: dict[str, Any], alpha: float) -> Any:
    color = item.get("text_color", item.get("color", "#666666"))
    kind = item.get("kind", "text")
    if kind in {"vspan", "hspan"}:
        return Patch(facecolor=color, edgecolor=color, alpha=alpha)
    return Line2D(
        [],
        [],
        color=color,
        alpha=alpha,
        linewidth=float(item.get("linewidth", 1.0)),
        linestyle="none" if kind == "text" else item.get("linestyle", "--"),
        marker="o" if kind == "text" else None,
        markersize=4,
    )


def _stage_legend_entries(
    config: dict[str, Any],
    engine: QueryEngine,
    layer_frames: dict[str, pl.DataFrame],
    shared_x_values: list[Any] | None,
) -> tuple[list[Any], list[str], list[bool]]:
    handles: list[Any] = []
    labels: list[str] = []
    visibility: list[bool] = []
    selections = config.get("_stage_legend_elements", {})
    overlay_alpha = float(config["stages"].get("overlay_alpha", 0.25))
    for index, raw_layer in enumerate(config.get("_stage_legend_layers", [])):
        if raw_layer.get("plot_type", "line") in {"box", "violin", "hexbin"}:
            continue
        group_names: list[str | None] = [None]
        if raw_layer.get("group_column"):
            frame = layer_frames.get(str(raw_layer.get("id")))
            if frame is None:
                query_layer = raw_layer
                if shared_x_values is not None:
                    query_layer = {
                        **raw_layer,
                        "fixed_x_values": shared_x_values,
                        "result_limit": None,
                        "sort": "none",
                    }
                frame, _, _ = engine.execute(query_layer)
            group_names = [group for group, _ in _groups(frame)]
        selection = selections.get(f"layer:{raw_layer['id']}")
        visible = selection is not None
        style = raw_layer.get("style") or {}
        alpha = float(style.get("alpha", 1.0))
        if visible and selection.get("overlay", False):
            alpha *= overlay_alpha
        if not visible:
            alpha = 0.0
        color = str(style.get("color", STD_COLORS[index % len(STD_COLORS)]))
        for group in group_names:
            label = raw_layer["label"] if group is None else f"{raw_layer['label']}: {group}"
            handles.append(_layer_legend_proxy(raw_layer, color, alpha))
            labels.append(label)
            visibility.append(visible)
    for annotation in config.get("_stage_legend_annotations", []):
        visible = f"annotation:{annotation['id']}" in selections
        alpha = float(annotation.get("alpha", 0.6)) if visible else 0.0
        handles.append(_annotation_legend_proxy(annotation, alpha))
        labels.append(str(annotation["legend_label"]))
        visibility.append(visible)
    return handles, labels, visibility


def build_figure(config: dict[str, Any], engine: QueryEngine, registry: Registry):
    figure = config["figure"]
    global_font_size = float(figure["font_size"])
    label_font_size = (
        float(config["axes"]["label_font_size"])
        if config["axes"]["label_font_size_override"]
        else global_font_size
    )
    tick_font_size = (
        float(config["axes"]["tick_font_size"])
        if config["axes"]["tick_font_size_override"]
        else global_font_size
    )
    legend_font_size = (
        float(config["legend"]["font_size"])
        if config["legend"]["font_size_override"]
        else global_font_size
    )
    broken_y = config["broken_y_axis"]
    if broken_y["enabled"]:
        fig, created_axes = plt.subplots(
            nrows=len(broken_y["ranges"]),
            sharex=True,
            figsize=(float(figure["width"]), float(figure["height"])),
            gridspec_kw={"hspace": float(broken_y["gap"])},
        )
        primary_axes = list(created_axes)
        primary = primary_axes[-1]
    else:
        fig, primary = plt.subplots(figsize=(float(figure["width"]), float(figure["height"])))
        primary_axes = [primary]
    secondary = (
        primary.twinx()
        if any(
            layer.get("secondary_y", False)
            for layer in config.get("_stage_legend_layers", [])
        )
        else None
    )
    state: dict[str, Any] = {}
    cache_states: list[dict[str, str]] = []
    layer_frames: dict[str, pl.DataFrame] = {}
    time_axis_x = False
    x_values: list[Any] = []
    time_bins: list[str] = []
    color_index = 0
    enabled_layers = [layer for layer in config["layers"] if layer.get("enabled", True)]
    anchor_raw = next(
        (layer for layer in enabled_layers if layer.get("fix_x_values", False)),
        config.get("_fixed_x_anchor_layer"),
    )
    shared_x_values: list[Any] | None = None
    shared_x_positions: dict[Any, int] | None = None
    prepared_layers: dict[str, tuple[pl.DataFrame, dict[str, Any], str]] = {}
    if anchor_raw is not None:
        anchor_frame, anchor_layer, anchor_cache_state = engine.execute(anchor_raw)
        shared_x_values = list(
            dict.fromkeys(anchor_frame.get_column("_x").drop_nulls().to_list())
        )
        if not shared_x_values:
            raise ConfigurationError(
                f"layer {anchor_layer['label']} cannot fix shared X values because its "
                "query returned no X values"
            )
        shared_x_positions = {
            value: position for position, value in enumerate(shared_x_values)
        }
        if any(layer.get("id") == anchor_layer["id"] for layer in enabled_layers):
            prepared_layers[anchor_layer["id"]] = (
                anchor_frame,
                anchor_layer,
                anchor_cache_state,
            )
    for raw_layer in config["layers"]:
        if not raw_layer.get("enabled", True):
            continue
        prepared = prepared_layers.get(str(raw_layer.get("id")))
        if prepared is not None:
            frame, layer, cache_state = prepared
        else:
            query_layer = raw_layer
            if shared_x_values is not None:
                query_layer = {
                    **raw_layer,
                    "fixed_x_values": shared_x_values,
                    # The anchor alone determines selection and order. A follower's
                    # own result limit or sort must not select a different subset.
                    "result_limit": None,
                    "sort": "none",
                }
            frame, layer, cache_state = engine.execute(query_layer)
        layer_frames[layer["id"]] = frame
        time_axis_x = time_axis_x or (
            shared_x_values is None and layer["time_bin"] is not None
        )
        if layer["plot_type"] not in {"histogram", "box", "violin"}:
            layer_x_values = frame.get_column("_x").drop_nulls().to_list()
            if shared_x_values is None:
                x_values.extend(layer_x_values)
                time_axis_x = time_axis_x or bool(layer_x_values) and isinstance(
                    layer_x_values[0], (date, datetime)
                )
        if layer["time_bin"]:
            time_bins.append(layer["time_bin"])
        grouping = (
            "group_by_dynamic"
            if layer["time_bin"]
            else "group_by"
            if layer["aggregation"] != "none"
            else "none"
        )
        cache_states.append(
            {
                "layer": layer["id"],
                "cache": cache_state,
                "rows": frame.height,
                "grouping": grouping,
                "aggregation": layer["aggregation"],
                "every": layer["time_bin"],
            }
        )
        default_color = STD_COLORS[color_index % len(STD_COLORS)]
        color_index += 1
        target_axes = primary_axes
        if layer["secondary_y"]:
            secondary = secondary or primary.twinx()
            target_axes = [secondary]
        for axis in target_axes:
            for group, group_frame in _groups(frame):
                label = layer["label"] if group is None else f"{layer['label']}: {group}"
                style = {**DEFAULT_STYLE, "color": default_color, **layer["style"]}
                if style.get("marker") == "none":
                    style["marker"] = None
                context = PlotContext(
                    ax=axis,
                    frame=group_frame,
                    layer=layer,
                    x=(
                        [
                            shared_x_positions[value]
                            for value in group_frame.get_column("_x").to_list()
                        ]
                        if shared_x_positions is not None
                        else group_frame.get_column("_x").to_list()
                    ),
                    y=group_frame.get_column("_y").drop_nulls().to_list()
                    if layer["plot_type"] in {"histogram", "box", "violin"}
                    else group_frame.get_column("_y").to_list(),
                    label=label,
                    style=style,
                    state=state,
                )
                registry.plots[layer["plot_type"]](context)
    referenced_layer_ids = {
        layer_id
        for annotation in config["annotations"]
        for layer_id in (annotation.get("inference") or {}).get("layer_ids", [])
    }
    reference_layers = {
        str(layer.get("id")): layer
        for layer in [
            *config["layers"],
            *config.get("_annotation_reference_layers", []),
        ]
    }
    for layer_id in referenced_layer_ids - layer_frames.keys():
        raw_reference = reference_layers.get(layer_id)
        if raw_reference is None:
            raise ConfigurationError(
                f"annotation inference layer is unavailable in this stage: {layer_id}"
            )
        query_reference = raw_reference
        if shared_x_values is not None and (
            anchor_raw is None or raw_reference.get("id") != anchor_raw.get("id")
        ):
            query_reference = {
                **raw_reference,
                "fixed_x_values": shared_x_values,
                "result_limit": None,
                "sort": "none",
            }
        reference_frame, reference_layer, _ = engine.execute(query_reference)
        layer_frames[reference_layer["id"]] = reference_frame
    axes = config["axes"]
    for axis in primary_axes:
        axis.set_xscale(axes["xscale"])
        axis.set_yscale(axes["yscale"])
    if secondary is not None:
        secondary.set_yscale(axes["secondary_yscale"])
    primary.set_xlabel(str(axes["xlabel"]).replace("\\n", "\n"), fontsize=label_font_size)
    if broken_y["enabled"]:
        fig.supylabel(str(axes["ylabel"]).replace("\\n", "\n"), fontsize=label_font_size)
    else:
        primary.set_ylabel(str(axes["ylabel"]).replace("\\n", "\n"), fontsize=label_font_size)
    if secondary is not None:
        secondary.set_ylabel(
            str(axes["secondary_ylabel"]).replace("\\n", "\n"), fontsize=label_font_size
        )
    if shared_x_values is not None:
        primary.set_xlim(-0.5, len(shared_x_values) - 0.5)
    elif axes["xmin"] is None and axes["xmax"] is None:
        _automatic_x_limits(primary, x_values, time_bins)
    xmin = _x_limit_value(axes["xmin"], x_values, shared_x_values)
    xmax = _x_limit_value(axes["xmax"], x_values, shared_x_values)
    if broken_y["enabled"]:
        for axis, value_range in zip(primary_axes, reversed(broken_y["ranges"]), strict=True):
            axis.set_ylim(value_range["min"], value_range["max"])
    else:
        _limits(primary, axes["ymin"], axes["ymax"], "y")
    if secondary is not None:
        _limits(secondary, axes["secondary_ymin"], axes["secondary_ymax"], "y")
    if shared_x_values is not None:
        shared_tick_axes = {
            **axes,
            "custom_x_ticks": [],
            "custom_x_tick_labels": [],
            "x_value_ticks": True,
        }
        _ticks(
            primary,
            shared_tick_axes,
            False,
            [str(value) for value in shared_x_values],
        )
    else:
        _ticks(primary, axes, time_axis_x, x_values)
    # set_xticks may expand Matplotlib's view interval. Explicit bounds are the
    # final authority and therefore must be applied after all X tick locators.
    _limits(primary, xmin, xmax, "x")
    if axes["y_engineering"]:
        for axis in primary_axes:
            axis.yaxis.set_major_formatter(mticker.EngFormatter(sep=""))
    for axis in primary_axes:
        _y_ticks(axis, axes)
        _minor_y_ticks(axis, axes["minor_y_ticks"])
    if secondary is not None and axes["secondary_y_engineering"]:
        secondary.yaxis.set_major_formatter(mticker.EngFormatter(sep=""))
    if secondary is not None:
        _y_ticks(secondary, axes, secondary=True)
        _minor_y_ticks(secondary, axes["secondary_minor_y_ticks"])
    for axis in primary_axes:
        axis.tick_params(axis="both", which="both", labelsize=tick_font_size)
    if secondary is not None:
        secondary.tick_params(axis="y", which="both", labelsize=tick_font_size)
    for axis in primary_axes:
        axis.set_axisbelow(True)
        _grid(axis, bool(axes["x_grid"]), "x", float(axes["grid_alpha"]))
        _grid(axis, bool(axes["y_grid"]), "y", float(axes["grid_alpha"]))
    if secondary is not None:
        secondary.set_axisbelow(True)
        _grid(
            secondary,
            bool(axes["secondary_y_grid"]),
            "y",
            float(axes["grid_alpha"]),
        )
    for annotation in config["annotations"]:
        if not annotation.get("enabled", True):
            continue
        resolved_annotation = _resolve_annotation(
            annotation, layer_frames, shared_x_positions
        )
        annotation_axes = _annotation_axes(primary_axes, resolved_annotation)
        label_axis = _annotation_label_axis(annotation_axes, resolved_annotation)
        for axis in annotation_axes:
            _annotation(
                axis,
                resolved_annotation,
                global_font_size,
                draw_attached_text=axis is label_axis,
            )
        if resolved_annotation["show_in_legend"]:
            _annotation_legend_handle(primary, resolved_annotation)
    if broken_y["enabled"]:
        _broken_axis_marks(primary_axes)
    if config["legend"]["enabled"]:
        legend_visibility: list[bool] | None = None
        if "_stage_legend_layers" in config:
            handles, labels, legend_visibility = _stage_legend_entries(
                config, engine, layer_frames, shared_x_values
            )
        else:
            handles, labels = [], []
            for axis in (primary, secondary):
                if axis is not None:
                    current_handles, current_labels = axis.get_legend_handles_labels()
                    handles.extend(current_handles)
                    labels.extend(current_labels)
        if handles:
            legend_options = {
                "loc": config["legend"]["loc"],
                "ncols": int(config["legend"]["ncols"]),
                "fontsize": legend_font_size,
                "handlelength": float(config["legend"]["handlelength"]),
                "columnspacing": float(config["legend"]["columnspacing"]),
                "handletextpad": float(config["legend"]["handletextpad"]),
                "framealpha": float(config["legend"]["opacity"]),
            }
            if config["legend"]["bbox_enabled"]:
                legend_options["bbox_to_anchor"] = (
                    float(config["legend"]["bbox_x"]),
                    float(config["legend"]["bbox_y"]),
                )
            if legend_visibility is not None:
                created_legend = fig.legend(handles, labels, **legend_options)
                for text, visible in zip(
                    created_legend.get_texts(), legend_visibility, strict=True
                ):
                    text.set_alpha(1.0 if visible else 0.0)
            else:
                legend_axis = primary_axes[0] if broken_y["enabled"] else primary
                created_legend = legend_axis.legend(handles, labels, **legend_options)
    if figure["font_family"] == "monospace":
        for text in fig.findobj(match=plt.Text):
            text.set_fontfamily("monospace")
    if broken_y["enabled"]:
        fig.subplots_adjust(hspace=float(broken_y["gap"]))
    else:
        fig.tight_layout()
    return fig, cache_states


def _grid(ax, enabled: bool, axis: str, alpha: float) -> None:
    if enabled:
        ax.grid(True, axis=axis, which="major", alpha=alpha)
    else:
        ax.grid(False, axis=axis, which="major")


def _broken_axis_marks(axes: list[Any]) -> None:
    size = 0.012
    style = {"color": "black", "clip_on": False, "linewidth": 0.8}
    for upper, lower in zip(axes, axes[1:]):
        upper.spines["bottom"].set_visible(False)
        lower.spines["top"].set_visible(False)
        upper.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        for x in (0, 1):
            upper.plot(
                (x - size, x + size),
                (-size, size),
                transform=upper.transAxes,
                **style,
            )
            lower.plot(
                (x - size, x + size),
                (1 - size, 1 + size),
                transform=lower.transAxes,
                **style,
            )


def _limits(ax, lower: Any, upper: Any, axis: str) -> None:
    if lower is None and upper is None:
        return
    current_lower, current_upper = getattr(ax, f"get_{axis}lim")()
    getattr(ax, f"set_{axis}lim")(
        current_lower if lower is None else lower,
        current_upper if upper is None else upper,
    )


def _x_limit_value(
    value: Any,
    x_values: list[Any],
    shared_x_values: list[Any] | None = None,
) -> Any:
    """Resolve a configured X bound into the coordinate system used by the plot."""
    if value is None or isinstance(value, (date, datetime)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ConfigurationError("X limit must be finite")
        return float(value)
    domain = shared_x_values if shared_x_values is not None else x_values
    sample = next((item for item in domain if item is not None), None)
    if shared_x_values is not None:
        text = str(value)
        for position, item in enumerate(shared_x_values):
            if item == value or str(item) == text:
                return float(position)
        raise ConfigurationError(f"X limit {value!r} is not in the fixed X-value domain")
    if isinstance(sample, datetime):
        if not isinstance(value, str):
            raise ConfigurationError("date/time X limits must use an ISO date or timestamp")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ConfigurationError(f"invalid date/time X limit: {value!r}") from error
    if isinstance(sample, date):
        if not isinstance(value, str):
            raise ConfigurationError("date X limits must use an ISO date")
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ConfigurationError(f"invalid date X limit: {value!r}") from error
    if isinstance(sample, str):
        text = str(value)
        values = list(dict.fromkeys(str(item) for item in domain if item is not None))
        if text not in values:
            raise ConfigurationError(f"X limit {value!r} is not present in the X values")
        return float(values.index(text))
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"X limit must be numeric: {value!r}") from error
    if not math.isfinite(result):
        raise ConfigurationError("X limit must be finite")
    return result


def _automatic_x_limits(ax, values: list[Any], time_bins: list[str]) -> None:
    """Use the observed X range and avoid Matplotlib's years-wide single-date fallback."""
    if not values:
        return
    first = values[0]
    try:
        if isinstance(first, (date, datetime)):
            coordinates = [float(mdates.date2num(value)) for value in values]
            padding = _smallest_time_bin_days(time_bins) / 2
        elif isinstance(first, (int, float)) and not isinstance(first, bool):
            coordinates = [float(value) for value in values]
            padding = max(abs(coordinates[0]) * 0.05, 0.5)
        else:
            return
    except (TypeError, ValueError):
        return
    lower, upper = min(coordinates), max(coordinates)
    if lower == upper:
        lower -= padding
        upper += padding
    ax.set_xlim(lower, upper)


def _smallest_time_bin_days(time_bins: list[str]) -> float:
    seconds_per_unit = {
        "ns": 1e-9,
        "us": 1e-6,
        "ms": 1e-3,
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 7 * 86400,
        "mo": 30 * 86400,
        "q": 91 * 86400,
        "y": 365 * 86400,
    }
    durations = []
    for value in time_bins:
        match = re.fullmatch(r"([1-9]\d*)(ns|us|ms|s|m|h|d|w|mo|q|y)", value)
        if match:
            durations.append(int(match.group(1)) * seconds_per_unit[match.group(2)] / 86400)
    return min(durations, default=1 / 1440)


def _ticks(
    ax,
    axes: dict[str, Any],
    time_binned: bool = False,
    x_values: list[Any] | None = None,
) -> None:
    if not axes["major_x_ticks"]:
        ax.xaxis.set_major_locator(mticker.NullLocator())
        ax.xaxis.set_minor_locator(mticker.NullLocator())
        return
    ticks = axes["custom_x_ticks"]
    if ticks:
        labels = axes["custom_x_tick_labels"] or [str(value) for value in ticks]
        ax.set_xticks(ticks, labels)
    elif axes.get("x_value_ticks", False) and x_values:
        values = list(dict.fromkeys(x_values))
        interval = int(axes.get("x_value_tick_interval", 1))
        selected_indices = list(range(0, len(values), interval))
        selected_values = [values[index] for index in selected_indices]
        if values and isinstance(values[0], str):
            ax.set_xticks(selected_indices, [str(value) for value in selected_values])
        else:
            ax.set_xticks(selected_values, [str(value) for value in selected_values])
    elif time_binned:
        locator = mdates.AutoDateLocator(minticks=3, maxticks=10)
        ax.xaxis.set_major_locator(locator)
        datetime_format = axes.get("x_datetime_format", "")
        ax.xaxis.set_major_formatter(
            mdates.DateFormatter(datetime_format)
            if datetime_format
            else mdates.ConciseDateFormatter(locator)
        )
    elif axes["x_engineering"]:
        ax.xaxis.set_major_formatter(mticker.EngFormatter(sep=""))
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


def _y_ticks(ax, axes: dict[str, Any], secondary: bool = False) -> None:
    prefix = "secondary_" if secondary else ""
    tick_key = "custom_secondary_y_ticks" if secondary else "custom_y_ticks"
    label_key = (
        "custom_secondary_y_tick_labels" if secondary else "custom_y_tick_labels"
    )
    ticks = axes.get(tick_key, [])
    labels = axes.get(label_key, [])
    if not ticks and axes.get(f"{prefix}y_tick_min") is not None:
        lower = float(axes[f"{prefix}y_tick_min"])
        upper = float(axes[f"{prefix}y_tick_max"])
        step = float(axes[f"{prefix}y_tick_step"])
        count = int((upper - lower) / step + 1e-12) + 1
        ticks = [lower + index * step for index in range(count)]
    if not ticks:
        return
    ax.yaxis.set_major_locator(mticker.FixedLocator(ticks))
    if labels:
        ax.yaxis.set_major_formatter(mticker.FixedFormatter(labels))


def _minor_y_ticks(ax, enabled: bool) -> None:
    if enabled:
        # Axis.minorticks_on selects a locator appropriate for the active scale
        # (linear, log, symlog, or logit).
        ax.yaxis.minorticks_on()
        ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    else:
        ax.yaxis.set_minor_locator(mticker.NullLocator())


def _resolve_annotation(
    item: dict[str, Any],
    layer_frames: dict[str, pl.DataFrame],
    shared_x_positions: dict[Any, int] | None = None,
) -> dict[str, Any]:
    resolved = dict(item)
    inference = item.get("inference") or {}
    layer_ids = inference.get("layer_ids", [])
    frames = [layer_frames[layer_id] for layer_id in layer_ids if layer_id in layer_frames]
    x_values = [
        value
        for frame in frames
        for value in frame.get_column("_x").to_list()
        if value is not None
    ]
    pairs = [
        (x, y)
        for frame in frames
        for x, y in frame.select("_x", "_y").iter_rows()
        if x is not None and y is not None
    ]
    x_mode = inference.get("x", "manual")
    y_mode = inference.get("y", "manual")
    try:
        if x_mode == "min_x":
            resolved["x"] = min(x_values)
        elif x_mode == "max_x":
            resolved["x"] = max(x_values)
        elif x_mode == "x_at_min_y":
            resolved["x"] = min(pairs, key=lambda point: point[1])[0]
        elif x_mode == "x_at_max_y":
            resolved["x"] = max(pairs, key=lambda point: point[1])[0]
        if y_mode == "min_y":
            resolved["y"] = min(y for _, y in pairs)
        elif y_mode == "max_y":
            resolved["y"] = max(y for _, y in pairs)
        elif y_mode == "y_at_min_x":
            resolved["y"] = min(pairs, key=lambda point: point[0])[1]
        elif y_mode == "y_at_max_x":
            resolved["y"] = max(pairs, key=lambda point: point[0])[1]
    except (TypeError, ValueError) as error:
        raise ConfigurationError(
            f"annotation {item['id']} could not infer coordinates from the selected layers"
        ) from error
    if x_mode != "manual" and "x" not in resolved:
        raise ConfigurationError(
            f"annotation {item['id']} cannot infer X because the selected layers have no values"
        )
    if y_mode != "manual" and "y" not in resolved:
        raise ConfigurationError(
            f"annotation {item['id']} cannot infer Y because the selected layers have no values"
        )
    if shared_x_positions is not None:
        for field in ("x", "x1", "x2", "text_x"):
            value = _annotation_x(resolved.get(field))
            if value in shared_x_positions:
                resolved[field] = shared_x_positions[value]
    return resolved


def _annotation_axes(primary_axes: list[Any], item: dict[str, Any]) -> list[Any]:
    if len(primary_axes) == 1 or item["kind"] in {"vline", "vspan"}:
        return primary_axes
    if item["kind"] in {"text", "hline"}:
        values = [item.get("y")]
    elif item["kind"] == "hspan":
        values = [item.get("y1"), item.get("y2")]
    else:
        return primary_axes
    try:
        lower, upper = sorted(float(value) for value in values)
    except (TypeError, ValueError):
        return [primary_axes[-1]]
    matches = []
    for axis in primary_axes:
        axis_lower, axis_upper = sorted(axis.get_ylim())
        if lower <= axis_upper and upper >= axis_lower:
            matches.append(axis)
    return matches or [primary_axes[-1]]


def _annotation_label_axis(annotation_axes: list[Any], item: dict[str, Any]) -> Any:
    if item.get("text_y") is None:
        return annotation_axes[0]
    try:
        y = float(item["text_y"])
    except (TypeError, ValueError):
        return annotation_axes[0]
    for axis in annotation_axes:
        lower, upper = sorted(axis.get_ylim())
        if lower <= y <= upper:
            return axis
    return annotation_axes[0]


def _annotation(
    ax,
    item: dict[str, Any],
    default_font_size: float = 10.0,
    draw_attached_text: bool = True,
) -> None:
    kind = item.get("kind")
    color = item.get("color", "#666666")
    text_color = item.get("text_color", color)
    alpha = float(item.get("alpha", 0.6))
    text = str(item.get("text", "")).replace("\\n", "\n")
    fontsize = float(item.get("fontsize", default_font_size))
    line_options = {
        "color": color,
        "alpha": alpha,
        "linestyle": item.get("linestyle", "--"),
        "linewidth": float(item.get("linewidth", 1.0)),
        "label": "_nolegend_",
    }
    if kind == "vline":
        ax.axvline(_annotation_x(item["x"]), **line_options)
    elif kind == "hline":
        ax.axhline(item["y"], **line_options)
    elif kind == "vspan":
        ax.axvspan(
            _annotation_x(item["x1"]),
            _annotation_x(item["x2"]),
            color=color,
            alpha=alpha,
            label="_nolegend_",
        )
    elif kind == "hspan":
        ax.axhspan(
            item["y1"], item["y2"], color=color, alpha=alpha, label="_nolegend_"
        )
    elif kind == "text":
        _foreground_text(
            ax,
            _annotation_x(item["x"]),
            item["y"],
            text,
            color=text_color,
            alpha=alpha,
            fontsize=float(item.get("fontsize", default_font_size)),
            bbox=_annotation_bbox(item),
        )
    else:
        raise ConfigurationError(f"unsupported annotation kind: {kind}")
    if text and kind != "text" and draw_attached_text:
        _annotation_label(ax, item, text, text_color, alpha, fontsize)


def _annotation_legend_handle(ax, item: dict[str, Any]) -> None:
    kind = item["kind"]
    color = item.get("color", "#666666")
    alpha = float(item.get("alpha", 0.6))
    label = item["legend_label"]
    if kind in {"vspan", "hspan"}:
        handle = mpl.patches.Rectangle(
            (0, 0), 0, 0, facecolor=color, alpha=alpha, label=label
        )
        ax.add_artist(handle)
        return
    handle = mpl.lines.Line2D(
        [],
        [],
        color=item.get("text_color", color) if kind == "text" else color,
        alpha=alpha,
        linestyle="none" if kind == "text" else item.get("linestyle", "--"),
        linewidth=float(item.get("linewidth", 1.0)),
        marker="o" if kind == "text" else None,
        markersize=4,
        label=label,
    )
    ax.add_line(handle)


def _annotation_bbox(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("text_background_enabled", False):
        return None
    return {
        "facecolor": item.get("text_background_color", "#ffffff"),
        "edgecolor": "none",
        "alpha": float(item.get("alpha", 0.6)),
        "pad": 2.0,
    }


def _foreground_text(
    ax,
    x: Any,
    y: Any,
    text: str,
    transform: Any | None = None,
    x_data: bool = True,
    y_data: bool = True,
    **options: Any,
) -> Any:
    """Draw text above every subplot while retaining the selected axis coordinates."""
    return ax.figure.text(
        ax.convert_xunits(x) if x_data else x,
        ax.convert_yunits(y) if y_data else y,
        text,
        transform=transform if transform is not None else ax.transData,
        clip_on=False,
        zorder=1000,
        **options,
    )


def _annotation_x(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


def _annotation_label(
    ax, item: dict[str, Any], text: str, color: str, alpha: float, fontsize: float
) -> None:
    kind = item["kind"]
    if item.get("text_y") is not None:
        _foreground_text(
            ax,
            _annotation_x(item.get("text_x", item.get("x", item.get("x1")))),
            item["text_y"],
            text,
            color=color,
            alpha=alpha,
            fontsize=fontsize,
            horizontalalignment=item.get("text_horizontal_alignment", "left"),
            verticalalignment=item.get("text_vertical_alignment", "center"),
            bbox=_annotation_bbox(item),
        )
        return
    if kind in {"vline", "vspan"}:
        x = item["x"] if kind == "vline" else item["x1"]
        _foreground_text(
            ax,
            _annotation_x(x),
            0.98,
            text,
            transform=ax.get_xaxis_transform(),
            y_data=False,
            color=color,
            alpha=alpha,
            fontsize=fontsize,
            rotation=90,
            horizontalalignment="right",
            verticalalignment="top",
            bbox=_annotation_bbox(item),
        )
    else:
        y = item["y"] if kind == "hline" else item["y2"]
        _foreground_text(
            ax,
            0.99,
            y,
            text,
            transform=ax.get_yaxis_transform(),
            x_data=False,
            color=color,
            alpha=alpha,
            fontsize=fontsize,
            horizontalalignment="right",
            verticalalignment="bottom",
            bbox=_annotation_bbox(item),
        )


def render_artifacts(
    config: dict[str, Any],
    engine: QueryEngine,
    registry: Registry,
    formats: list[str] | None = None,
) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    formats = formats or config["export_formats"]
    artifacts: dict[str, bytes] = {}
    cache_states: list[dict[str, str]] = []
    if "json" in formats:
        artifacts[f"{config['filename']}.json"] = (json.dumps(config, indent=2) + "\n").encode()
    plot_formats = [item for item in formats if item != "json"]
    if not plot_formats:
        return artifacts, cache_states
    for suffix, stage_label, stage_config in _stage_configs(config):
        fig, current_states = build_figure(stage_config, engine, registry)
        cache_states.extend(
            {**item, **({"stage": stage_label} if stage_label else {})}
            for item in current_states
        )
        try:
            for output_format in plot_formats:
                buffer = BytesIO()
                fig.savefig(
                    buffer,
                    format=output_format,
                    dpi=int(config["figure"]["dpi"]),
                    bbox_inches="tight",
                )
                filename = f"{config['filename']}{suffix}.{output_format}"
                artifacts[filename] = buffer.getvalue()
        finally:
            plt.close(fig)
    return artifacts, cache_states


def _stage_configs(config: dict[str, Any]) -> list[tuple[str, str | None, dict[str, Any]]]:
    stages = config["stages"]
    if not stages["enabled"]:
        return [("", None, config)]
    result = []
    overlay_alpha = float(stages["overlay_alpha"])
    fixed_x_anchor = next(
        (
            layer
            for layer in config["layers"]
            if layer.get("enabled", True) and layer.get("fix_x_values", False)
        ),
        None,
    )
    staged_element_ids = {
        element["id"]
        for step in stages["steps"]
        for element in step["elements"]
    }
    for index, step in enumerate(stages["steps"]):
        selected = {item["id"]: item for item in step["elements"]}
        stage = deepcopy(config)
        stage["stages"]["enabled"] = False
        stage["_stage_legend_layers"] = []
        legend_color_index = 0
        for layer in config["layers"]:
            if not layer.get("enabled", True):
                continue
            default_color = STD_COLORS[legend_color_index % len(STD_COLORS)]
            legend_color_index += 1
            if f"layer:{layer['id']}" not in staged_element_ids:
                continue
            legend_layer = deepcopy(layer)
            legend_layer.setdefault("style", {})
            legend_layer["style"].setdefault("color", default_color)
            stage["_stage_legend_layers"].append(legend_layer)
        stage["_stage_legend_annotations"] = [
            deepcopy(annotation)
            for annotation in config["annotations"]
            if annotation.get("enabled", True) and annotation.get("show_in_legend", False)
            and f"annotation:{annotation['id']}" in staged_element_ids
        ]
        stage["_stage_legend_elements"] = deepcopy(selected)
        if stage["legend"]["loc"] == "best":
            stage["legend"]["loc"] = "upper right"
        stage["layers"] = []
        stage_color_index = 0
        for layer in config["layers"]:
            default_color = STD_COLORS[stage_color_index % len(STD_COLORS)]
            if layer.get("enabled", True):
                stage_color_index += 1
            selection = selected.get(f"layer:{layer['id']}")
            if selection is None:
                continue
            current = deepcopy(layer)
            current.setdefault("style", {})
            current["style"].setdefault("color", default_color)
            if selection["overlay"]:
                current["style"]["alpha"] = (
                    float(current["style"].get("alpha", 1.0)) * overlay_alpha
                )
            stage["layers"].append(current)
        if fixed_x_anchor is not None and not any(
            layer.get("fix_x_values", False) for layer in stage["layers"]
        ):
            # A stage may show only a follower, but it still needs the original
            # anchor query to establish the same filtered, ordered domain.
            stage["_fixed_x_anchor_layer"] = deepcopy(fixed_x_anchor)
        stage["annotations"] = [
            deepcopy(annotation)
            for annotation in config["annotations"]
            if f"annotation:{annotation['id']}" in selected
        ]
        annotation_reference_ids = {
            layer_id
            for annotation in stage["annotations"]
            for layer_id in (annotation.get("inference") or {}).get("layer_ids", [])
        }
        visible_layer_ids = {layer["id"] for layer in stage["layers"]}
        stage["_annotation_reference_layers"] = [
            deepcopy(layer)
            for layer in config["layers"]
            if layer["id"] in annotation_reference_ids
            and layer["id"] not in visible_layer_ids
        ]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", step["label"]).strip("-_") or f"stage-{index + 1}"
        result.append((f"-{index + 1:02d}-{slug}", step["label"], stage))
    return result


def preview(artifacts: dict[str, bytes]) -> list[dict[str, str]]:
    return [
        {"filename": name, "image": "data:image/png;base64," + base64.b64encode(content).decode()}
        for name, content in artifacts.items()
        if Path(name).suffix == ".png"
    ]
