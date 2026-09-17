"""Generate readable standalone Python scripts for built-in plots."""

from __future__ import annotations

from datetime import date, datetime
import math
from pprint import pformat
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .data import MODULE_NAME, PATH_VARIABLE, DataCatalog, normalize_reader_options
from .errors import ConfigurationError
from .query import validate_layer
from .registry import BUILTIN_AGGREGATIONS, BUILTIN_PLOTS, Registry


def _reader_options_code(options: dict[str, Any]) -> str:
    """Render normalized reader options as standalone Python source."""

    def render(value: Any) -> str:
        if isinstance(value, pl.DataType) or (
            isinstance(value, type) and issubclass(value, pl.DataType)
        ):
            return f"pl.{value!r}"
        if isinstance(value, dict):
            items = ", ".join(f"{key!r}: {render(item)}" for key, item in value.items())
            return "{" + items + "}"
        if isinstance(value, list):
            return "[" + ", ".join(render(item) for item in value) + "]"
        return repr(value)

    return render(normalize_reader_options(options))


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
        if isinstance(dtype, pl.Datetime):
            parsed = datetime.fromisoformat(str(value))
            if dtype.time_zone:
                zone = ZoneInfo(dtype.time_zone)
                return (
                    parsed.replace(tzinfo=zone)
                    if parsed.tzinfo is None
                    else parsed.astimezone(zone)
                )
            return parsed.replace(tzinfo=None)
        return str(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} is incompatible with {dtype}: {value}") from error


def _filter_code(item: dict[str, Any], schema: pl.Schema) -> str:
    if item.get("type") == "group":
        children = [_filter_code(child, schema) for child in item["filters"]]
        combiner = "all_horizontal" if item.get("logic", "and") == "and" else "any_horizontal"
        return f"pl.{combiner}([{', '.join(children)}])"
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
    if operator == "between":
        lower = _coerce(
            item.get("min", item.get("value")), schema[column], f"filter {column} minimum"
        )
        upper = _coerce(
            item.get("max", item.get("value2")), schema[column], f"filter {column} maximum"
        )
        return f"{expression}.is_between({lower!r}, {upper!r}, closed='both')"
    value = _coerce(raw, schema[column], f"filter {column}")
    symbol = {"eq": "==", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}[
        operator
    ]
    return f"{expression} {symbol} {value!r}"


def _aggregation_code(layer: dict[str, Any]) -> str:
    aggregation = layer["aggregation"]
    if aggregation in {"count", "relative_count"}:
        return "pl.len()"
    column = f"pl.col({layer['y_column']!r})"
    if aggregation == "quantile":
        quantile = float(layer["aggregation_options"].get("quantile", 0.5))
        return f"{column}.quantile({quantile!r})"
    if aggregation == "relative_value":
        denominator = f"pl.col({layer['aggregation_options']['denominator']!r}).sum()"
        multiplier = (
            100.0 if layer["aggregation_options"].get("scale") == "percent" else 1.0
        )
        return (
            f"pl.when({denominator} != 0).then("
            f"{column}.sum().cast(pl.Float64) / {denominator} * {multiplier!r}"
            ").otherwise(None)"
        )
    return f"{column}.{aggregation}()"


def _query_code(index: int, source_variable: str, layer: dict[str, Any], schema: pl.Schema) -> str:
    variable = f"layer_{index}"
    all_variable = f"{variable}_all"
    comment = str(layer["label"]).replace("\n", " ").replace("\r", " ")
    lines = [f"    # {comment}", f"    {variable} = {source_variable}"]
    if layer["limit"] is not None:
        lines.append(f"    {variable} = {variable}.limit({layer['limit']!r})")
    required_filters = [_filter_code(item, schema) for item in layer["required_filters"]]
    if required_filters:
        lines.append(
            f"    {variable} = {variable}.filter(pl.all_horizontal(["
            f"{', '.join(required_filters)}]))"
        )
    if layer["aggregation"] == "relative_count":
        lines.append(f"    {all_variable} = {variable}")
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
        x_key = "_time_bin" if layer["time_bin"] else x_column
        if layer["time_bin"]:
            truncate = (
                f"pl.col({x_column!r}).dt.truncate({layer['time_bin']!r})"
                ".alias('_time_bin')"
            )
            lines.append(f"    {variable} = {variable}.with_columns({truncate})")
            if layer["aggregation"] == "relative_count":
                lines.append(f"    {all_variable} = {all_variable}.with_columns({truncate})")
        groups = [column for column in (x_key, group_column) if column]
        if layer["aggregation"] == "relative_count":
            multiplier = (
                100.0 if layer["aggregation_options"].get("scale") == "percent" else 1.0
            )
            if groups:
                lines.extend(
                    [
                        f"    {all_variable} = {all_variable}.group_by({groups!r}).agg("
                        "pl.len().alias('_denominator'))",
                        f"    {variable} = {variable}.group_by({groups!r}).agg("
                        "pl.len().alias('_numerator'))",
                        f"    {variable} = {all_variable}.join({variable}, on={groups!r}, "
                        "how='left').with_columns((pl.col('_numerator').fill_null(0).cast("
                        f"pl.Float64) / pl.col('_denominator') * {multiplier!r}).alias('_y'))",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"    {all_variable} = {all_variable}.select("
                        "pl.len().alias('_denominator'))",
                        f"    {variable} = {variable}.select(pl.len().alias('_numerator'))",
                        f"    {variable} = {all_variable}.join({variable}, how='cross').select(("
                        "pl.col('_numerator').cast(pl.Float64) / pl.col('_denominator') * "
                        f"{multiplier!r}).alias('_y')).with_row_index('_x', offset=1)",
                    ]
                )
        else:
            aggregation = _aggregation_code(layer) + ".alias('_y')"
            if groups:
                lines.append(f"    {variable} = {variable}.group_by({groups!r}).agg({aggregation})")
            else:
                lines.append(
                    f"    {variable} = {variable}.select({aggregation})"
                    ".with_row_index('_x', offset=1)"
                )
        if groups:
            selections = [f"pl.col({x_key!r}).alias('_x')"]
            if group_column:
                selections.append(f"pl.col({group_column!r}).cast(pl.String).alias('_group')")
            selections.append("pl.col('_y')")
            lines.append(f"    {variable} = {variable}.select([{', '.join(selections)}])")
    if layer["result_y_min"] is not None:
        lines.append(
            f"    {variable} = {variable}.filter("
            f"pl.col('_y') >= {layer['result_y_min']!r})"
        )
    if layer["result_y_max"] is not None:
        lines.append(
            f"    {variable} = {variable}.filter("
            f"pl.col('_y') <= {layer['result_y_max']!r})"
        )
    if layer["sort"] != "none":
        column, descending = layer["sort"].split("_")
        lines.append(
            f"    {variable} = {variable}.sort('_{column}', "
            f"descending={descending == 'descending'!r}, nulls_last=True)"
        )
    if layer["result_limit"] is not None:
        lines.append(f"    {variable} = {variable}.limit({layer['result_limit']!r})")
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
        f"        style = {{**DEFAULT_STYLE, 'color': STD_COLORS[{index} % "
        f"len(STD_COLORS)], **{layer['style']!r}}}",
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
        where = layer["options"].get("where", "post")
        lines.append(
            "        ax.step(x, y, label=label, "
            f"where={where!r}, **plot_style)"
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
        position = float(layer["options"].get("position", 1))
        lines.append(f"        ax.boxplot(y, positions=[{position!r}], tick_labels=[label])")
    elif plot_type == "violin":
        showmeans = bool(layer["options"].get("showmeans", False))
        position = float(layer["options"].get("position", 1))
        lines.append(
            f"        ax.violinplot(y, positions=[{position!r}], showmeans={showmeans!r})"
        )
    elif plot_type == "stem":
        lines.append("        ax.stem(x, y, label=label)")
    elif plot_type == "hexbin":
        gridsize = int(layer["options"].get("gridsize", 30))
        lines.append(f"        ax.hexbin(x, y, gridsize={gridsize!r}, mincnt=1)")
    return "\n".join(lines)


def _annotation_code(item: dict[str, Any], axis: str = "primary") -> str:
    kind = item.get("kind")
    color = item.get("color", "#666666")
    alpha = float(item.get("alpha", 0.6))
    text = str(item.get("text", "")).replace("\\n", "\n")
    label = item["legend_label"] if item.get("show_in_legend", False) else "_nolegend_"
    label_code = f"\n        _annotation_label({axis}, {item!r})" if text and kind != "text" else ""
    if kind == "vline":
        return (
            f"        {axis}.axvline(_annotation_x({item['x']!r}), color={color!r}, "
            f"alpha={alpha!r}, "
            f"linestyle={item.get('linestyle', '--')!r}, "
            f"linewidth={float(item.get('linewidth', 1.0))!r}, label={label!r})"
        ) + label_code
    if kind == "hline":
        return (
            f"        {axis}.axhline({item['y']!r}, color={color!r}, alpha={alpha!r}, "
            f"linestyle={item.get('linestyle', '--')!r}, "
            f"linewidth={float(item.get('linewidth', 1.0))!r}, label={label!r})"
        ) + label_code
    if kind == "vspan":
        return (
            f"        {axis}.axvspan(_annotation_x({item['x1']!r}), "
            f"_annotation_x({item['x2']!r}), "
            f"facecolor={color!r}, edgecolor='none', alpha={alpha!r}, label={label!r})"
        ) + label_code
    if kind == "hspan":
        return (
            f"        {axis}.axhspan({item['y1']!r}, {item['y2']!r}, "
            f"facecolor={color!r}, edgecolor='none', alpha={alpha!r}, label={label!r})"
        ) + label_code
    if kind == "text":
        code = (
            f"        if _annotation_y_visible({axis}, {item['y']!r}):\n"
            f"            _foreground_text({axis}, _annotation_x({item['x']!r}), "
            f"{item['y']!r}, {text!r}, "
            f"color={item.get('text_color', color)!r}, "
            f"alpha={alpha!r}, fontsize={float(item.get('fontsize', 10))!r}, "
            f"bbox=_annotation_bbox({item!r}))"
        )
        if item.get("show_in_legend", False):
            code += (
                f"\n            {axis}.plot([], [], linestyle='none', marker='o', "
                f"markersize=4, color={item.get('text_color', color)!r}, "
                f"alpha={alpha!r}, label={label!r})"
            )
        return code
    raise ConfigurationError(f"unsupported annotation kind: {kind}")


HELPERS = '''
STD_COLORS = ["#375E97", "#FB6542", "#c1195c", "#37975e"]
DEFAULT_STYLE = {
    "color": STD_COLORS[0],
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
    if lower is None and upper is None:
        return
    current_lower, current_upper = getattr(ax, f"get_{axis}lim")()
    getattr(ax, f"set_{axis}lim")(
        current_lower if lower is None else lower,
        current_upper if upper is None else upper,
    )


def _x_limit_value(value, x_values):
    if value is None or isinstance(value, (datetime.date, datetime.datetime)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    sample = next((item for item in x_values if item is not None), None)
    if isinstance(sample, datetime.datetime):
        try:
            return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"invalid date/time X limit: {value!r}") from error
    if isinstance(sample, datetime.date):
        try:
            return datetime.date.fromisoformat(str(value))
        except ValueError as error:
            raise ValueError(f"invalid date X limit: {value!r}") from error
    if isinstance(sample, str):
        text = str(value)
        values = list(dict.fromkeys(str(item) for item in x_values if item is not None))
        if text not in values:
            raise ValueError(f"X limit {value!r} is not present in the X values")
        return float(values.index(text))
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"X limit must be numeric: {value!r}") from error


def _grid(ax, enabled, axis, alpha):
    if enabled:
        ax.grid(True, axis=axis, which="major", alpha=alpha)
    else:
        ax.grid(False, axis=axis, which="major")


def _automatic_x_limits(ax, values, time_bins):
    if not values:
        return
    first = values[0]
    try:
        if isinstance(first, (datetime.date, datetime.datetime)):
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


def _smallest_time_bin_days(time_bins):
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
        match = re.fullmatch(r"([1-9]\\d*)(ns|us|ms|s|m|h|d|w|mo|q|y)", value)
        if match:
            durations.append(int(match.group(1)) * seconds_per_unit[match.group(2)] / 86400)
    return min(durations, default=1 / 1440)


def _annotation_x(value):
    if not isinstance(value, str):
        return value
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


def _annotation_bbox(item):
    if not item.get("text_background_enabled", False):
        return None
    return {
        "facecolor": item.get("text_background_color", "#ffffff"),
        "edgecolor": "none",
        "alpha": float(item.get("alpha", 0.6)),
        "pad": 2.0,
    }


def _foreground_text(
    ax, x, y, text, transform=None, x_data=True, y_data=True, **options
):
    return ax.figure.text(
        ax.convert_xunits(x) if x_data else x,
        ax.convert_yunits(y) if y_data else y,
        text,
        transform=transform if transform is not None else ax.transData,
        clip_on=False, zorder=1000, **options,
    )


def _annotation_y_visible(ax, y):
    try:
        lower, upper = sorted(ax.get_ylim())
        return lower <= float(y) <= upper
    except (TypeError, ValueError):
        return True


def _annotation_label(ax, item):
    kind = item["kind"]
    text = str(item.get("text", "")).replace("\\\\n", "\\n")
    color = item.get("text_color", item.get("color", "#666666"))
    alpha = float(item.get("alpha", 0.6))
    fontsize = float(item.get("fontsize", 10))
    if item.get("text_y") is not None:
        lower, upper = sorted(ax.get_ylim())
        if not lower <= float(item["text_y"]) <= upper:
            return
        _foreground_text(
            ax,
            _annotation_x(item.get("text_x", item.get("x", item.get("x1")))),
            item["text_y"], text, color=color, alpha=alpha, fontsize=fontsize,
            horizontalalignment=item.get("text_horizontal_alignment", "left"),
            verticalalignment=item.get("text_vertical_alignment", "center"),
            bbox=_annotation_bbox(item),
        )
        return
    if kind in {"vline", "vspan"}:
        try:
            if not ax.get_subplotspec().is_first_row():
                return
        except AttributeError:
            pass
        x = item["x"] if kind == "vline" else item["x1"]
        _foreground_text(
            ax,
            _annotation_x(x), 0.98, text, transform=ax.get_xaxis_transform(),
            y_data=False,
            color=color, alpha=alpha, fontsize=fontsize, rotation=90,
            horizontalalignment="right", verticalalignment="top",
            bbox=_annotation_bbox(item),
        )
    else:
        y = item["y"] if kind == "hline" else item["y2"]
        if not _annotation_y_visible(ax, y):
            return
        _foreground_text(
            ax,
            0.99, y, text, transform=ax.get_yaxis_transform(), color=color,
            x_data=False,
            alpha=alpha, fontsize=fontsize, horizontalalignment="right",
            verticalalignment="bottom", bbox=_annotation_bbox(item),
        )


def _ticks(ax, axes, time_binned=False, x_values=None):
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


def _y_ticks(ax, axes, secondary=False):
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


def _minor_y_ticks(ax, enabled):
    if enabled:
        ax.yaxis.minorticks_on()
        ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    else:
        ax.yaxis.set_minor_locator(mticker.NullLocator())


def _broken_axis_marks(axes):
    size = 0.012
    style = {"color": "black", "clip_on": False, "linewidth": 0.8}
    for upper, lower in zip(axes, axes[1:]):
        upper.spines["bottom"].set_visible(False)
        lower.spines["top"].set_visible(False)
        upper.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        for x in (0, 1):
            upper.plot(
                (x - size, x + size), (-size, size), transform=upper.transAxes, **style
            )
            lower.plot(
                (x - size, x + size), (1 - size, 1 + size),
                transform=lower.transAxes, **style
            )
'''.strip()


def generate_script(config: dict[str, Any], catalog: DataCatalog, registry: Registry) -> str:
    """Return a standalone script for a validated built-in plot configuration."""
    plot_formats = [item for item in config["export_formats"] if item in {"png", "pdf"}]
    if not plot_formats:
        raise ConfigurationError("select PNG and/or PDF before exporting code")
    if config["stages"]["enabled"]:
        raise ConfigurationError(
            "standalone code export does not yet support stages; use Download to export all stages"
        )
    if any(
        layer.get("enabled", True) and layer.get("fix_x_values", False)
        for layer in config["layers"]
    ):
        raise ConfigurationError(
            "standalone code export does not yet support fixed shared X values; "
            "use Download to export the rendered plot"
        )
    if any(
        (annotation.get("inference") or {}).get("x", "manual") != "manual"
        or (annotation.get("inference") or {}).get("y", "manual") != "manual"
        for annotation in config["annotations"]
    ):
        raise ConfigurationError(
            "standalone code export does not yet support inferred annotation coordinates; "
            "use Download to export the rendered plot"
        )

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
        "import re",
        "import sys",
        "import zoneinfo",
        "import matplotlib as mpl",
        'mpl.use("Agg")',
        'mpl.rcParams["pdf.fonttype"] = 42',
        'mpl.rcParams["ps.fonttype"] = 42',
        "import matplotlib.pyplot as plt",
        "import matplotlib.dates as mdates",
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
        options = _reader_options_code(dict(item.get("options") or {}))
        source_lines.append(
            f"    {lazy_variable} = pl.{scanner}({path_variable}, **{options})"
        )

    draw_functions = [_draw_code(index, layer) for index, layer in enumerate(enabled_layers)]
    query_blocks = []
    for index, layer in enumerate(enabled_layers):
        schema = catalog.schema(layer["source"])
        query_blocks.append(_query_code(index, source_variables[layer["source"]], layer, schema))

    figure = config["figure"]
    axes = config["axes"]
    global_font_size = float(figure["font_size"])
    label_font_size = (
        float(axes["label_font_size"])
        if axes["label_font_size_override"]
        else global_font_size
    )
    tick_font_size = (
        float(axes["tick_font_size"])
        if axes["tick_font_size_override"]
        else global_font_size
    )
    legend_font_size = (
        float(config["legend"]["font_size"])
        if config["legend"]["font_size_override"]
        else global_font_size
    )
    legend_options = (
        f"loc={config['legend']['loc']!r}, ncols={int(config['legend']['ncols'])!r}, "
        f"fontsize={legend_font_size!r}, "
        f"handlelength={float(config['legend']['handlelength'])!r}, "
        f"columnspacing={float(config['legend']['columnspacing'])!r}, "
        f"handletextpad={float(config['legend']['handletextpad'])!r}, "
        f"framealpha={float(config['legend']['opacity'])!r}"
    )
    if config["legend"]["bbox_enabled"]:
        legend_options += (
            f", bbox_to_anchor=({float(config['legend']['bbox_x'])!r}, "
            f"{float(config['legend']['bbox_y'])!r})"
        )
    x_value_layers = [
        index
        for index, layer in enumerate(enabled_layers)
        if layer["plot_type"] not in {"histogram", "box", "violin"}
    ]
    x_values_code = " + ".join(
        f"layer_{index}.get_column('_x').drop_nulls().to_list()" for index in x_value_layers
    ) or "[]"
    time_bins = [layer["time_bin"] for layer in enabled_layers if layer["time_bin"]]
    time_axis = any(
        layer["time_bin"]
        or (
            layer["x_column"] is not None
            and (
                catalog.schema(layer["source"])[layer["x_column"]] == pl.Date
                or isinstance(catalog.schema(layer["source"])[layer["x_column"]], pl.Datetime)
            )
        )
        for layer in enabled_layers
    )
    broken_y = config["broken_y_axis"]
    if broken_y["enabled"]:
        figure_lines = [
            "    fig, created_axes = plt.subplots(",
            f"        nrows={len(broken_y['ranges'])!r}, sharex=True,",
            f"        figsize=({float(figure['width'])!r}, {float(figure['height'])!r}),",
            f"        gridspec_kw={{'hspace': {float(broken_y['gap'])!r}}},",
            "    )",
            "    primary_axes = list(created_axes)",
            "    primary = primary_axes[-1]",
        ]
    else:
        figure_lines = [
            f"    fig, primary = plt.subplots(figsize="
            f"({float(figure['width'])!r}, {float(figure['height'])!r}))",
            "    primary_axes = [primary]",
        ]
    main_lines = [
        "def main():",
        *source_lines,
        "",
        *[line for block in query_blocks for line in (block, "")],
        *figure_lines,
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
            main_lines.extend(
                [
                    "    for plot_axis in primary_axes:",
                    f"        _draw_layer_{index}(plot_axis, layer_{index}, state)",
                ]
            )
    main_lines.extend(
        [
            f"    axes = {axes!r}",
            "    for plot_axis in primary_axes:",
            "        plot_axis.set_xscale(axes['xscale'])",
            "        plot_axis.set_yscale(axes['yscale'])",
            "    if secondary is not None:",
            "        secondary.set_yscale(axes['secondary_yscale'])",
            "    primary.set_xlabel(str(axes['xlabel']).replace('\\\\n', '\\n'), "
            f"fontsize={label_font_size!r})",
            (
                "    fig.supylabel(str(axes['ylabel']).replace('\\\\n', '\\n'), "
                f"fontsize={label_font_size!r})"
                if broken_y["enabled"]
                else "    primary.set_ylabel(str(axes['ylabel']).replace('\\\\n', '\\n'), "
                f"fontsize={label_font_size!r})"
            ),
            "    if secondary is not None:",
            "        secondary.set_ylabel(str(axes['secondary_ylabel']).replace('\\\\n', "
            f"'\\n'), fontsize={label_font_size!r})",
            f"    x_axis_values = {x_values_code}",
            "    xmin = _x_limit_value(axes['xmin'], x_axis_values)",
            "    xmax = _x_limit_value(axes['xmax'], x_axis_values)",
            "    if axes['xmin'] is None and axes['xmax'] is None:",
            f"        _automatic_x_limits(primary, x_axis_values, {time_bins!r})",
        ]
    )
    if broken_y["enabled"]:
        main_lines.extend(
            [
                f"    broken_ranges = {broken_y['ranges']!r}",
                "    for plot_axis, value_range in zip(primary_axes, "
                "reversed(broken_ranges), strict=True):",
                "        plot_axis.set_ylim(value_range['min'], value_range['max'])",
            ]
        )
    else:
        main_lines.append("    _limits(primary, axes['ymin'], axes['ymax'], 'y')")
    main_lines.extend(
        [
            "    if secondary is not None:",
            "        _limits(secondary, axes['secondary_ymin'], axes['secondary_ymax'], 'y')",
            f"    _ticks(primary, axes, {time_axis!r}, x_axis_values)",
            "    _limits(primary, xmin, xmax, 'x')",
            "    if axes['y_engineering']:",
            "        for plot_axis in primary_axes:",
            "            plot_axis.yaxis.set_major_formatter(mticker.EngFormatter(sep=''))",
            "    for plot_axis in primary_axes:",
            "        _y_ticks(plot_axis, axes)",
            "        _minor_y_ticks(plot_axis, axes['minor_y_ticks'])",
            "    if secondary is not None and axes['secondary_y_engineering']:",
            "        secondary.yaxis.set_major_formatter(mticker.EngFormatter(sep=''))",
            "    if secondary is not None:",
            "        _y_ticks(secondary, axes, secondary=True)",
            "        _minor_y_ticks(secondary, axes['secondary_minor_y_ticks'])",
            "    for plot_axis in primary_axes:",
            "        plot_axis.tick_params(axis='both', which='both', "
            f"labelsize={tick_font_size!r})",
            "    if secondary is not None:",
            f"        secondary.tick_params(axis='y', which='both', labelsize={tick_font_size!r})",
            "    for plot_axis in primary_axes:",
            "        plot_axis.set_axisbelow(True)",
            "        _grid(plot_axis, bool(axes['x_grid']), 'x', "
            "float(axes['grid_alpha']))",
            "        _grid(plot_axis, bool(axes['y_grid']), 'y', "
            "float(axes['grid_alpha']))",
            "    if secondary is not None:",
            "        secondary.set_axisbelow(True)",
            "        _grid(secondary, bool(axes['secondary_y_grid']), 'y', "
            "float(axes['grid_alpha']))",
            "    for plot_axis in primary_axes:",
        ]
    )
    main_lines.extend(
        _annotation_code(item, "plot_axis")
        for item in config["annotations"]
        if item.get("enabled", True)
    )
    if not any(item.get("enabled", True) for item in config["annotations"]):
        main_lines.append("        pass")
    if broken_y["enabled"]:
        main_lines.append("    _broken_axis_marks(primary_axes)")
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
                (
                    f"        primary_axes[0].legend(handles, labels, {legend_options})"
                    if broken_y["enabled"]
                    else f"        primary.legend(handles, labels, {legend_options})"
                ),
            ]
        )
    if figure["font_family"] == "monospace":
        main_lines.extend(
            [
                "    for text in fig.findobj(match=plt.Text):",
                "        text.set_fontfamily('monospace')",
            ]
        )
    main_lines.append(
        f"    fig.subplots_adjust(hspace={float(broken_y['gap'])!r})"
        if broken_y["enabled"]
        else "    fig.tight_layout()"
    )
    if (
        config["legend"]["enabled"]
        and config["legend"]["loc"] == "upper center"
        and config["legend"]["bbox_enabled"]
        and config["legend"]["bbox_x"] == 0.5
        and config["legend"]["bbox_y"] == 1.2
    ):
        main_lines.extend(
            [
                f"    legend_axis = {'primary_axes[0]' if broken_y['enabled'] else 'primary'}",
                "    placed_legend = legend_axis.get_legend()",
                "    if placed_legend is not None:",
                "        fig.canvas.draw()",
                "        renderer = fig.canvas.get_renderer()",
                "        axis_bounds = legend_axis.get_window_extent(renderer)",
                "        legend_bounds = placed_legend.get_window_extent(renderer)",
                "        rise = max(0.0, axis_bounds.y1 + fig.dpi * 4 / 72 - legend_bounds.y0)",
                "        if rise:",
                "            placed_legend.set_bbox_to_anchor(",
                "                (0.5, 1.2 + rise / axis_bounds.height),",
                "                transform=legend_axis.transAxes,",
                "            )",
            ]
        )
    main_lines.append("    try:")
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
