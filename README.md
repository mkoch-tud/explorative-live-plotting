# Explorative Live Plotting [elp]

Plugin your data source and explore it using live plotting instead of jupyter notebooks.
The plot renders (most of the time) live in the Web-UI and can be customized without having to write any code.
For reproducibility, the framework creates .json config files which allow to easily reload a plot again and edit it.
Furthermore, plots can be downloaded in .png and .pdf format and can be edited to be "paper-ready".
To enhance reproducibility for artifact evaluation the script can generate a standalone python script that calls the data sources and plots the figures as configured (this functionality hasn't been tested yet).

## Note on AI usage

This repository is entirely vibe coded with `gpt-5.6-sol` (medium/high reasoning).

## Install and run

Clone this repo, then:

```bash
cd explorative-live-plotting
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
elp
```

Open <http://127.0.0.1:9000>. Sources can be registered in the browser,. but you can pass cli arguments to register them directly (see below).
Run the command from any project directory; relative output and cache paths are resolved from that directory.

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

Schema options use JSON strings for Polars data types. For example, force a CSV
timestamp column to be parsed as a datetime with:

```json
{"schema_overrides": {"timestamp": "Datetime"}}
```

Both `"Datetime"` and `"pl.Datetime"` are accepted. To specify temporal details,
use an object such as `{"type":"Datetime","time_unit":"ms","time_zone":"UTC"}`.
Other common Polars scalar type names, including `Date`, `String`, `Boolean`,
`Int64`, and `Float64`, work the same way. The `schema`, `schema_overrides`, and
`hive_schema` reader options all support these JSON-safe type declarations.

CSV sources have a dedicated optional separator field. Enter a single-byte
character such as `;` or `|`, or enter `\t` for a tab. When a source is added,
the server collects only its first 100 rows in a threaded request worker and
caches the inferred schema; the browser remains interactive during discovery.

The source form remains available after each registration, so there is no fixed
limit on the number of independent data sources in a plot. Each plot layer can
select any registered source.

### Polars filter expressions

The advanced source options accept a **Global Polars filter**. It is applied
lazily to previews and every layer using that source. Expressions may be written
inline:

```python
pl.col("tcp.flags.text").str.contains("SYN") & ~pl.col("tcp.flags.text").str.contains("ACK")
```

They may also reference a public `pl.Expr` exported by the configured module or
by a module it exposes. For example, after configuring a module that imports
`constants as const`, enter `const.IS_SYN`. Available expression variables are
suggested by the browser. Only Polars functions and methods can be called; the
field does not execute unrestricted Python code.

Each layer has two corresponding fields under **Filters, limits & styling**:

- **Layer base Polars expression** is treated as a required condition when set
  and runs before the denominator of `relative_count` is calculated.
- **Layer Polars expression** is combined with the structured filter conditions
  using the selected root AND/OR rule, so it affects the plotted rows and the
  numerator of `relative_count`.

For a weekly percentage of irregular SYN packets, use `const.IS_SYN` globally
or as the layer base expression, then use `pl.col("is_irregular_syn")` as the
layer expression and select `relative_count` with `{"scale":"percent"}`.

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

While a source path is being typed, the browser shows its live resolved value
and offers completions for `${...}` variables from the applied config module as
well as matching directories and files. Press **Tab** to accept the highlighted
completion, use the arrow keys to select another match, or click a suggestion.

The **Show RAM/CPU** checkbox in the page header enables a bottom-right system
usage display. It reads lightweight host CPU and memory counters every ten
seconds and remains inactive when unchecked.

## Workspace tabs

Use **+ New workspace** above the configuration panel to open another plotting
workspace in the same page. Each tab keeps an independent configuration module,
source catalog, source-entry draft, layers, figure settings, render status, and
preview. This allows sources with the same name to refer to different files in
different tabs without collisions.

Click a tab to switch to it, double-click its name to rename it, and use its
close button to release its server-side catalog. An orange dot marks a workspace
whose query settings have changed since its last render. Query result files are
content-addressed and shared between workspaces, so identical queries can still
reuse cached results without sharing editable state. Workspace tabs last for the
current page session; downloaded JSON configurations remain the persistent way
to save an exploration across application restarts.

## Query and plot model

Each plot is composed of independent layers. A layer selects:

- a source and plot renderer;
- x, y, and optional grouping/color columns;
- an aggregation and optional aggregation parameters;
- any number of filters combined with AND or OR;
- sorting, an input-row limit, stacking, styling, and primary/secondary y-axis.

When X is a date or datetime column, select **group_by_dynamic** and set
**Every** to a Polars-style duration such as `1m`, `5m`, `1h`, `1d`, `1w`,
`1mo`, or `1y`. The timestamp is truncated to the start of each non-overlapping
interval before the selected aggregation is applied to Y.
This remains part of the lazy query, so raw rows are not collected in the UI.
Time-binned plots automatically use date-aware ticks that adapt from dates down
to hours, minutes, seconds, and fractions as appropriate for the visible span.
Set **Datetime tick format** to override those automatic labels with a Python
`strftime` pattern. For example, `%b %d` produces `Apr 01`, `%b %d, %Y`
produces `Apr 01, 2026`, and `%Y-%m-%d %H:%M` includes date, hour, and minute.
Common fields are `%Y` (year), `%y` (short year), `%b`/`%B` (abbreviated/full
month), `%m` (numeric month), `%d` (day), and `%H:%M:%S` (time). The browser
shows this reference next to the setting.
Unless explicit X limits are supplied, the axis is bounded by the minimum and
maximum plotted X values. A single time-bin point receives one bin of padding
instead of Matplotlib's years-wide single-date fallback.

Built-in aggregations are `none`, `sum`, `min`, `max`, `mean`, `median`, `std`,
`var`, `first`, `last`, `count`, `relative_count`, `relative_value`, `n_unique`,
and `quantile`.
Built-in plot renderers are line, step, scatter, bar, area, histogram, box,
violin, stem, and hexbin. Layers can be mixed freely on primary and secondary
axes.

`relative_count` divides the number of rows matching the layer's filters by all
rows in the same time/group bin. It returns a fraction from 0 to 1 by default;
use `{"scale":"percent"}` as the aggregation options for 0 to 100. For example,
to inspect high-TTL SYN packets by minute:

- total series: X `timestamp`, grouping `group_by_dynamic`, Every `1m`,
  aggregation `count`, no filter;
- high-TTL series: the same settings plus filter `ttl gt 200`;
- relative series: the same grouping with aggregation `relative_count`, filter
  `ttl gt 200`, and optionally percent scale.

`relative_value` is intended for data that already contains aggregated count
columns. It divides the sum of the selected Y column by the sum of another
numeric column in every X/group bin. Select `group_by`, choose the numerator as
Y, and set the denominator in the aggregation options. For example, to plot the
percentage represented by `irregular_tcp_options` out of `total`, use
`{"denominator":"total","scale":"percent"}`. A zero denominator produces a
null value instead of infinity.

### Grouping and aggregation

The layer editor exposes the query order explicitly:

1. **none (raw rows)** plots the selected X and Y values without aggregation.
2. **group_by** uses each distinct X value as a group. The aggregation function
   is applied to the selected Y values inside each X group.
3. **group_by_dynamic** requires a Date or Datetime X column. **Every** truncates
   that time to regular bins such as `1s`, `1m`, `5m`, `1h`, `1d`, `1w`, or
   `1mo`; the aggregation function is applied to Y inside each bin.

**Split series by / color** is independent of the X grouping. Selecting a
categorical column creates one plotted series and legend entry per distinct
value. During aggregation it is included as an additional grouping key. For
example, grouping timestamps every minute and splitting by `protocol` produces
one per-minute series for each protocol.

The aggregation options field must contain a JSON object. These are all
built-in aggregation functions and their options:

| Function | Applies to | Aggregation options JSON |
| --- | --- | --- |
| `none` | No grouping or aggregation; raw X/Y rows | `{}` |
| `count` | Counts rows in each group; Y is ignored | `{}` |
| `relative_count` | Filtered row count divided by all rows in the same group; Y is ignored | `{}` for a 0–1 fraction, or `{"scale":"percent"}` for 0–100 |
| `relative_value` | Sum of Y divided by the sum of another numeric column | `{"denominator":"total"}` for a 0–1 fraction, optionally with `"scale":"percent"` for 0–100 |
| `sum`, `min`, `max`, `mean`, `median`, `std`, `var` | Selected Y column | `{}` |
| `first`, `last` | First or last Y value in each group | `{}` |
| `n_unique` | Number of unique Y values in each group | `{}` |
| `quantile` | Quantile of Y | `{"quantile":0.95}`; must be from 0 to 1 and defaults to `0.5` |

Filters run before aggregation. The root filter list can use AND or OR, and
**+ nested group** adds a parenthesized group with its own AND/OR choice. Groups
can be nested repeatedly. For example, this selects packets in a time range
whose TTL is high or whose TCP-options field is non-null:

```json
{
  "filter_logic": "and",
  "filters": [
    {
      "column": "timestamp",
      "operator": "between",
      "value": "2026-04-01T00:05:00+00:00",
      "value2": "2026-04-01T00:10:00+00:00"
    },
    {
      "type": "group",
      "logic": "or",
      "filters": [
        {"column": "ttl", "operator": "gt", "value": 200},
        {"column": "tcp_options", "operator": "is_not_null"}
      ]
    }
  ]
}
```

`between` includes both endpoints. Its `value` is the minimum and `value2` is
the maximum; JSON configurations may equivalently use `min` and `max`. For a
single time bound, use **minimum / at or after (>=)** (`ge`) or
**maximum / at or before (<=)** (`le`). Date and datetime text is interpreted
as ISO 8601, such as `2026-04-01`, `2026-04-01T05:30:00`, or
`2026-04-01T05:30:00+00:00`.

Every condition has a **Query 5 values** button. It returns the column minimum,
maximum, and three ordered distribution values in between, then exposes those
values as input suggestions. Numeric and temporal columns use one lazy
projection with aggregate quantiles, so only one small result row is collected;
other data types use ordered distinct values but still return at most five
values to the browser. This helper queries the source column independently of
the layer's filters and input-row limit.

For `equals` on a Date or Datetime column, open **Choose an existing time
value** to browse the column's actual distinct timestamps. The list is sorted
and scrollable. It requests 250 values at a time and automatically fetches the
next page near the bottom, so every distinct value remains available without
one unbounded JSON response. Selecting an entry copies its exact ISO value into
the equality filter. Like **Query 5 values**, this picker reads the source
column before layer filters or the input-row limit.

The exception to normal filter order is the denominator of `relative_count`,
which intentionally counts all input rows in the same bin before the layer
filters are applied. Plugin aggregations receive the entire aggregation-options
object.

### Built-in plot options JSON

Plot options are renderer-specific and do not control styling. Use the visible
Color, Opacity, Marker, Marker size, Line width, and Line style controls for style. JSON uses double
quotes and lowercase `true`/`false` values.

| Plot type | Supported plot options |
| --- | --- |
| `line`, `scatter`, `bar`, `area`, `stem` | `{}`; no built-in JSON options |
| `step` | `{"where":"post"}` (default), `{"where":"pre"}`, or `{"where":"mid"}` |
| `histogram` | `{"bins":30,"density":false}`; `bins` is a positive integer |
| `box` | `{"position":1}`; numeric axis position |
| `violin` | `{"position":1,"showmeans":false}` |
| `hexbin` | `{"gridsize":30}`; positive integer grid resolution |

Unlisted keys are ignored by built-in renderers. Plugin plot types receive the
entire plot-options object through `context.layer["options"]`.

Axis controls include labels, limits, linear/log/symlog/logit scales, grids,
custom X/Y ticks and labels, major/minor ticks, rotation, alignment, and a
plot-wide default or monospace font. Independent engineering-notation toggles
are available for X, Y, and secondary Y. Custom X tick labels take precedence
over automatic date or engineering formatting. A global font size applies by
default; new plots start at 5.6 × 2.8 inches with a 12-point global font.
Checkboxes enable separate axis-label, legend, and tick sizes.
Engineering labels use compact notation without whitespace, such as `1k` or
`2.5M`. X, Y, and secondary-Y grids are always drawn behind plot layers. The
shared **Grid opacity** setting ranges from 0 (invisible) to 1 (fully opaque).
**X min** and **X max** are true Matplotlib-style `xlim` bounds: either side may
be supplied independently, and explicit limits are reapplied after tick
placement so custom ticks cannot enlarge the visible range. Numeric axes accept
numbers, categorical axes accept actual X values, and date/time axes accept ISO
values such as `2026-04-01T00:05:00+00:00`.
Primary and secondary Y axes have independent minor-tick checkboxes. The minor
locator follows the selected scale, including linear, log, symlog, and logit.

Primary Y ticks can be configured in two ways. **Custom Y ticks** accepts exact
comma-separated positions, with optional matching labels. Alternatively, set
all three **Tick range minimum**, **Tick range maximum**, and **Tick step**
fields to generate evenly spaced tick positions. Custom positions take
precedence when both forms are present. Tick settings do not change the visible
axis extent; use the separate Y min/max controls for that. On a broken Y axis,
the same locator is applied to every panel and only ticks inside each panel's
visible range are drawn.

The secondary Y axis has its own scale selector, limits, custom ticks and
labels, and min/max/step tick range. These settings apply only to layers with
**Secondary y** enabled. For example, select `log` under **Secondary Y scale**
and use custom ticks `1, 10, 100, 1000` without changing the primary Y scale or
ticks. Secondary-axis settings are also preserved in exported standalone code.

Enable **Use actual X-column values as tick labels** for categorical plots to
show values such as `DEU`, `NLD`, and `USA` instead of numeric category
positions. The interval controls whether every value or every nth value is
shown. Explicit custom X ticks take precedence over this mode. Migrated ranked
charts enable X-value labels automatically.

For several ranked layers, enable **Fix shared X values from this layer** on
exactly one layer. That anchor layer is evaluated first, including its filters,
aggregation, result-Y bounds, sorting, and result limit. Its final distinct X
values become the shared ordered domain. Every other enabled layer is then
filtered to those X values *before* its aggregation and is reordered to match
the anchor. For example, if the anchor's top ten countries are `DEU`, `NLD`,
and `USA`, a bar layer calculates and displays only those countries in exactly
that order even when its own top values would be different. A missing value in
a follower remains an empty position on the shared axis. Fixed shared X values
use a linear positional X axis and cannot be combined with histogram, box, or
violin layers.

The legend can be toggled independently and configured with an anchor location,
optional `bbox_to_anchor` X/Y coordinates, handle length, column count, and
frame opacity. **Spacing between columns** maps to Matplotlib's `columnspacing`,
while **Spacing between handle and label** maps to `handletextpad`; both are
measured in units of the legend font size and update live without recalculating
layer data. New plots place the legend above the plot with `upper center` and
`bbox_to_anchor=(0.5, 1.2)` by default. Handle length defaults to `1.5`, column
spacing to `0.8`, and handle-to-label spacing to `0.5`. If several legend rows
would overlap the axes, the default placement is lifted to keep a small gap.

The configuration panel starts wider than before and can be resized by dragging
the divider between the controls and preview. Its width is remembered locally;
double-click the divider (or focus it and press Home) to restore the default.
On narrow screens the layout automatically stacks the configuration and preview
vertically.

### Broken Y axes

Enable **Broken Y axis** to show non-contiguous Y ranges as vertically stacked
panels with a shared X axis. Enter ranges from low to high; they are rendered
from bottom to top. **Panel gap** is the fractional vertical spacing between
panels: 0 packs panels closely, while larger values up to 5 increase their
separation. Diagonal marks identify every break. Changing these controls is a
presentation-only update and reuses the cached layer query results.

The equivalent JSON is:

```json
{
  "broken_y_axis": {
    "enabled": true,
    "gap": 0.17,
    "ranges": [
      {"min": 600, "max": 1000},
      {"min": 15000, "max": 28000}
    ]
  }
}
```

An enabled broken Y axis needs at least two ordered, non-overlapping ranges.
Its ranges replace the regular Y min/max controls. Secondary-Y layers cannot
be combined with a broken primary Y axis.

New layers cycle through the built-in standard palette `#375E97`, `#FB6542`,
`#c1195c`, and `#37975e`. The same colors are available as one-click swatches
beside each layer's custom color picker, and standalone exports use this cycle
when a layer has no explicit color.

Annotations have structured browser editors for text, vertical/horizontal
lines, and vertical/horizontal spans. Every annotation has an editable,
unique ID and a separate descriptive label; **Displayed text** is the content
drawn on the plot. Each editor also provides coordinates, color, opacity, line
style, font size, and numeric coordinate/font nudge buttons. ISO datetime
strings are accepted for X coordinates. On broken Y axes, coordinate-based
text is drawn only in the panel containing its Y value, preventing duplicate
text at panel boundaries. Text is always drawn above plot and grid artists.
Enable **Text background** and select a color to place a colored box behind the
text. **Nudge step** is the numeric amount added or subtracted on each press of
an annotation coordinate's −/+ buttons; for example, a step of `1000000` moves
Y by one million per click.

X and Y can optionally be inferred from computed plot layers. Select one or
more **Inference layers**, then choose minimum/maximum X or Y, X at minimum or
maximum Y, or Y at minimum or maximum X. Selecting **X at maximum Y** together
with **maximum Y** places a text annotation at the global maximum across all
selected layers. Layer filtering, grouping, and aggregation happen before this
inference. Stages evaluate selected reference layers in the background even
when those layers are not visible in that stage.

Enable **Add to legend** per annotation to include it, supply an independent
legend label, or leave it disabled to omit the annotation. The raw annotation
JSON editor remains available under the advanced disclosure. Inferred
annotation coordinates render and download normally but are not currently
available in standalone code export.

With **Live style updates** enabled, presentation-only changes such as colors,
labels, line widths, fonts, axes, annotations, and stage composition trigger a
debounced preview refresh. Query-affecting changes still wait for an explicit
render. Once any query setting changes, live style updates remain paused until
that query has been explicitly rendered successfully; this prevents a partially
configured new layer from launching a raw-data query in the background.
Presentation fields are excluded from query-cache keys, so restyling a rendered
layer reuses its collected frame rather than recalculating it.

## Staged plots

Enable **Create staged plots** to render multiple plot files/previews from one
configuration. **Generate cumulative stages** creates the default sequence:
the first layer, then the first two layers, and so on, followed by annotations.
Earlier layers are marked as overlays so the newest element stands out. Every
stage exposes simple **Include** and **Overlay** checkboxes, and the shared
overlay-opacity control sets how strongly selected overlay layers are faded.
Stages can also be added and composed manually. When a fixed-X anchor is not
visible in a stage, it is still evaluated in the background so that follower
layers retain the same shared X domain.

The legend reserves the union of all entries used by the stage sequence from
the first stage onward. Future handles and labels are transparent placeholders
and uncover in place with their layer or annotation, keeping the legend box at
the same size and position throughout. Staged legends are anchored to the
figure canvas so axis-layout changes cannot move them. For split/grouped layers,
the grouped query may be evaluated early to discover the final legend labels.
Because Matplotlib's `best` location can move as plot data appears, staged plots
pin a `best` legend to `upper right`; select another explicit anchor location
when a different fixed position is preferred.

Stage preview, save, and download operations create filenames such as
`plot-01-Stage-1.png`. The JSON configuration is written once. Standalone code
export currently requires stages to be disabled; use **Download** to retrieve
all staged PNG/PDF files in one archive.

PNG and PDF plots and the complete JSON configuration can be downloaded or
saved to the configured output directory. The JSON includes source paths and
reader settings. Loading it in the browser restores the plot and atomically
replaces the active source catalog with the saved one.

### Former-schema configurations

The loader recognizes the former schema by its `data_sources` and `lines`
fields—the old and current formats both use numeric schema version 2, so the
version number alone is not sufficient. Former time-chart configurations are
migrated before their sources are registered:

- `data_sources` become the current source list; legacy CSV timelines enable
  date parsing for the implicit `scan_date` column;
- `lines` become layers, with `value_column` mapped to Y and `scan_date` to X;
- aggregations, filters, styles, axes, engineering notation, legends, exports,
  annotations, and broken-Y ranges are retained;
- legacy region selector values such as `arin` are normalized to the uppercase
  values stored in the historical summary data;
- `uncover_stages` become current stages with their explicitly listed layers
  and annotations at full opacity.

For legacy `chart.mode: "ranked"`, `chart.rank_label` becomes the categorical
X column and each line's `value_column` remains Y. `scan_date` is applied as a
required input filter, while `sort`, `top_n`, `value_min`, and `value_max` are
applied to the aggregated result. Thus a country ranking groups by country,
aggregates the selected numeric value, sorts the finished categories, and only
then selects the requested top results.

Configuration loading is best-effort. Migration issues are reported with their
field or item location, but valid sections remain editable. Sources are
registered independently; a missing or invalid source does not block other
sources, and layers referring to a failed source are retained but disabled.
Unknown chart modes and non-default ASN-specific preprocessing are reported
instead of silently changing their meaning.

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
reliably into a standalone file. Fixed shared X values are currently rendered
by the application but are not yet supported by standalone code export; use
**Download** for that configuration.

PDF and PostScript Matplotlib output uses font type 42, embedding TrueType fonts
instead of type 3 glyphs. The same setting is included in standalone scripts.

## Lazy execution and cache

Adding a source collects only a 100-row head to infer and cache its schema.
Layer input limits are applied immediately after the lazy source scan and before
filters, grouping, aggregation, or sorting. This makes the limit a bound on raw
input rows rather than a trim of the finished plot. The remaining filters,
projections, grouping, aggregation, and sorting stay in a Polars `LazyFrame`
until a plot is validated, rendered, saved, or downloaded.

**Result limit** is distinct from the input-row limit: it runs after filters,
aggregation, result-Y bounds, and sorting. It is therefore suitable for ranked
top-N plots without truncating the raw input used to compute the ranking.

Collected query results are cached:

- a small in-memory LRU provides immediate repeated renders;
- Arrow IPC files in `.elp-cache/` persist results across server restarts;
- cache keys include the normalized layer query and source fingerprints;
- fingerprints include path, size, and nanosecond modification time for every
  file matched by a source, so changed data invalidates old entries;
- **Clear cache** removes both memory and disk entries.

The cache contains aggregated/projected layer results, not entire input files.
This keeps large raw datasets lazy and makes interactive restyling inexpensive.
Web previews refuse layer results above 1,000,000 plotted rows before they are
cached or passed to Matplotlib. For large sources, select `group_by` or
`group_by_dynamic` with an aggregation, or set an explicit **Input row limit**.
This guard does not limit how many source rows an aggregation may scan; it limits
the number of points produced for the plot. **Clear cache** also removes
temporary cache writes left behind if an earlier server process was interrupted.

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
