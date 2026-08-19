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
    [--config-module DOTTED.MODULE]
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

# Resolve ${DATA_PATH} from an existing repository's Python configuration
elp --config-module ipv6_ndpi.config --source measurements='${DATA_PATH}/measurements.csv'

# Custom aggregation and plot type
elp --source data=data.parquet --plugin examples/plugin.py
```

Supported lazy source formats are CSV, Parquet, newline-delimited JSON, and
Arrow IPC/Feather. Reader keyword arguments can be supplied as JSON when a
source is registered in the browser. A directory or glob can represent a
multi-file dataset where the Polars scanner supports it.

CSV sources have a dedicated optional separator field. Enter a single-byte
character such as `;` or `|`, or enter `\t` for a tab. When a source is added,
the server collects only its first 100 rows in a threaded request worker and
caches the inferred schema; the browser remains interactive during discovery.

The source form remains available after each registration, so there is no fixed
limit on the number of independent data sources in a plot. Each plot layer can
select any registered source.

## Portable paths and repository integration

When this workbench is installed into or alongside another Python repository,
it can import path values from that repository's existing configuration module.
Run `elp` from the target repository root so its package and ordinary relative
paths are importable:

```bash
python -m pip install -e /path/to/explorative-live-plotting
elp --config-module ipv6_ndpi.config
```

The module may expose strings or `pathlib.Path` values:

```python
# ipv6_ndpi/config.py
from pathlib import Path

DATA_PATH = Path("/srv/measurements")
```

Enter `${DATA_PATH}/daily/*.parquet` as a source path in the browser. The raw
template is retained in saved plot configurations while the resolved path is
used for schema discovery and lazy scans. The config module can be changed in
the browser; all existing sources are revalidated before the change takes
effect. Paths without placeholders keep their existing behavior: relative paths
are resolved from the directory where `elp` or an exported script is run.

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
reader settings. Loading it in the browser restores the plot and atomically
replaces the active source catalog with the saved one.

**Export code** downloads a single `<plot-name>.py` containing the complete
configuration plus explicit Polars query and Matplotlib plotting code. Run it
from the target repository root to recreate the selected PNG/PDF outputs:

```bash
python explorative-plot.py
```

The generated file needs Polars and Matplotlib, but it does not import this
workbench. If it contains portable paths, the configured module must be
importable. Built-in aggregations and plot types can be exported; plots that use
custom plugin callables are rejected because those callables cannot be embedded
reliably into a standalone file.

PDF and PostScript Matplotlib output uses font type 42, embedding TrueType fonts
instead of type 3 glyphs. The same setting is included in standalone scripts.

## Lazy execution and cache

Adding a source collects only a 100-row head to infer and cache its schema.
Layer filters, projections, grouping, aggregation, sorting, and limiting remain
in a Polars `LazyFrame` until a plot is validated, rendered, saved, or
downloaded.

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
