"""Validation and lazy Polars query execution."""

from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .cache import QueryCache
from .data import DataCatalog
from .errors import ConfigurationError
from .logging import log
from .registry import Registry

OPERATORS = {
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "between",
    "in",
    "not_in",
    "is_null",
    "is_not_null",
}
TIME_BIN = re.compile(r"[1-9]\d*(?:ns|us|ms|s|m|h|d|w|mo|q|y)")
WEEKDAYS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}
MAX_PLOT_ROWS = 1_000_000
CACHE_SCHEMA_VERSION = 10
QUERY_FIELDS = (
    "source",
    "x_column",
    "y_column",
    "group_column",
    "aggregation",
    "aggregation_options",
    "time_bin",
    "time_bin_start_by",
    "fill_missing_time_bins_with_zero",
    "filter_logic",
    "base_filter_expression",
    "filter_expression",
    "required_filters",
    "filters",
    "sort",
    "limit",
    "result_limit",
    "result_y_min",
    "result_y_max",
    "fixed_x_values",
)


def _fill_missing_time_bins(lazy: pl.LazyFrame, time_bin: str) -> pl.LazyFrame:
    """Complete each series over the layer's time span, preserving existing nulls."""
    schema = lazy.collect_schema()
    dtype = schema["_x"]
    range_options = {"interval": time_bin}
    if dtype == pl.Date:
        ranges = pl.date_ranges
    else:
        ranges = pl.datetime_ranges
        range_options.update(time_unit=dtype.time_unit, time_zone=dtype.time_zone)
    domain = lazy.select(
        ranges(pl.col("_x").min(), pl.col("_x").max(), **range_options).alias("_x")
    ).explode("_x").drop_nulls("_x")
    keys = ["_x"]
    if "_group" in schema:
        domain = domain.join(lazy.select("_group").unique(), how="cross")
        keys.append("_group")
    return (
        domain.join(
            lazy.with_columns(pl.lit(True).alias("_present")),
            on=keys, how="left", nulls_equal=True,
        )
        .with_columns(
            pl.when(pl.col("_present").is_null()).then(0).otherwise(pl.col("_y")).alias("_y")
        )
        .drop("_present")
        .sort(keys)
    )


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


def _filter_expression(item: dict[str, Any], schema: pl.Schema) -> pl.Expr:
    if item.get("type") == "group":
        logic = item.get("logic", "and")
        if logic not in {"and", "or"}:
            raise ConfigurationError("nested filter group logic must be `and` or `or`")
        children = item.get("filters")
        if not isinstance(children, list) or not children:
            raise ConfigurationError("nested filter groups must contain at least one filter")
        expressions = []
        for child in children:
            if not isinstance(child, dict):
                raise ConfigurationError("each nested filter must be an object")
            expressions.append(_filter_expression(child, schema))
        return (
            pl.all_horizontal(expressions)
            if logic == "and"
            else pl.any_horizontal(expressions)
        )
    column = item.get("column")
    operator = item.get("operator", "eq")
    if column not in schema:
        raise ConfigurationError(f"filter column does not exist: {column}")
    if operator not in OPERATORS:
        raise ConfigurationError(f"unsupported filter operator: {operator}")
    expression = pl.col(column)
    if operator == "is_null":
        return expression.is_null()
    if operator == "is_not_null":
        return expression.is_not_null()
    raw = item.get("value")
    if operator in {"in", "not_in"}:
        values = raw if isinstance(raw, list) else [part.strip() for part in str(raw).split(",")]
        converted = [_coerce(value, schema[column], f"filter {column}") for value in values]
        result = expression.is_in(converted)
        return ~result if operator == "not_in" else result
    if operator == "between":
        lower_raw = item.get("min", item.get("value"))
        upper_raw = item.get("max", item.get("value2"))
        lower = _coerce(lower_raw, schema[column], f"filter {column} minimum")
        upper = _coerce(upper_raw, schema[column], f"filter {column} maximum")
        if lower > upper:
            raise ConfigurationError(
                f"filter {column} minimum cannot be greater than its maximum"
            )
        return expression.is_between(lower, upper, closed="both")
    value = _coerce(raw, schema[column], f"filter {column}")
    return {
        "eq": expression == value,
        "ne": expression != value,
        "gt": expression > value,
        "ge": expression >= value,
        "lt": expression < value,
        "le": expression <= value,
    }[operator]


def validate_layer(raw: Any, catalog: DataCatalog, registry: Registry) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("each plot layer must be an object")
    source = raw.get("source")
    spec = catalog.get(source)
    schema = catalog.schema(spec.name)
    plot_type = raw.get("plot_type", "line")
    aggregation = raw.get("aggregation", "none")
    time_bin = str(raw.get("time_bin") or "").strip() or None
    raw_options = raw.get("options", {})
    legacy_start_by = (
        raw_options.get("start_by") if isinstance(raw_options, dict) else None
    )
    time_bin_start_by = str(
        raw.get("time_bin_start_by", legacy_start_by) or "monday"
    ).strip().lower()
    if time_bin_start_by not in WEEKDAYS:
        raise ConfigurationError(
            "time bin start weekday must be monday, tuesday, wednesday, thursday, "
            "friday, saturday, or sunday"
        )
    x_column = raw.get("x_column") or None
    y_column = raw.get("y_column") or None
    group_column = raw.get("group_column") or None
    for field, column in (("x", x_column), ("y", y_column), ("group", group_column)):
        if column is not None and column not in schema:
            raise ConfigurationError(f"{field} column does not exist in {source}: {column}")
    fixed_x_values = raw.get("fixed_x_values")
    if fixed_x_values is not None:
        if not isinstance(fixed_x_values, list):
            raise ConfigurationError("fixed X values must be an array or null")
        if x_column is None:
            raise ConfigurationError("fixed X values require an x column")
        fixed_x_values = [
            _coerce(value, schema[x_column], "fixed X value") for value in fixed_x_values
        ]
    if plot_type not in registry.plots:
        raise ConfigurationError(f"unknown plot type: {plot_type}")
    if aggregation not in registry.aggregations:
        raise ConfigurationError(f"unknown aggregation: {aggregation}")
    if time_bin is not None:
        if aggregation == "none":
            raise ConfigurationError("time binning requires an aggregation")
        if x_column is None:
            raise ConfigurationError("time binning requires an x column")
        if schema[x_column] != pl.Date and not isinstance(schema[x_column], pl.Datetime):
            raise ConfigurationError("time binning requires a date or datetime x column")
        if TIME_BIN.fullmatch(time_bin) is None:
            raise ConfigurationError(
                "time bin must be a positive Polars duration such as 1m, 1h, 1d, 1w, or 1mo"
            )
    break_on_missing_time_bin = raw.get("break_on_missing_time_bin", False)
    if not isinstance(break_on_missing_time_bin, bool):
        raise ConfigurationError("break on missing time bin must be true or false")
    fill_missing_time_bins_with_zero = raw.get("fill_missing_time_bins_with_zero", False)
    if not isinstance(fill_missing_time_bins_with_zero, bool):
        raise ConfigurationError("fill missing time bins with zero must be true or false")
    if break_on_missing_time_bin and fill_missing_time_bins_with_zero:
        raise ConfigurationError("choose either break on missing time bin or fill with zero")
    if plot_type not in {"histogram", "box", "violin"} and x_column is None:
        raise ConfigurationError(f"{plot_type} requires an x column")
    if aggregation not in {"count", "relative_count", "relative_value"} and y_column is None:
        raise ConfigurationError(f"{aggregation} requires a y column")
    filters = raw.get("filters", [])
    if not isinstance(filters, list):
        raise ConfigurationError("layer filters must be an array")
    for item in filters:
        if not isinstance(item, dict):
            raise ConfigurationError("each filter must be an object")
        _filter_expression(item, schema)
    required_filters = raw.get("required_filters", [])
    if not isinstance(required_filters, list):
        raise ConfigurationError("required layer filters must be an array")
    for item in required_filters:
        if not isinstance(item, dict):
            raise ConfigurationError("each required filter must be an object")
        _filter_expression(item, schema)
    raw_base_filter_expression = raw.get("base_filter_expression")
    raw_filter_expression = raw.get("filter_expression")
    if raw_base_filter_expression is not None and not isinstance(
        raw_base_filter_expression, str
    ):
        raise ConfigurationError("layer base filter expression must be a string or null")
    if raw_filter_expression is not None and not isinstance(raw_filter_expression, str):
        raise ConfigurationError("layer filter expression must be a string or null")
    base_filter_expression = str(raw_base_filter_expression or "").strip() or None
    filter_expression = str(raw_filter_expression or "").strip() or None
    if base_filter_expression:
        catalog.filter_expression(base_filter_expression, schema)
    if filter_expression:
        catalog.filter_expression(filter_expression, schema)
    filter_logic = raw.get("filter_logic", "and")
    if filter_logic not in {"and", "or"}:
        raise ConfigurationError("filter logic must be and or or")
    raw_aggregation_options = raw.get("aggregation_options", {})
    if raw_aggregation_options is None:
        raw_aggregation_options = {}
    if not isinstance(raw_aggregation_options, dict):
        raise ConfigurationError("aggregation options must be a JSON object")
    aggregation_options = dict(raw_aggregation_options)
    if aggregation in {"relative_count", "relative_value"}:
        denominator_source = aggregation_options.get("denominator_source") or None
        if denominator_source is not None and not isinstance(denominator_source, str):
            raise ConfigurationError(f"{aggregation} denominator source must be a source name")
        denominator_schema = catalog.schema(denominator_source) if denominator_source else schema
        if denominator_source:
            denominator_x = aggregation_options.get("denominator_x_column") or x_column
            denominator_group = aggregation_options.get("denominator_group_column") or None
            if x_column is not None:
                if denominator_x not in denominator_schema:
                    raise ConfigurationError(f"{aggregation} denominator X column does not exist")
                numerator_dtype = schema[x_column]
                denominator_dtype = denominator_schema[denominator_x]
                numerator_temporal = numerator_dtype == pl.Date or isinstance(
                    numerator_dtype, pl.Datetime
                )
                denominator_temporal = denominator_dtype == pl.Date or isinstance(
                    denominator_dtype, pl.Datetime
                )
                if time_bin and not denominator_temporal:
                    raise ConfigurationError(f"{aggregation} denominator X must be a date or datetime")
                if not (numerator_temporal and denominator_temporal) and (
                    numerator_dtype != denominator_dtype
                ):
                    raise ConfigurationError(f"{aggregation} X columns must have compatible types")
            if denominator_group and (
                not group_column or denominator_group not in denominator_schema
            ):
                raise ConfigurationError(
                    f"{aggregation} denominator split column requires a numerator split "
                    "and must exist in the denominator source"
                )
            aggregation_options["denominator_x_column"] = denominator_x
            aggregation_options["denominator_group_column"] = denominator_group
        aggregation_options["denominator_source"] = denominator_source
    if aggregation == "relative_count":
        scale = aggregation_options.get("scale", "fraction")
        if scale not in {"fraction", "percent"}:
            raise ConfigurationError("relative_count scale must be fraction or percent")
    if aggregation == "relative_value":
        value_aggregation = aggregation_options.get("value_aggregation", "sum")
        if (
            not isinstance(value_aggregation, str)
            or value_aggregation not in registry.aggregations
            or value_aggregation in {"none", "relative_count", "relative_value"}
        ):
            raise ConfigurationError("relative_value requires a non-relative value aggregation")
        denominator = aggregation_options.get("denominator")
        if value_aggregation != "count" and (not isinstance(denominator, str) or not denominator):
            raise ConfigurationError(
                "relative_value requires a denominator column in aggregation options"
            )
        if denominator is not None and denominator not in denominator_schema:
            raise ConfigurationError(
                "relative_value denominator column does not exist in "
                f"{denominator_source or source}: {denominator}"
            )
        if value_aggregation != "count" and y_column is None:
            raise ConfigurationError("relative_value requires a Y column")
        if value_aggregation not in {"count", "n_unique"} and not schema[y_column].is_numeric():
            raise ConfigurationError("relative_value requires a numeric Y column")
        if value_aggregation not in {"count", "n_unique"} and not denominator_schema[denominator].is_numeric():
            raise ConfigurationError("relative_value requires a numeric denominator column")
        aggregation_options["value_aggregation"] = value_aggregation
        scale = aggregation_options.get("scale", "fraction")
        if scale not in {"fraction", "percent"}:
            raise ConfigurationError("relative_value scale must be fraction or percent")
    if aggregation == "quantile" or (
        aggregation == "relative_value" and aggregation_options["value_aggregation"] == "quantile"
    ):
        try:
            quantile = float(aggregation_options.get("quantile", 0.5))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("quantile must be a number between 0 and 1") from error
        if not math.isfinite(quantile) or not 0 <= quantile <= 1:
            raise ConfigurationError("quantile must be a number between 0 and 1")
        aggregation_options["quantile"] = quantile
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise ConfigurationError("plot options must be a JSON object")
    options = dict(raw_options)
    # Older configurations placed this query option in renderer options. Keep
    # them working while exposing the setting as a first-class layer field.
    options.pop("start_by", None)
    if plot_type == "step" and options.get("where", "post") not in {"pre", "post", "mid"}:
        raise ConfigurationError("step where must be pre, post, or mid")
    if plot_type == "histogram":
        bins = options.get("bins", 30)
        if isinstance(bins, bool) or not isinstance(bins, int) or bins < 1:
            raise ConfigurationError("histogram bins must be a positive integer")
        if not isinstance(options.get("density", False), bool):
            raise ConfigurationError("histogram density must be true or false")
    if plot_type in {"box", "violin"}:
        position = options.get("position", 1)
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            raise ConfigurationError(f"{plot_type} position must be a finite number")
        if not math.isfinite(float(position)):
            raise ConfigurationError(f"{plot_type} position must be a finite number")
    if plot_type == "violin" and not isinstance(options.get("showmeans", False), bool):
        raise ConfigurationError("violin showmeans must be true or false")
    if plot_type == "hexbin":
        gridsize = options.get("gridsize", 30)
        if isinstance(gridsize, bool) or not isinstance(gridsize, int) or gridsize < 1:
            raise ConfigurationError("hexbin gridsize must be a positive integer")
    sort = raw.get("sort", "x_ascending")
    if sort not in {"none", "x_ascending", "x_descending", "y_ascending", "y_descending"}:
        raise ConfigurationError(f"invalid sort mode: {sort}")
    limit = raw.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ConfigurationError("layer limit must be a positive integer or null")
    result_limit = raw.get("result_limit")
    if result_limit is not None and (
        isinstance(result_limit, bool) or not isinstance(result_limit, int) or result_limit < 1
    ):
        raise ConfigurationError("result limit must be a positive integer or null")
    result_bounds = []
    for field in ("result_y_min", "result_y_max"):
        value = raw.get(field)
        if value is None:
            result_bounds.append(None)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"{field} must be a number or null") from error
        if not math.isfinite(number):
            raise ConfigurationError(f"{field} must be a finite number or null")
        result_bounds.append(number)
    result_y_min, result_y_max = result_bounds
    if (
        result_y_min is not None
        and result_y_max is not None
        and result_y_min > result_y_max
    ):
        raise ConfigurationError("result Y minimum cannot be greater than its maximum")
    return {
        "id": str(raw.get("id") or "layer"),
        "enabled": bool(raw.get("enabled", True)),
        "label": str(raw.get("label") or y_column or plot_type),
        "source": source,
        "plot_type": plot_type,
        "x_column": x_column,
        "y_column": y_column,
        "group_column": group_column,
        "aggregation": aggregation,
        "aggregation_options": aggregation_options,
        "time_bin": time_bin,
        "time_bin_start_by": time_bin_start_by,
        "break_on_missing_time_bin": break_on_missing_time_bin,
        "fill_missing_time_bins_with_zero": fill_missing_time_bins_with_zero,
        "filter_logic": filter_logic,
        "base_filter_expression": base_filter_expression,
        "filter_expression": filter_expression,
        "required_filters": required_filters,
        "filters": filters,
        "sort": sort,
        "limit": limit,
        "result_limit": result_limit,
        "result_y_min": result_y_min,
        "result_y_max": result_y_max,
        "fixed_x_values": fixed_x_values,
        "fix_x_values": bool(raw.get("fix_x_values", False)),
        "stacked": bool(raw.get("stacked", False)),
        "secondary_y": bool(raw.get("secondary_y", False)),
        "style": dict(raw.get("style") or {}),
        "options": options,
    }


class QueryEngine:
    def __init__(self, catalog: DataCatalog, registry: Registry, cache: QueryCache) -> None:
        self.catalog = catalog
        self.registry = registry
        self.cache = cache

    def execute(self, raw_layer: dict[str, Any]) -> tuple[pl.DataFrame, dict[str, Any], str]:
        layer = validate_layer(raw_layer, self.catalog, self.registry)
        grouping = (
            f"group_by_dynamic(x={layer['x_column']}, every={layer['time_bin']}, "
            f"start_by={layer['time_bin_start_by']})"
            if layer["time_bin"]
            else f"group_by(x={layer['x_column']})"
            if layer["aggregation"] != "none"
            else "none (raw rows)"
        )
        log(
            f"Layer {layer['id']} query: {grouping}, aggregation={layer['aggregation']}, "
            f"y={layer['y_column']}, split={layer['group_column'] or '(none)'}, "
            f"input_limit={layer['limit']}, result_limit={layer['result_limit']}"
        )
        query = {field: layer[field] for field in QUERY_FIELDS}
        payload = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "source": self.catalog.fingerprint(layer["source"]),
            "query": query,
            "max_plot_rows": MAX_PLOT_ROWS,
        }
        denominator_source = layer["aggregation_options"].get("denominator_source")
        if layer["aggregation"] in {"relative_count", "relative_value"} and denominator_source:
            payload["denominator_source"] = self.catalog.fingerprint(denominator_source)
        key = self.cache.key(payload)

        def compute() -> pl.DataFrame:
            log(f"Collecting lazy query for layer {layer['id']} from {layer['source']}")
            frame = self._lazy_query(layer).collect(engine="streaming")
            if frame.height > MAX_PLOT_ROWS:
                raise ConfigurationError(
                    f"layer {layer['label']} produces more than "
                    f"{MAX_PLOT_ROWS:,} plot rows; select group_by or group_by_dynamic "
                    f"with an aggregation, or set Input row limit to {MAX_PLOT_ROWS:,} or less"
                )
            return frame

        frame, cache_state = self.cache.get_or_compute(key, compute)
        log(f"Layer {layer['id']}: {frame.height} rows ({cache_state})")
        return frame, layer, cache_state

    def _lazy_query(self, layer: dict[str, Any]) -> pl.LazyFrame:
        base = self.catalog.lazy(layer["source"])
        if layer["limit"] is not None:
            # Cap the source before filters and aggregations so a limit bounds
            # the raw input read rather than merely trimming the plotted result.
            base = base.limit(layer["limit"])
        schema = self.catalog.schema(layer["source"])
        if layer["fixed_x_values"] is not None and not layer["time_bin"]:
            base = base.filter(
                pl.col(layer["x_column"]).is_in(layer["fixed_x_values"])
            )
        required_expressions = [
            _filter_expression(item, schema) for item in layer["required_filters"]
        ]
        if layer["base_filter_expression"]:
            required_expressions.append(
                self.catalog.filter_expression(layer["base_filter_expression"], schema)
            )
        if required_expressions:
            base = base.filter(pl.all_horizontal(required_expressions))
        lazy = base
        expressions = [_filter_expression(item, schema) for item in layer["filters"]]
        if layer["filter_expression"]:
            expressions.append(
                self.catalog.filter_expression(layer["filter_expression"], schema)
            )
        if expressions:
            combined = (
                pl.all_horizontal(expressions)
                if layer["filter_logic"] == "and"
                else pl.any_horizontal(expressions)
            )
            lazy = lazy.filter(combined)
        x_column, y_column, group_column = (
            layer["x_column"],
            layer["y_column"],
            layer["group_column"],
        )
        if layer["aggregation"] == "none":
            selections = []
            if x_column:
                selections.append(pl.col(x_column).alias("_x"))
            if y_column:
                selections.append(pl.col(y_column).alias("_y"))
            if group_column:
                selections.append(pl.col(group_column).cast(pl.String).alias("_group"))
            lazy = lazy.select(selections)
            if x_column is None:
                lazy = lazy.with_row_index("_x", offset=1)
        elif layer["aggregation"] == "relative_count":
            lazy = self._relative_count(base, lazy, layer)
        elif layer["aggregation"] == "relative_value":
            lazy = self._relative_value(lazy, layer)
        else:
            aggregation = self.registry.aggregations[layer["aggregation"]](
                y_column, layer["aggregation_options"]
            ).alias("_y")
            lazy = self._aggregate(lazy, layer, aggregation)
        external_relative = (
            layer["aggregation"] in {"relative_count", "relative_value"}
            and layer["aggregation_options"].get("denominator_source")
        )
        if layer["fill_missing_time_bins_with_zero"] and layer["time_bin"] and not external_relative:
            lazy = _fill_missing_time_bins(lazy, layer["time_bin"])
        if layer["result_y_min"] is not None:
            lazy = lazy.filter(pl.col("_y") >= layer["result_y_min"])
        if layer["result_y_max"] is not None:
            lazy = lazy.filter(pl.col("_y") <= layer["result_y_max"])
        sort = layer["sort"]
        if layer["fixed_x_values"] is not None:
            lazy = lazy.with_columns(
                pl.col("_x")
                .replace_strict(
                    layer["fixed_x_values"],
                    list(range(len(layer["fixed_x_values"]))),
                    default=None,
                    return_dtype=pl.Int64,
                )
                .alias("_fixed_x_order")
            ).filter(pl.col("_fixed_x_order").is_not_null()).sort("_fixed_x_order").drop(
                "_fixed_x_order"
            )
        elif sort != "none":
            column, descending = sort.split("_")
            if external_relative and layer["group_column"]:
                lazy = lazy.sort(
                    [f"_{column}", "_group"],
                    descending=[descending == "descending", False], nulls_last=True,
                )
            else:
                lazy = lazy.sort(f"_{column}", descending=descending == "descending", nulls_last=True)
        if layer["result_limit"] is not None:
            lazy = lazy.limit(layer["result_limit"])
        # Bound materialization before Matplotlib conversion. The extra row lets
        # execute() distinguish a complete result from overflow.
        lazy = lazy.limit(MAX_PLOT_ROWS + 1)
        return lazy

    @staticmethod
    def _grouped(
        lazy: pl.LazyFrame, layer: dict[str, Any], aggregation: pl.Expr
    ) -> tuple[pl.LazyFrame, list[str]]:
        """Apply regular or calendar-aware dynamic grouping and return join keys."""
        x_column = layer["x_column"]
        group_column = layer["group_column"]
        if layer["time_bin"]:
            group_options: dict[str, Any] = {
                "every": layer["time_bin"],
                "start_by": layer["time_bin_start_by"],
            }
            if group_column:
                group_options["group_by"] = group_column
            # Polars dynamic windows require the time index to be ascending.
            # Sorting here also keeps loaded files that are not pre-sorted correct.
            grouped = (
                lazy.sort(x_column)
                .group_by_dynamic(x_column, **group_options)
                .agg(aggregation)
            )
            return grouped, [column for column in (x_column, group_column) if column]
        keys = [column for column in (x_column, group_column) if column]
        if not keys:
            return lazy.select(aggregation), []
        return lazy.group_by(keys).agg(aggregation), keys

    @classmethod
    def _aggregate(
        cls, lazy: pl.LazyFrame, layer: dict[str, Any], aggregation: pl.Expr
    ) -> pl.LazyFrame:
        lazy, keys = cls._grouped(lazy, layer, aggregation)
        if not keys:
            return lazy.with_row_index("_x", offset=1)
        x_key = layer["x_column"]
        selections = [pl.col(x_key).alias("_x")]
        if layer["group_column"]:
            selections.append(pl.col(layer["group_column"]).cast(pl.String).alias("_group"))
        return lazy.select(*selections, "_y")

    def _relative_value(self, lazy: pl.LazyFrame, layer: dict[str, Any]) -> pl.LazyFrame:
        options = layer["aggregation_options"]
        aggregate = self.registry.aggregations[options["value_aggregation"]]
        numerator = aggregate(layer["y_column"], options)
        denominator = aggregate(options.get("denominator"), options)
        multiplier = 100.0 if options.get("scale") == "percent" else 1.0
        if not options["denominator_source"]:
            ratio = (
                pl.when(denominator != 0)
                .then(numerator.cast(pl.Float64) / denominator * multiplier)
                .otherwise(None)
                .alias("_y")
            )
            return self._aggregate(lazy, layer, ratio)

        numerator_frame = self._aggregate(lazy, layer, numerator.alias("_y"))
        return self._external_relative_ratio(numerator_frame, layer, denominator)

    def _external_relative_ratio(
        self, numerator_frame: pl.LazyFrame, layer: dict[str, Any], denominator: pl.Expr
    ) -> pl.LazyFrame:
        options = layer["aggregation_options"]
        multiplier = 100.0 if options.get("scale") == "percent" else 1.0
        if layer["fill_missing_time_bins_with_zero"] and layer["time_bin"]:
            numerator_frame = _fill_missing_time_bins(numerator_frame, layer["time_bin"])
        denominator_base = self.catalog.lazy(options["denominator_source"])
        denominator_layer = {
            **layer, "x_column": options["denominator_x_column"],
            "group_column": options["denominator_group_column"],
        }
        if layer["x_column"]:
            dtype = self.catalog.schema(layer["source"])[layer["x_column"]]
            denominator_base = denominator_base.with_columns(
                pl.col(denominator_layer["x_column"]).cast(dtype)
            )
        denominator_frame = self._aggregate(
            denominator_base, denominator_layer, denominator.alias("_y")
        ).rename({"_y": "_denominator"})
        keys = ["_x"]
        if denominator_layer["group_column"]:
            keys.append("_group")
        return (
            numerator_frame.join(denominator_frame, on=keys, how="left", nulls_equal=True)
            .with_columns(
                pl.when(pl.col("_denominator") != 0)
                .then(pl.col("_y").cast(pl.Float64) / pl.col("_denominator") * multiplier)
                .otherwise(None)
                .alias("_y")
            )
            .drop("_denominator")
            .sort(["_x", "_group"] if layer["group_column"] else ["_x"])
        )

    def _relative_count(
        self, base: pl.LazyFrame, filtered: pl.LazyFrame, layer: dict[str, Any]
    ) -> pl.LazyFrame:
        """Count filtered rows divided by all rows in each x/group bin."""
        if layer["aggregation_options"]["denominator_source"]:
            # Retain bins/series from the numerator's base rows even when its
            # layer filters select no rows, matching same-source relative_count.
            domain = self._aggregate(base, layer, pl.len().alias("_y")).drop("_y")
            numerator = self._aggregate(filtered, layer, pl.len().alias("_y"))
            keys = ["_x", "_group"] if layer["group_column"] else ["_x"]
            numerator_frame = domain.join(
                numerator, on=keys, how="left", nulls_equal=True
            ).with_columns(pl.col("_y").fill_null(0))
            return self._external_relative_ratio(numerator_frame, layer, pl.len())
        multiplier = 100.0 if layer["aggregation_options"].get("scale") == "percent" else 1.0
        denominator, keys = self._grouped(
            base, layer, pl.len().alias("_denominator")
        )
        numerator, _ = self._grouped(
            filtered, layer, pl.len().alias("_numerator")
        )
        if not keys:
            return denominator.join(numerator, how="cross").select(
                (
                    pl.col("_numerator").cast(pl.Float64)
                    / pl.col("_denominator")
                    * multiplier
                ).alias("_y")
            ).with_row_index("_x", offset=1)
        result = denominator.join(numerator, on=keys, how="left").with_columns(
            (
                pl.col("_numerator").fill_null(0).cast(pl.Float64)
                / pl.col("_denominator")
                * multiplier
            ).alias("_y")
        )
        x_key = layer["x_column"]
        selections = [pl.col(x_key).alias("_x")]
        if layer["group_column"]:
            selections.append(pl.col(layer["group_column"]).cast(pl.String).alias("_group"))
        return result.select(*selections, "_y")
