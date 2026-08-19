# Explorative Live Plotting

A standalone localhost plotting workbench for exploratory analysis. It reads
arbitrary tabular files with Polars lazy scans, performs only the operations
needed for the selected plot layers, caches collected query results, and renders
with Matplotlib.

## Install and run

```bash
cd /mnt/data/explorative-live-plotting
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/elp --source measurements=/path/to/data.parquet
```

Open <http://127.0.0.1:9000>. Sources can also be registered in the browser.
Run the command from any project directory; relative output and cache paths are
resolved from that directory.

Useful options:

```text
elp [--source NAME=PATH]... [--plugin FILE]...
    [--output-dir DIRECTORY] [--cache-dir DIRECTORY]
    [--memory-cache-entries N] [--host HOST] [--port PORT]
```

Examples:

```bash
# One CSV
elp --source results=./results.csv

# A partitioned Parquet dataset via a glob
elp --source scans='/data/scans/*.parquet' --port 9000

# Multiple unrelated sources
elp --source routers=routers.parquet --source countries=countries.csv

# Custom aggregation and plot type
elp --source data=data.parquet --plugin examples/plugin.py
```

Supported lazy source formats are CSV, Parquet, newline-delimited JSON, and
Arrow IPC/Feather. Reader keyword arguments can be supplied as JSON when a
source is registered in the browser. A directory or glob can represent a
multi-file dataset where the Polars scanner supports it.

## Query and plot model

Each plot is composed of independent layers. A layer selects:

- a source and plot renderer;
- x, y, and optional grouping/color columns;
- an aggregation and optional aggregation parameters;
- any number of filters combined with AND or OR;
- sorting, row limit, stacking, styling, and primary/secondary y-axis.

Built-in aggregations are `none`, `sum`, `min`, `max`, `mean`, `median`, `std`,
`var`, `first`, `last`, `count`, `n_unique`, and `quantile`. Built-in plot
renderers are line, step, scatter, bar, area, histogram, box, violin, stem, and
hexbin. Layers can be mixed freely on primary and secondary axes.

Axis controls include labels, limits, linear/log/symlog/logit scales, grids,
custom x ticks and labels, major/minor ticks, rotation, alignment, and a
plot-wide default or monospace font. Basic lines, spans, and text annotations
can be supplied as JSON.

PNG and PDF plots and the complete JSON configuration can be downloaded or
saved to the configured output directory. The JSON includes source paths and
reader settings. Loading it in the browser re-registers missing sources and
restores the plot.

## Lazy execution and cache

Adding a source reads only its schema. Layer filters, projections, grouping,
aggregation, sorting, and limiting remain in a Polars `LazyFrame` until a plot
is validated, rendered, saved, or downloaded.

Collected query results are cached:

- a small in-memory LRU provides immediate repeated renders;
- Arrow IPC files in `.elp-cache/` persist results across server restarts;
- cache keys include the normalized layer query and source fingerprints;
- fingerprints include path, size, and nanosecond modification time for every
  file matched by a source, so changed data invalidates old entries;
- **Clear cache** removes both memory and disk entries.

The cache contains aggregated/projected layer results, not entire input files.
This keeps large raw datasets lazy and makes interactive restyling inexpensive.

## Custom aggregations and plot types

Plugins are regular Python files with a `register(registry)` function. They run
locally with the same permissions as the application and should therefore only
be loaded from trusted files.

```python
import polars as pl


def register(registry):
    registry.aggregation(
        "range",
        lambda column, options: pl.col(column).max() - pl.col(column).min(),
    )

    def lollipop(context):
        color = context.style.get("color", "#0072b2")
        context.ax.vlines(context.x, 0, context.y, color=color)
        context.ax.scatter(context.x, context.y, color=color, label=context.label)

    registry.plot("lollipop", lollipop)
```

Aggregation factories receive `(column_name, options)` and return one Polars
expression. Renderer functions receive a `PlotContext` containing the target
Matplotlib axis, collected layer frame, validated layer configuration, x/y
values, label, style, and shared per-figure state. The included
[`examples/plugin.py`](examples/plugin.py) is directly runnable.

This registry is the extension point for specialized plots and domain-specific
aggregations; no central application changes are required.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
```

The server binds to localhost by default. Supplying a different `--host`
exposes filesystem-backed source and plugin functionality to that interface;
only do so in a trusted environment.

