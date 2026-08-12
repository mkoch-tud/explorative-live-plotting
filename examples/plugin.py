"""Example custom aggregation and plot renderer plugin."""

import polars as pl


def register(registry):
    registry.aggregation(
        "range",
        lambda column, options: pl.col(column).max() - pl.col(column).min(),
    )

    def lollipop(context):
        color = context.style.get("color", "#0072b2")
        context.ax.vlines(context.x, 0, context.y, color=color, alpha=0.65)
        context.ax.scatter(context.x, context.y, color=color, label=context.label)

    registry.plot("lollipop", lollipop)
