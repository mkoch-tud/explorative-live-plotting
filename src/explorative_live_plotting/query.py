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
MAX_PLOT_ROWS = 1_000_000
CACHE_SCHEMA_VERSION = 7
QUERY_FIELDS = (
    "source",
    "x_column",
    "y_column",
    "group_column",
    "aggregation",
    "aggregation_options",
    "time_bin",
    "filter_logic",
    "required_filters",
    "filters",
    "sort",
    "limit",
    "result_limit",
    "result_y_min",
    "result_y_max",
    "fixed_x_values",
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
    if plot_type not in {"histogram", "box", "violin"} and x_column is None:
        raise ConfigurationError(f"{plot_type} requires an x column")
    if aggregation not in {"count", "relative_count"} and y_column is None:
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
    filter_logic = raw.get("filter_logic", "and")
    if filter_logic not in {"and", "or"}:
        raise ConfigurationError("filter logic must be and or or")
    raw_aggregation_options = raw.get("aggregation_options", {})
    if raw_aggregation_options is None:
        raw_aggregation_options = {}
    if not isinstance(raw_aggregation_options, dict):
        raise ConfigurationError("aggregation options must be a JSON object")
    aggregation_options = dict(raw_aggregation_options)
    if aggregation == "relative_count":
        scale = aggregation_options.get("scale", "fraction")
        if scale not in {"fraction", "percent"}:
            raise ConfigurationError("relative_count scale must be fraction or percent")
    if aggregation == "relative_value":
        denominator = aggregation_options.get("denominator")
        if not isinstance(denominator, str) or not denominator:
            raise ConfigurationError(
                "relative_value requires a denominator column in aggregation options"
            )
        if denominator not in schema:
            raise ConfigurationError(
                f"relative_value denominator column does not exist in {source}: {denominator}"
            )
        if y_column is not None and not schema[y_column].is_numeric():
            raise ConfigurationError("relative_value requires a numeric Y column")
        if not schema[denominator].is_numeric():
            raise ConfigurationError("relative_value requires a numeric denominator column")
        scale = aggregation_options.get("scale", "fraction")
        if scale not in {"fraction", "percent"}:
            raise ConfigurationError("relative_value scale must be fraction or percent")
    if aggregation == "quantile":
        try:
            quantile = float(aggregation_options.get("quantile", 0.5))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("quantile must be a number between 0 and 1") from error
        if not math.isfinite(quantile) or not 0 <= quantile <= 1:
            raise ConfigurationError("quantile must be a number between 0 and 1")
        aggregation_options["quantile"] = quantile
    raw_options = raw.get("options", {})
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise ConfigurationError("plot options must be a JSON object")
    options = dict(raw_options)
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
        "filter_logic": filter_logic,
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
            f"group_by_dynamic(x={layer['x_column']}, every={layer['time_bin']})"
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
        if layer["fixed_x_values"] is not None:
            fixed_expression = (
                pl.col(layer["x_column"])
                .dt.truncate(layer["time_bin"])
                .is_in(layer["fixed_x_values"])
                if layer["time_bin"]
                else pl.col(layer["x_column"]).is_in(layer["fixed_x_values"])
            )
            base = base.filter(fixed_expression)
        required_expressions = [
            _filter_expression(item, schema) for item in layer["required_filters"]
        ]
        if required_expressions:
            base = base.filter(pl.all_horizontal(required_expressions))
        lazy = base
        expressions = [_filter_expression(item, schema) for item in layer["filters"]]
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
        else:
            aggregation = self.registry.aggregations[layer["aggregation"]](
                y_column, layer["aggregation_options"]
            ).alias("_y")
            lazy = self._aggregate(lazy, layer, aggregation)
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
            lazy = lazy.sort(f"_{column}", descending=descending == "descending", nulls_last=True)
        if layer["result_limit"] is not None:
            lazy = lazy.limit(layer["result_limit"])
        # Bound materialization before Matplotlib conversion. The extra row lets
        # execute() distinguish a complete result from overflow.
        lazy = lazy.limit(MAX_PLOT_ROWS + 1)
        return lazy

    @staticmethod
    def _group_keys(lazy: pl.LazyFrame, layer: dict[str, Any]) -> tuple[pl.LazyFrame, list[str]]:
        x_column = layer["x_column"]
        keys: list[str] = []
        if x_column:
            if layer["time_bin"]:
                lazy = lazy.with_columns(
                    pl.col(x_column).dt.truncate(layer["time_bin"]).alias("_time_bin")
                )
                keys.append("_time_bin")
            else:
                keys.append(x_column)
        if layer["group_column"]:
            keys.append(layer["group_column"])
        return lazy, keys

    @classmethod
    def _aggregate(
        cls, lazy: pl.LazyFrame, layer: dict[str, Any], aggregation: pl.Expr
    ) -> pl.LazyFrame:
        lazy, keys = cls._group_keys(lazy, layer)
        if not keys:
            return lazy.select(aggregation).with_row_index("_x", offset=1)
        lazy = lazy.group_by(keys).agg(aggregation)
        x_key = "_time_bin" if layer["time_bin"] else layer["x_column"]
        selections = [pl.col(x_key).alias("_x")]
        if layer["group_column"]:
            selections.append(pl.col(layer["group_column"]).cast(pl.String).alias("_group"))
        return lazy.select(*selections, "_y")

    @classmethod
    def _relative_count(
        cls, base: pl.LazyFrame, filtered: pl.LazyFrame, layer: dict[str, Any]
    ) -> pl.LazyFrame:
        """Count filtered rows divided by all rows in each x/group bin."""
        denominator, keys = cls._group_keys(base, layer)
        numerator, _ = cls._group_keys(filtered, layer)
        multiplier = 100.0 if layer["aggregation_options"].get("scale") == "percent" else 1.0
        if not keys:
            return denominator.select(pl.len().alias("_denominator")).join(
                numerator.select(pl.len().alias("_numerator")), how="cross"
            ).select(
                (
                    pl.col("_numerator").cast(pl.Float64)
                    / pl.col("_denominator")
                    * multiplier
                ).alias("_y")
            ).with_row_index("_x", offset=1)
        denominator = denominator.group_by(keys).agg(pl.len().alias("_denominator"))
        numerator = numerator.group_by(keys).agg(pl.len().alias("_numerator"))
        result = denominator.join(numerator, on=keys, how="left").with_columns(
            (
                pl.col("_numerator").fill_null(0).cast(pl.Float64)
                / pl.col("_denominator")
                * multiplier
            ).alias("_y")
        )
        x_key = "_time_bin" if layer["time_bin"] else layer["x_column"]
        selections = [pl.col(x_key).alias("_x")]
        if layer["group_column"]:
            selections.append(pl.col(layer["group_column"]).cast(pl.String).alias("_group"))
        return result.select(*selections, "_y")
