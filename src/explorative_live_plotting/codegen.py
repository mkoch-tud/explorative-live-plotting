"""Generate readable standalone Python scripts for built-in plots."""

from __future__ import annotations

from datetime import date, datetime
import math
from pprint import pformat
from typing import Any

import polars as pl

from .data import DataCatalog, MODULE_NAME, PATH_VARIABLE
from .errors import ConfigurationError
from .query import validate_layer
from .registry import BUILTIN_AGGREGATIONS, BUILTIN_PLOTS, Registry


def _coerce(value: Any, dtype: pl.DataType, field: str) -> Any:
    try:
        if dtype.is_integer():
            return int(value)
        if dtype.is_float() or dtype == pl.Decimal:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
            return number
        if dtype == pl.Boolean:
            if isinstance(value, bool):
                return value
            lowered = str(value).lower()
            if lowered not in {"true", "false", "1", "0"}:
                raise ValueError
            return lowered in {"true", "1"}
        if dtype == pl.Date:
            return date.fromisoformat(str(value))
        if dtype == pl.Datetime:
            return datetime.fromisoformat(str(value))
        return str(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} is incompatible with {dtype}: {value}") from error


def _filter_code(item: dict[str, Any], schema: pl.Schema) -> str:
    column = item["column"]
    operator = item.get("operator", "eq")
    expression = f"pl.col({column!r})"
    if operator == "is_null":
        return f"{expression}.is_null()"
    if operator == "is_not_null":
        return f"{expression}.is_not_null()"
    raw = item.get("value")
    if operator in {"in", "not_in"}:
        values = raw if isinstance(raw, list) else [part.strip() for part in str(raw).split(",")]
        converted = [_coerce(value, schema[column], f"filter {column}") for value in values]
        code = f"{expression}.is_in({converted!r})"
        return f"~({code})" if operator == "not_in" else code
    value = _coerce(raw, schema[column], f"filter {column}")
    symbol = {"eq": "==", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}[
        operator
    ]
    return f"{expression} {symbol} {value!r}"


def _aggregation_code(layer: dict[str, Any]) -> str:
    aggregation = layer["aggregation"]
    if aggregation == "count":
        return "pl.len()"
    column = f"pl.col({layer['y_column']!r})"
    if aggregation == "quantile":
        quantile = float(layer["aggregation_options"].get("quantile", 0.5))
        return f"{column}.quantile({quantile!r})"
    return f"{column}.{aggregation}()"


def _query_code(index: int, source_variable: str, layer: dict[str, Any], schema: pl.Schema) -> str:
    variable = f"layer_{index}"
    comment = str(layer["label"]).replace("\n", " ").replace("\r", " ")
    lines = [f"    # {comment}", f"    {variable} = {source_variable}"]
    filters = [_filter_code(item, schema) for item in layer["filters"]]
    if filters:
        combiner = "all_horizontal" if layer["filter_logic"] == "and" else "any_horizontal"
        lines.append(
            f"    {variable} = {variable}.filter(pl.{combiner}([{', '.join(filters)}]))"
        )
    x_column = layer["x_column"]
    y_column = layer["y_column"]
    group_column = layer["group_column"]
    if layer["aggregation"] == "none":
        selections = []
        if x_column:
            selections.append(f"pl.col({x_column!r}).alias('_x')")
        if y_column:
            selections.append(f"pl.col({y_column!r}).alias('_y')")
        if group_column:
            selections.append(f"pl.col({group_column!r}).cast(pl.String).alias('_group')")
        lines.append(f"    {variable} = {variable}.select([{', '.join(selections)}])")
        if x_column is None:
            lines.append(f"    {variable} = {variable}.with_row_index('_x', offset=1)")
    else:
        groups = [column for column in (x_column, group_column) if column]
        aggregation = _aggregation_code(layer) + ".alias('_y')"
        if groups:
            lines.append(f"    {variable} = {variable}.group_by({groups!r}).agg({aggregation})")
            selections = [f"pl.col({x_column!r}).alias('_x')"]
            if group_column:
                selections.append(f"pl.col({group_column!r}).cast(pl.String).alias('_group')")
            selections.append("pl.col('_y')")
            lines.append(f"    {variable} = {variable}.select([{', '.join(selections)}])")
        else:
            lines.append(
                f"    {variable} = {variable}.select({aggregation}).with_row_index('_x', offset=1)"
            )
    if layer["sort"] != "none":
        column, descending = layer["sort"].split("_")
        lines.append(
            f"    {variable} = {variable}.sort('_{column}', "
            f"descending={descending == 'descending'!r}, nulls_last=True)"
        )
    if layer["limit"] is not None:
        lines.append(f"    {variable} = {variable}.limit({layer['limit']!r})")
    lines.append(f"    {variable} = {variable}.collect(engine='streaming')")
    return "\n".join(lines)


def _draw_code(index: int, layer: dict[str, Any]) -> str:
    plot_type = layer["plot_type"]
    histogram_like = plot_type in {"histogram", "box", "violin"}
    lines = [
        f"def _draw_layer_{index}(ax, frame, state):",
        "    for group, group_frame in _groups(frame):",
        f"        label = {layer['label']!r} if group is None else "
        f"{layer['label']!r} + ': ' + str(group)",
        f"        style = {{**DEFAULT_STYLE, **{layer['style']!r}}}",
        "        if style.get('marker') == 'none':",
        "            style['marker'] = None",
        "        x = group_frame.get_column('_x').to_list()",
        (
            "        y = group_frame.get_column('_y').drop_nulls().to_list()"
            if histogram_like
            else "        y = group_frame.get_column('_y').to_list()"
        ),
        "        plot_style = _plot_style(style)",
    ]
    if plot_type == "line":
        lines.append("        ax.plot(x, y, label=label, **plot_style)")
    elif plot_type == "step":
        lines.append(
            "        ax.step(x, y, label=label, "
            "where=style.get('where', 'post'), **plot_style)"
        )
    elif plot_type == "scatter":
        lines.extend(
            [
                "        if 'linewidth' in plot_style:",
                "            plot_style['linewidths'] = plot_style.pop('linewidth')",
                "        if 'markersize' in plot_style:",
                "            plot_style['s'] = float(plot_style.pop('markersize')) ** 2",
                "        ax.scatter(x, y, label=label, **plot_style)",
            ]
        )
    elif plot_type == "bar":
        lines.extend(
            [
                "        plot_style.pop('marker', None)",
                "        plot_style.pop('markersize', None)",
                "        bottom = None",
                f"        if {layer['stacked']!r}:",
                "            key = (id(ax), 'bar', tuple(x))",
                "            bottom = state.setdefault(key, [0.0] * len(y))",
                "        ax.bar(x, y, bottom=bottom, label=label, **plot_style)",
                "        if bottom is not None:",
                "            state[key] = [old + float(value or 0) for old, value in "
                "zip(bottom, y, strict=True)]",
            ]
        )
    elif plot_type == "area":
        lines.extend(
            [
                "        plot_style.pop('marker', None)",
                "        plot_style.pop('markersize', None)",
                "        ax.fill_between(x, y, label=label, **plot_style)",
            ]
        )
    elif plot_type == "histogram":
        lines.extend(
            [
                "        plot_style.pop('marker', None)",
                "        plot_style.pop('markersize', None)",
                f"        ax.hist(y, bins={int(layer['options'].get('bins', 30))!r}, "
                f"density={bool(layer['options'].get('density', False))!r}, "
                "label=label, **plot_style)",
            ]
        )
    elif plot_type == "box":
        lines.append("        ax.boxplot(y, positions=[1], tick_labels=[label])")
    elif plot_type == "violin":
        showmeans = bool(layer["options"].get("showmeans", False))
        lines.append(f"        ax.violinplot(y, positions=[1], showmeans={showmeans!r})")
    elif plot_type == "stem":
        lines.append("        ax.stem(x, y, label=label)")
    elif plot_type == "hexbin":
        gridsize = int(layer["options"].get("gridsize", 30))
        lines.append(f"        ax.hexbin(x, y, gridsize={gridsize!r}, mincnt=1)")
    return "\n".join(lines)


def _annotation_code(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    color = item.get("color", "#666666")
    alpha = float(item.get("alpha", 0.6))
    if kind == "vline":
        return (
            f"    primary.axvline({item['x']!r}, color={color!r}, alpha={alpha!r}, "
            f"linestyle={item.get('linestyle', '--')!r})"
        )
    if kind == "hline":
        return (
            f"    primary.axhline({item['y']!r}, color={color!r}, alpha={alpha!r}, "
            f"linestyle={item.get('linestyle', '--')!r})"
        )
    if kind == "vspan":
        return (
            f"    primary.axvspan({item['x1']!r}, {item['x2']!r}, "
            f"color={color!r}, alpha={alpha!r})"
        )
    if kind == "hspan":
        return (
            f"    primary.axhspan({item['y1']!r}, {item['y2']!r}, "
            f"color={color!r}, alpha={alpha!r})"
        )
    if kind == "text":
        text = str(item.get("text", "")).replace("\\n", "\n")
        return f"    primary.text({item['x']!r}, {item['y']!r}, {text!r})"
    raise ConfigurationError(f"unsupported annotation kind: {kind}")


HELPERS = '''
DEFAULT_STYLE = {
    "color": "#0072b2",
    "alpha": 1.0,
    "linewidth": 1.5,
    "marker": "none",
    "markersize": 4,
}


def _groups(frame):
    if "_group" not in frame.columns:
        return [(None, frame)]
    values = frame.get_column("_group").drop_nulls().unique(maintain_order=True).to_list()
    return [(str(value), frame.filter(pl.col("_group") == value)) for value in values]


def _plot_style(style):
    allowed = {"color", "alpha", "linewidth", "linestyle", "marker", "markersize", "zorder"}
    return {key: value for key, value in style.items() if key in allowed}


def _limits(ax, lower, upper, axis):
    if (lower is None) != (upper is None):
        raise ValueError(f"both {axis} limits must be supplied")
    if lower is not None:
        getattr(ax, f"set_{axis}lim")(lower, upper)


def _ticks(ax, axes):
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
'''.strip()


def generate_script(config: dict[str, Any], catalog: DataCatalog, registry: Registry) -> str:
    """Return a standalone script for a validated built-in plot configuration."""
    plot_formats = [item for item in config["export_formats"] if item in {"png", "pdf"}]
    if not plot_formats:
        raise ConfigurationError("select PNG and/or PDF before exporting code")

    enabled_layers = []
    for raw_layer in config["layers"]:
        if not raw_layer.get("enabled", True):
            continue
        layer = validate_layer(raw_layer, catalog, registry)
        if (
            layer["plot_type"] not in BUILTIN_PLOTS
            or layer["aggregation"] not in BUILTIN_AGGREGATIONS
        ):
            raise ConfigurationError(
                f"layer {layer['label']} uses a custom plugin and cannot be exported standalone"
            )
        enabled_layers.append(layer)

    if not isinstance(config["sources"], list) or any(
        not isinstance(item, dict) for item in config["sources"]
    ):
        raise ConfigurationError("sources must be an array of objects")
    source_specs = {item.get("name"): item for item in config["sources"]}
    used_sources: list[str] = []
    for layer in enabled_layers:
        if layer["source"] not in used_sources:
            used_sources.append(layer["source"])
    for name in used_sources:
        if name not in source_specs:
            raise ConfigurationError(f"configuration is missing source definition: {name}")

    module_name = config.get("config_module")
    variables = sorted(
        {
            variable
            for name in used_sources
            for variable in PATH_VARIABLE.findall(str(source_specs[name]["path"]))
        }
    )
    if variables and not module_name:
        raise ConfigurationError("portable source paths require a config module")
    if module_name and MODULE_NAME.fullmatch(module_name) is None:
        raise ConfigurationError(f"invalid config module name: {module_name}")

    imports = [
        "from __future__ import annotations",
        "",
        "import datetime",
        "import os",
        "import sys",
        "import matplotlib as mpl",
        'mpl.use("Agg")',
        'mpl.rcParams["pdf.fonttype"] = 42',
        'mpl.rcParams["ps.fonttype"] = 42',
        "import matplotlib.pyplot as plt",
        "import matplotlib.ticker as mticker",
        "import polars as pl",
    ]
    if variables:
        imports.extend(
            [
                "",
                "if os.getcwd() not in sys.path:",
                "    sys.path.insert(0, os.getcwd())",
                f"from {module_name} import {', '.join(variables)}",
            ]
        )

    source_variables: dict[str, str] = {}
    source_lines = []
    for index, name in enumerate(used_sources):
        item = source_specs[name]
        raw_path = str(item["path"])
        path_expression = repr(raw_path)
        for variable in sorted(set(PATH_VARIABLE.findall(raw_path))):
            path_expression += f".replace('${{{variable}}}', os.fspath({variable}))"
        path_variable = f"SOURCE_{index}_PATH"
        lazy_variable = f"source_{index}"
        source_variables[name] = lazy_variable
        source_lines.append(f"    {path_variable} = os.path.expanduser({path_expression})")
        metadata = catalog.metadata(name)
        scanner = {
            "csv": "scan_csv",
            "parquet": "scan_parquet",
            "ndjson": "scan_ndjson",
            "ipc": "scan_ipc",
        }[metadata["format"]]
        options = dict(item.get("options") or {})
        source_lines.append(
            f"    {lazy_variable} = pl.{scanner}({path_variable}, **{options!r})"
        )

    draw_functions = [_draw_code(index, layer) for index, layer in enumerate(enabled_layers)]
    query_blocks = []
    for index, layer in enumerate(enabled_layers):
        schema = catalog.schema(layer["source"])
        query_blocks.append(_query_code(index, source_variables[layer["source"]], layer, schema))

    figure = config["figure"]
    axes = config["axes"]
    main_lines = [
        "def main():",
        *source_lines,
        "",
        *[line for block in query_blocks for line in (block, "")],
        f"    fig, primary = plt.subplots(figsize="
        f"({float(figure['width'])!r}, {float(figure['height'])!r}))",
        "    secondary = None",
        "    state = {}",
    ]
    for index, layer in enumerate(enabled_layers):
        if layer["secondary_y"]:
            main_lines.extend(
                [
                    "    if secondary is None:",
                    "        secondary = primary.twinx()",
                    f"    _draw_layer_{index}(secondary, layer_{index}, state)",
                ]
            )
        else:
            main_lines.append(f"    _draw_layer_{index}(primary, layer_{index}, state)")
    main_lines.extend(
        [
            f"    axes = {axes!r}",
            "    primary.set_xscale(axes['xscale'])",
            "    primary.set_yscale(axes['yscale'])",
            "    if secondary is not None:",
            "        secondary.set_yscale(axes['secondary_yscale'])",
            "    primary.set_xlabel(str(axes['xlabel']).replace('\\\\n', '\\n'))",
            "    primary.set_ylabel(str(axes['ylabel']).replace('\\\\n', '\\n'))",
            "    if secondary is not None:",
            "        secondary.set_ylabel(str(axes['secondary_ylabel']).replace('\\\\n', '\\n'))",
            "    _limits(primary, axes['xmin'], axes['xmax'], 'x')",
            "    _limits(primary, axes['ymin'], axes['ymax'], 'y')",
            "    if secondary is not None:",
            "        _limits(secondary, axes['secondary_ymin'], axes['secondary_ymax'], 'y')",
            "    _ticks(primary, axes)",
            "    primary.grid(bool(axes['x_grid']), axis='x', which='major')",
            "    primary.grid(bool(axes['y_grid']), axis='y', which='major')",
            "    if secondary is not None:",
            "        secondary.grid(bool(axes['secondary_y_grid']), axis='y', which='major')",
        ]
    )
    main_lines.extend(_annotation_code(item) for item in config["annotations"])
    if config["legend"]["enabled"]:
        main_lines.extend(
            [
                "    handles, labels = [], []",
                "    for axis in (primary, secondary):",
                "        if axis is not None:",
                "            current_handles, current_labels = axis.get_legend_handles_labels()",
                "            handles.extend(current_handles)",
                "            labels.extend(current_labels)",
                "    if handles:",
                f"        primary.legend(handles, labels, loc={config['legend']['loc']!r}, "
                f"ncols={int(config['legend']['ncols'])!r})",
            ]
        )
    if figure["font_family"] == "monospace":
        main_lines.extend(
            [
                "    for text in fig.findobj(match=plt.Text):",
                "        text.set_fontfamily('monospace')",
            ]
        )
    main_lines.extend(
        [
            "    fig.tight_layout()",
            "    try:",
        ]
    )
    for output_format in plot_formats:
        output = f"{config['filename']}.{output_format}"
        main_lines.extend(
            [
                f"        fig.savefig({output!r}, format={output_format!r}, "
                f"dpi={int(figure['dpi'])!r}, bbox_inches='tight')",
                f"        print('Saved {output}')",
            ]
        )
    main_lines.extend(
        [
            "    finally:",
            "        plt.close(fig)",
            "",
            "",
            "if __name__ == '__main__':",
            "    main()",
        ]
    )

    embedded_config = "CONFIG = " + pformat(config, sort_dicts=False, width=100)
    sections = [
        "\n".join(imports),
        "# Complete Explorative Live Plotting configuration.\n" + embedded_config,
        HELPERS,
        "\n\n".join(draw_functions),
        "\n".join(main_lines),
    ]
    script = "\n\n\n".join(section for section in sections if section) + "\n"
    try:
        compile(script, f"{config['filename']}.py", "exec")
    except SyntaxError as error:
        raise ConfigurationError(f"generated Python is invalid: {error}") from error
    return script
