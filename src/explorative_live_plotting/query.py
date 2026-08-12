"""Validation and lazy Polars query execution."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any

import polars as pl

from .cache import QueryCache
from .data import DataCatalog
from .errors import ConfigurationError
from .logging import log
from .registry import Registry

OPERATORS = {"eq", "ne", "gt", "ge", "lt", "le", "in", "not_in", "is_null", "is_not_null"}


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


def _filter_expression(item: dict[str, Any], schema: pl.Schema) -> pl.Expr:
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
    schema = catalog.lazy(spec).collect_schema()
    plot_type = raw.get("plot_type", "line")
    aggregation = raw.get("aggregation", "none")
    x_column = raw.get("x_column") or None
    y_column = raw.get("y_column") or None
    group_column = raw.get("group_column") or None
    for field, column in (("x", x_column), ("y", y_column), ("group", group_column)):
        if column is not None and column not in schema:
            raise ConfigurationError(f"{field} column does not exist in {source}: {column}")
    if plot_type not in registry.plots:
        raise ConfigurationError(f"unknown plot type: {plot_type}")
    if aggregation not in registry.aggregations:
        raise ConfigurationError(f"unknown aggregation: {aggregation}")
    if plot_type not in {"histogram", "box", "violin"} and x_column is None:
        raise ConfigurationError(f"{plot_type} requires an x column")
    if aggregation != "count" and y_column is None:
        raise ConfigurationError(f"{aggregation} requires a y column")
    filters = raw.get("filters", [])
    if not isinstance(filters, list):
        raise ConfigurationError("layer filters must be an array")
    for item in filters:
        if not isinstance(item, dict):
            raise ConfigurationError("each filter must be an object")
        _filter_expression(item, schema)
    filter_logic = raw.get("filter_logic", "and")
    if filter_logic not in {"and", "or"}:
        raise ConfigurationError("filter logic must be and or or")
    sort = raw.get("sort", "x_ascending")
    if sort not in {"none", "x_ascending", "x_descending", "y_ascending", "y_descending"}:
        raise ConfigurationError(f"invalid sort mode: {sort}")
    limit = raw.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ConfigurationError("layer limit must be a positive integer or null")
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
        "aggregation_options": dict(raw.get("aggregation_options") or {}),
        "filter_logic": filter_logic,
        "filters": filters,
        "sort": sort,
        "limit": limit,
        "stacked": bool(raw.get("stacked", False)),
        "secondary_y": bool(raw.get("secondary_y", False)),
        "style": dict(raw.get("style") or {}),
        "options": dict(raw.get("options") or {}),
    }


class QueryEngine:
    def __init__(self, catalog: DataCatalog, registry: Registry, cache: QueryCache) -> None:
        self.catalog = catalog
        self.registry = registry
        self.cache = cache

    def execute(self, raw_layer: dict[str, Any]) -> tuple[pl.DataFrame, dict[str, Any], str]:
        layer = validate_layer(raw_layer, self.catalog, self.registry)
        payload = {"source": self.catalog.fingerprint(layer["source"]), "query": layer}
        key = self.cache.key(payload)

        def compute() -> pl.DataFrame:
            log(f"Collecting lazy query for layer {layer['id']} from {layer['source']}")
            return self._lazy_query(layer).collect(engine="streaming")

        frame, cache_state = self.cache.get_or_compute(key, compute)
        log(f"Layer {layer['id']}: {frame.height} rows ({cache_state})")
        return frame, layer, cache_state

    def _lazy_query(self, layer: dict[str, Any]) -> pl.LazyFrame:
        lazy = self.catalog.lazy(layer["source"])
        schema = lazy.collect_schema()
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
        else:
            groups = [column for column in (x_column, group_column) if column]
            aggregation = self.registry.aggregations[layer["aggregation"]](
                y_column, layer["aggregation_options"]
            ).alias("_y")
            if groups:
                lazy = lazy.group_by(groups).agg(aggregation)
                expressions = [pl.col(x_column).alias("_x")]
                if group_column:
                    expressions.append(pl.col(group_column).cast(pl.String).alias("_group"))
                lazy = lazy.select(*expressions, "_y")
            else:
                lazy = lazy.select(aggregation).with_row_index("_x", offset=1)
        sort = layer["sort"]
        if sort != "none":
            column, descending = sort.split("_")
            lazy = lazy.sort(f"_{column}", descending=descending == "descending", nulls_last=True)
        if layer["limit"] is not None:
            lazy = lazy.limit(layer["limit"])
        return lazy
