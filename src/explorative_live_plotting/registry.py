"""Registries for aggregation expressions and Matplotlib plot renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import matplotlib.axes
import polars as pl

from .errors import ConfigurationError

AggregationFactory = Callable[[str | None, dict[str, Any]], pl.Expr]
PlotRenderer = Callable[["PlotContext"], None]
BUILTIN_AGGREGATIONS = {
    "none",
    "sum",
    "min",
    "max",
    "mean",
    "median",
    "std",
    "var",
    "first",
    "last",
    "count",
    "n_unique",
    "quantile",
}
BUILTIN_PLOTS = {
    "line",
    "step",
    "scatter",
    "bar",
    "area",
    "histogram",
    "box",
    "violin",
    "stem",
    "hexbin",
}


@dataclass
class PlotContext:
    """Stable public context passed to plot renderer plugins."""

    ax: matplotlib.axes.Axes
    frame: pl.DataFrame
    layer: dict[str, Any]
    x: list[Any]
    y: list[Any]
    label: str
    style: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)


class Registry:
    """Namespaced registry with built-ins and optional external plugins."""

    def __init__(self) -> None:
        self.aggregations: dict[str, AggregationFactory] = {}
        self.plots: dict[str, PlotRenderer] = {}

    def aggregation(self, name: str, function: AggregationFactory) -> None:
        if not name or name in self.aggregations:
            raise ConfigurationError(f"aggregation name is empty or already registered: {name}")
        self.aggregations[name] = function

    def plot(self, name: str, function: PlotRenderer) -> None:
        if not name or name in self.plots:
            raise ConfigurationError(f"plot name is empty or already registered: {name}")
        self.plots[name] = function

    def metadata(self) -> dict[str, list[str]]:
        return {
            "aggregations": sorted(self.aggregations),
            "plot_types": sorted(self.plots),
        }


def _value(column: str | None) -> pl.Expr:
    if not column:
        raise ConfigurationError("this aggregation requires a y column")
    return pl.col(column)


def _style(context: PlotContext) -> dict[str, Any]:
    allowed = {
        "color",
        "alpha",
        "linewidth",
        "linestyle",
        "marker",
        "markersize",
        "zorder",
    }
    return {key: value for key, value in context.style.items() if key in allowed}


def _line(context: PlotContext) -> None:
    context.ax.plot(context.x, context.y, label=context.label, **_style(context))


def _step(context: PlotContext) -> None:
    context.ax.step(
        context.x,
        context.y,
        label=context.label,
        where=context.style.get("where", "post"),
        **_style(context),
    )


def _scatter(context: PlotContext) -> None:
    style = _style(context)
    if "linewidth" in style:
        style["linewidths"] = style.pop("linewidth")
    if "markersize" in style:
        style["s"] = float(style.pop("markersize")) ** 2
    context.ax.scatter(context.x, context.y, label=context.label, **style)


def _bar(context: PlotContext) -> None:
    style = _style(context)
    style.pop("marker", None)
    style.pop("markersize", None)
    bottom = None
    if context.layer.get("stacked", False):
        key = (id(context.ax), "bar", tuple(context.x))
        bottom = context.state.setdefault(key, [0.0] * len(context.y))
    context.ax.bar(context.x, context.y, bottom=bottom, label=context.label, **style)
    if bottom is not None:
        context.state[key] = [
            old + float(value or 0) for old, value in zip(bottom, context.y, strict=True)
        ]


def _area(context: PlotContext) -> None:
    style = _style(context)
    style.pop("marker", None)
    style.pop("markersize", None)
    context.ax.fill_between(context.x, context.y, label=context.label, **style)


def _histogram(context: PlotContext) -> None:
    style = _style(context)
    style.pop("marker", None)
    style.pop("markersize", None)
    context.ax.hist(
        context.y,
        bins=int(context.layer.get("options", {}).get("bins", 30)),
        density=bool(context.layer.get("options", {}).get("density", False)),
        label=context.label,
        **style,
    )


def _box(context: PlotContext) -> None:
    context.ax.boxplot(
        context.y,
        positions=[context.layer.get("position", 1)],
        tick_labels=[context.label],
    )


def _violin(context: PlotContext) -> None:
    context.ax.violinplot(
        context.y,
        positions=[context.layer.get("position", 1)],
        showmeans=bool(context.layer.get("options", {}).get("showmeans", False)),
    )


def _stem(context: PlotContext) -> None:
    context.ax.stem(context.x, context.y, label=context.label)


def _hexbin(context: PlotContext) -> None:
    context.ax.hexbin(
        context.x,
        context.y,
        gridsize=int(context.layer.get("options", {}).get("gridsize", 30)),
        mincnt=1,
    )


def builtins() -> Registry:
    registry = Registry()
    registry.aggregation("none", lambda column, options: _value(column))
    registry.aggregation("sum", lambda column, options: _value(column).sum())
    registry.aggregation("min", lambda column, options: _value(column).min())
    registry.aggregation("max", lambda column, options: _value(column).max())
    registry.aggregation("mean", lambda column, options: _value(column).mean())
    registry.aggregation("median", lambda column, options: _value(column).median())
    registry.aggregation("std", lambda column, options: _value(column).std())
    registry.aggregation("var", lambda column, options: _value(column).var())
    registry.aggregation("first", lambda column, options: _value(column).first())
    registry.aggregation("last", lambda column, options: _value(column).last())
    registry.aggregation("count", lambda column, options: pl.len())
    registry.aggregation("n_unique", lambda column, options: _value(column).n_unique())
    registry.aggregation(
        "quantile",
        lambda column, options: _value(column).quantile(float(options.get("quantile", 0.5))),
    )
    for name, renderer in {
        "line": _line,
        "step": _step,
        "scatter": _scatter,
        "bar": _bar,
        "area": _area,
        "histogram": _histogram,
        "box": _box,
        "violin": _violin,
        "stem": _stem,
        "hexbin": _hexbin,
    }.items():
        registry.plot(name, renderer)
    return registry


def load_plugin(path: Path, registry: Registry) -> ModuleType:
    """Load a plugin module exposing register(registry)."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"plugin does not exist: {path}")
    spec = importlib.util.spec_from_file_location(f"elp_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f"cannot load plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if not callable(register):
        raise ConfigurationError(f"plugin must define register(registry): {path}")
    register(registry)
    return module
