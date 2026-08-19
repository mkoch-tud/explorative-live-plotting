from datetime import datetime, timezone
import json
from pathlib import Path

import matplotlib
import matplotlib.colors as mcolors
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import polars as pl

matplotlib.use("Agg")

import explorative_live_plotting.query as query_module
from explorative_live_plotting.cache import QueryCache
from explorative_live_plotting.codegen import generate_script
from explorative_live_plotting.data import DataCatalog, SourceSpec
from explorative_live_plotting.errors import ConfigurationError
from explorative_live_plotting.plotting import (
    STD_COLORS,
    _stage_configs,
    build_figure,
    default_config,
    migrate_legacy_config,
    render_artifacts,
    validate_config,
)
from explorative_live_plotting.query import QueryEngine, validate_layer
from explorative_live_plotting.registry import builtins
from explorative_live_plotting.server import ApplicationState, create_app


def test_legacy_timeline_config_migration() -> None:
    fixture = Path(__file__).parents[1] / "plots" / "asn-and-router-ips-arin-timeline-090826.json"
    legacy = json.loads(fixture.read_text())
    migrated, warnings = migrate_legacy_config(legacy)

    assert migrated["schema_version"] == 2
    assert [item["name"] for item in migrated["sources"]] == ["short", "country", "asn"]
    assert all(item["options"] == {"try_parse_dates": True} for item in migrated["sources"])
    assert [item["x_column"] for item in migrated["layers"]] == ["scan_date", "scan_date"]
    assert [item["y_column"] for item in migrated["layers"]] == [
        "asn",
        "router_ips_with_loops",
    ]
    assert [item["aggregation"] for item in migrated["layers"]] == ["n_unique", "sum"]
    assert migrated["layers"][0]["style"] == {"color": "#3584e4", "marker": "D"}
    assert migrated["layers"][0]["filters"][1] == {
        "column": "looping_subnets",
        "operator": "gt",
        "value": "0",
    }
    assert migrated["layers"][0]["filters"][0]["value"] == "ARIN"
    assert migrated["axes"]["xlabel"] == "Time MM-YY [W]"
    assert migrated["axes"]["y_engineering"] is True
    assert migrated["axes"]["minor_x_ticks"] is True
    assert migrated["legend"]["bbox_enabled"] is True
    assert (migrated["legend"]["bbox_x"], migrated["legend"]["bbox_y"]) == (0.5, 1.02)
    assert migrated["stages"]["enabled"] is True
    assert migrated["stages"]["steps"][1]["elements"] == [
        {"id": "layer:line-4", "overlay": False},
        {"id": "layer:line-1786291768135-2", "overlay": False},
    ]
    assert migrated["broken_y_axis"]["ranges"][1] == {
        "min": 15000.0,
        "max": 28000.0,
    }
    assert warnings == []


def test_legacy_annotations_are_mapped_to_current_coordinates() -> None:
    fixture = Path(__file__).parents[1] / "plots" / "looping-64-per-rir-030826-v2.json"
    legacy = json.loads(fixture.read_text())
    migrated, warnings = migrate_legacy_config(legacy)

    assert warnings == []
    assert migrated["annotations"][1] == {
        "id": "annotation-2",
        "enabled": True,
        "kind": "vline",
        "label": "RIPE92",
        "text": "RIPE92",
        "color": "#808080",
        "text_color": "#808080",
        "alpha": 0.6,
        "fontsize": 8.0,
        "linewidth": 1.0,
        "linestyle": "--",
        "show_in_legend": False,
        "legend_label": "RIPE92",
        "x": "2026-05-18",
        "text_y": 97000000.0,
        "text_x": "2026-05-20T00:00:00",
    }
    validated = validate_config(migrated, builtins())
    assert validated["annotations"][1]["inference"] == {
        "x": "manual",
        "y": "manual",
        "layer_ids": [],
    }


def test_current_config_migration_is_a_noop_copy() -> None:
    current = default_config()
    migrated, warnings = migrate_legacy_config(current)
    assert migrated == current
    assert migrated is not current
    assert warnings == []


def test_legacy_ranked_chart_uses_category_x_and_post_aggregation_limits(
    tmp_path: Path,
) -> None:
    data = tmp_path / "countries.csv"
    pl.DataFrame(
        {
            "scan_date": ["2026-01-01", "2026-01-02", "2026-01-02", "2026-01-02"],
            "country": ["US", "US", "DE", "FR"],
            "packets": [100, 5, 10, 7],
        }
    ).write_csv(data)
    legacy = {
        "schema_version": 2,
        "filename": "country-ranking",
        "data_sources": {"country": str(data)},
        "lines": [
            {
                "id": "countries",
                "source": "country",
                "label": "Packets",
                "value_column": "packets",
                "aggregation": "sum",
                "plot_type": "bar",
            }
        ],
        "chart": {
            "mode": "ranked",
            "rank_label": "country",
            "scan_date": "2026-01-02",
            "sort": "descending",
            "top_n": 2,
            "value_min": 6,
            "value_max": None,
        },
    }
    config, warnings = migrate_legacy_config(legacy)
    assert warnings == []
    layer = config["layers"][0]
    assert layer["x_column"] == "country"
    assert layer["y_column"] == "packets"
    assert layer["sort"] == "y_descending"
    assert layer["result_limit"] == 2
    assert layer["result_y_min"] == 6
    assert layer["required_filters"] == [
        {"column": "scan_date", "operator": "eq", "value": "2026-01-02"}
    ]
    assert layer["filters"] == []

    catalog = DataCatalog()
    source = config["sources"][0]
    catalog.add(SourceSpec(source["name"], source["path"], source["format"], source["options"]))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    frame, validated_layer, _ = engine.execute(layer)
    assert validated_layer["result_limit"] == 2
    assert frame.get_column("_x").to_list() == ["DE", "FR"]
    assert frame.get_column("_y").to_list() == [10, 7]
    validated_config = validate_config(config, registry)
    assert validated_config["axes"]["x_value_ticks"] is True
    figure, _ = build_figure(validated_config, engine, registry)
    tick_labels = [item.get_text() for item in figure.axes[0].get_xticklabels()]
    assert tick_labels == ["DE", "FR"]
    script = generate_script(validated_config, catalog, registry)
    compile(script, "country-ranking.py", "exec")
    assert ".filter(pl.col('_y') >= 6.0)" in script
    assert ".sort('_y', descending=True" in script
    assert ".limit(2)" in script
    assert 'axes.get("x_value_ticks", False)' in script


def test_legacy_migration_keeps_valid_sections_and_reports_invalid_items(
    tmp_path: Path,
) -> None:
    data = tmp_path / "good.csv"
    pl.DataFrame({"scan_date": ["2026-01-01"], "value": [1]}).write_csv(data)
    legacy = {
        "schema_version": 2,
        "data_sources": {"good": str(data), "bad": 42},
        "lines": [
            {
                "id": "good-line",
                "source": "good",
                "value_column": "value",
                "aggregation": "sum",
            },
            "not-an-object",
        ],
        "legend": {"enabled": True, "bbox_to_anchor": [1]},
        "chart": {"mode": "time"},
    }
    config, warnings = migrate_legacy_config(legacy)
    assert [source["name"] for source in config["sources"]] == ["good"]
    assert [layer["id"] for layer in config["layers"]] == ["good-line"]
    assert config["legend"]["enabled"] is True
    assert any("data_sources.bad" in warning for warning in warnings)
    assert any("lines[1]" in warning for warning in warnings)
    assert any("legend.bbox_to_anchor" in warning for warning in warnings)


def test_partial_source_registration_keeps_successful_sources(tmp_path: Path) -> None:
    data = tmp_path / "good.csv"
    pl.DataFrame({"x": [1], "y": [2]}).write_csv(data)
    state = ApplicationState(
        DataCatalog(), builtins(), QueryCache(tmp_path / "cache"), tmp_path / "out"
    )
    client = create_app(state).test_client()
    response = client.post("/api/config-module", json={"module": None, "sources": []})
    assert response.status_code == 200
    good = client.post(
        "/api/sources",
        json={"name": "good", "path": str(data), "format": "auto", "options": {}},
    )
    bad = client.post(
        "/api/sources",
        json={
            "name": "missing",
            "path": str(tmp_path / "missing.csv"),
            "format": "auto",
            "options": {},
        },
    )
    assert good.status_code == 201
    assert bad.status_code == 400
    assert [item["name"] for item in state.catalog.specs()] == ["good"]


def test_lazy_grouped_query_is_cached(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    pl.DataFrame(
        {"category": ["a", "a", "b"], "group": ["x", "y", "x"], "value": [1, 2, 4]}
    ).write_csv(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    layer = {
        "id": "one",
        "source": "sample",
        "plot_type": "bar",
        "x_column": "category",
        "y_column": "value",
        "group_column": "group",
        "aggregation": "sum",
        "filters": [],
        "stacked": True,
    }
    first, _, first_state = engine.execute(layer)
    second, _, second_state = engine.execute(layer)
    restyled, _, restyled_state = engine.execute(
        {**layer, "label": "Restyled", "style": {"color": "#ff0000", "linewidth": 4}}
    )
    assert first.sort(first.columns).equals(second.sort(second.columns))
    assert first.sort(first.columns).equals(restyled.sort(restyled.columns))
    assert first_state == "computed"
    assert second_state == "memory"
    assert restyled_state == "memory"


def test_plot_row_guard_and_temporary_cache_cleanup(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    pl.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]}).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    cache = QueryCache(tmp_path / "cache")
    engine = QueryEngine(catalog, registry, cache)
    layer = {
        "id": "raw",
        "label": "Raw values",
        "source": "sample",
        "plot_type": "line",
        "x_column": "x",
        "y_column": "y",
        "aggregation": "none",
    }
    previous_limit = query_module.MAX_PLOT_ROWS
    query_module.MAX_PLOT_ROWS = 2
    try:
        try:
            engine.execute(layer)
        except ConfigurationError as error:
            assert "more than 2 plot rows" in str(error)
            assert "group_by_dynamic" in str(error)
        else:
            raise AssertionError("oversized raw plot result was accepted")
        assert not list((tmp_path / "cache").glob("*.ipc"))
        limited, _, _ = engine.execute({**layer, "limit": 2})
        assert limited.height == 2
    finally:
        query_module.MAX_PLOT_ROWS = previous_limit

    orphan = tmp_path / "cache" / ".interrupted.tmp"
    orphan.write_bytes(b"partial")
    cache.clear()
    assert not orphan.exists()


def test_generic_mixed_plot(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    pl.DataFrame({"x": [1, 2, 3], "bars": [2, 3, 4], "line": [20, 30, 40]}).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    config = default_config()
    config["layers"] = [
        {
            "id": "bars",
            "source": "sample",
            "plot_type": "bar",
            "x_column": "x",
            "y_column": "bars",
            "aggregation": "sum",
            "filters": [],
            "stacked": True,
        },
        {
            "id": "line",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "line",
            "aggregation": "sum",
            "filters": [],
            "secondary_y": True,
        },
    ]
    config["axes"].update(
        custom_x_ticks=[1, 3],
        custom_x_tick_labels=["first", "last"],
        x_grid=True,
        secondary_y_grid=True,
        grid_alpha=0.2,
    )
    config = validate_config(config, registry)
    figure, states = build_figure(config, engine, registry)
    assert len(figure.axes) == 2
    assert len(figure.axes[0].patches) == 3
    assert len(figure.axes[1].lines) == 1
    assert STD_COLORS == ["#375E97", "#FB6542", "#c1195c", "#37975e"]
    assert mcolors.to_hex(figure.axes[0].patches[0].get_facecolor()) == "#375e97"
    assert mcolors.to_hex(figure.axes[1].lines[0].get_color()) == "#fb6542"
    assert [item.get_text() for item in figure.axes[0].get_xticklabels()] == ["first", "last"]
    assert figure.axes[0].get_axisbelow() is True
    assert figure.axes[1].get_axisbelow() is True
    assert figure.axes[0].xaxis.get_zorder() < figure.axes[0].patches[0].get_zorder()
    assert figure.axes[0].yaxis.get_zorder() < figure.axes[0].patches[0].get_zorder()
    assert figure.axes[1].yaxis.get_zorder() < figure.axes[1].lines[0].get_zorder()
    assert {line.get_alpha() for line in figure.axes[0].get_xgridlines()} == {0.2}
    assert {line.get_alpha() for line in figure.axes[0].get_ygridlines()} == {0.2}
    assert {line.get_alpha() for line in figure.axes[1].get_ygridlines()} == {0.2}
    assert all(item["cache"] == "computed" for item in states)

    engineering_config = default_config()
    engineering_config["axes"].update(
        x_engineering=True,
        y_engineering=True,
        secondary_y_engineering=True,
    )
    engineering_config["layers"] = [config["layers"][1]]
    engineering_figure, _ = build_figure(engineering_config, engine, registry)
    assert isinstance(
        engineering_figure.axes[0].xaxis.get_major_formatter(), mticker.EngFormatter
    )
    assert isinstance(
        engineering_figure.axes[0].yaxis.get_major_formatter(), mticker.EngFormatter
    )
    assert engineering_figure.axes[0].xaxis.get_major_formatter().format_eng(1000) == "1k"
    assert engineering_figure.axes[0].yaxis.get_major_formatter().format_eng(1000) == "1k"
    assert engineering_figure.axes[1].yaxis.get_major_formatter().format_eng(1000) == "1k"
    assert engineering_figure.axes[0].get_xlim() == (1.0, 3.0)


def test_fixed_shared_x_values_filter_and_align_ranked_layers(tmp_path: Path) -> None:
    data = tmp_path / "ranked.parquet"
    pl.DataFrame(
        {
            "country": ["DEU", "DEU", "NLD", "USA", "FRA"],
            "line_value": [60, 40, 90, 80, 70],
            # This layer's independent top two would be USA and FRA.
            "bar_value": [2, 3, 4, 1000, 900],
        }
    ).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("countries", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    anchor = {
        "id": "anchor",
        "label": "Anchor ranking",
        "source": "countries",
        "plot_type": "line",
        "x_column": "country",
        "y_column": "line_value",
        "aggregation": "sum",
        "sort": "y_descending",
        "result_limit": 2,
        "fix_x_values": True,
    }
    follower = {
        "id": "follower",
        "label": "Follower values",
        "source": "countries",
        "plot_type": "bar",
        "x_column": "country",
        "y_column": "bar_value",
        "aggregation": "sum",
        "sort": "y_descending",
        # This must not trim the two values selected by the anchor.
        "result_limit": 1,
    }
    config = default_config()
    config["sources"] = catalog.specs()
    config["layers"] = [anchor, follower]
    config = validate_config(config, registry)

    figure, states = build_figure(config, engine, registry)
    axis = figure.axes[0]
    assert [item.get_text() for item in axis.get_xticklabels()] == ["DEU", "NLD"]
    assert list(axis.lines[0].get_xdata()) == [0, 1]
    assert list(axis.lines[0].get_ydata()) == [100, 90]
    assert [patch.get_x() + patch.get_width() / 2 for patch in axis.patches] == [0, 1]
    assert [patch.get_height() for patch in axis.patches] == [5, 4]
    assert [item["rows"] for item in states] == [2, 2]

    config["stages"] = {
        "enabled": True,
        "overlay_alpha": 0.25,
        "steps": [
            {
                "id": "follower-only",
                "label": "Follower only",
                "elements": [{"id": "layer:follower", "overlay": False}],
            }
        ],
    }
    config = validate_config(config, registry)
    stage = _stage_configs(config)[0][2]
    assert stage["layers"][0]["id"] == "follower"
    assert stage["_fixed_x_anchor_layer"]["id"] == "anchor"
    stage_figure, stage_states = build_figure(stage, engine, registry)
    stage_axis = stage_figure.axes[0]
    assert [item.get_text() for item in stage_axis.get_xticklabels()] == ["DEU", "NLD"]
    assert [patch.get_height() for patch in stage_axis.patches] == [5, 4]
    assert len(stage_states) == 1

    try:
        generate_script(
            {**config, "stages": {**config["stages"], "enabled": False}},
            catalog,
            registry,
        )
    except ConfigurationError as error:
        assert "fixed shared X values" in str(error)
    else:
        raise AssertionError("fixed shared X values were silently exported")


def test_broken_y_axis_draws_shared_panels_without_requerying(tmp_path: Path) -> None:
    data = tmp_path / "broken.parquet"
    pl.DataFrame({"x": [1, 2, 3], "y": [1.0, 10.0, 1.5]}).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    config = default_config()
    config["layers"] = [
        {
            "id": "broken-line",
            "label": "Values",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "y",
            "aggregation": "none",
        }
    ]
    config["broken_y_axis"] = {
        "enabled": True,
        "gap": 1.25,
        "ranges": [{"min": 0, "max": 2}, {"min": 8, "max": 12}],
    }
    config = validate_config(config, registry)
    figure, states = build_figure(config, engine, registry)

    assert len(states) == 1
    assert len(figure.axes) == 2
    upper, lower = figure.axes
    assert upper.get_ylim() == (8.0, 12.0)
    assert lower.get_ylim() == (0.0, 2.0)
    assert upper.get_shared_x_axes().joined(upper, lower)
    assert figure.subplotpars.hspace == 1.25
    assert not upper.spines["bottom"].get_visible()
    assert not lower.spines["top"].get_visible()
    assert upper.get_legend() is not None
    config["sources"] = catalog.specs()
    config["export_formats"] = ["png"]
    script = generate_script(config, catalog, registry)
    compile(script, "broken-plot.py", "exec")
    assert "_broken_axis_marks(primary_axes)" in script
    assert "broken_ranges = [{'min': 0.0, 'max': 2.0}" in script
    assert "fig.subplots_adjust(hspace=1.25)" in script


def test_annotation_inference_legend_and_broken_axis_deduplication(
    tmp_path: Path,
) -> None:
    data = tmp_path / "annotation-data.parquet"
    pl.DataFrame(
        {"x": [1, 2, 3], "first": [1, 5, 3], "second": [2, 20, 4]}
    ).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    config = default_config()
    config["sources"] = catalog.specs()
    config["layers"] = [
        {
            "id": "first",
            "label": "First",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "first",
            "aggregation": "sum",
        },
        {
            "id": "second",
            "label": "Second",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "second",
            "aggregation": "sum",
        },
    ]
    config["broken_y_axis"] = {
        "enabled": True,
        "gap": 0.15,
        "ranges": [{"min": 0, "max": 10}, {"min": 15, "max": 25}],
    }
    config["annotations"] = [
        {
            "id": "global-peak",
            "label": "Peak annotation",
            "kind": "text",
            "text": "Peak",
            "text_background_enabled": True,
            "text_background_color": "#ffeeaa",
            "show_in_legend": True,
            "legend_label": "Global maximum",
            "inference": {
                "x": "x_at_max_y",
                "y": "max_y",
                "layer_ids": ["first", "second"],
            },
        },
        {
            "id": "manual-note",
            "label": "Manual annotation",
            "kind": "text",
            "text": "Manual",
            "x": 1,
            "y": 5,
            "show_in_legend": False,
        },
        {
            "id": "boundary-line",
            "label": "Boundary line",
            "kind": "vline",
            "text": "Boundary label",
            "x": 2,
            "text_x": 2,
            "text_y": 15,
            "show_in_legend": False,
        },
    ]
    config = validate_config(config, registry)
    figure, _ = build_figure(config, engine, registry)

    peak_artists = [
        text
        for text in figure.texts
        if text.get_text() == "Peak"
    ]
    manual_artists = [
        text
        for text in figure.texts
        if text.get_text() == "Manual"
    ]
    assert len(peak_artists) == 1
    assert peak_artists[0].get_position() == (2, 20)
    assert peak_artists[0].get_clip_on() is False
    assert peak_artists[0].get_zorder() == 1000
    assert peak_artists[0].figure is figure
    assert mcolors.to_hex(peak_artists[0].get_bbox_patch().get_facecolor()) == "#ffeeaa"
    assert len(manual_artists) == 1
    boundary_artists = [
        text for text in figure.texts if text.get_text() == "Boundary label"
    ]
    assert len(boundary_artists) == 1
    assert boundary_artists[0].get_position() == (2, 15)
    assert boundary_artists[0].get_clip_on() is False
    assert boundary_artists[0].get_zorder() == 1000
    legend_labels = [text.get_text() for text in figure.axes[0].get_legend().get_texts()]
    assert "Global maximum" in legend_labels
    assert "Manual annotation" not in legend_labels

    try:
        generate_script(config, catalog, registry)
    except ConfigurationError as error:
        assert "inferred annotation coordinates" in str(error)
    else:
        raise AssertionError("inferred annotation coordinates were silently exported")

    config["stages"] = {
        "enabled": True,
        "overlay_alpha": 0.25,
        "steps": [
            {
                "id": "peak-only",
                "label": "Peak only",
                "elements": [
                    {"id": "layer:first", "overlay": False},
                    {"id": "annotation:global-peak", "overlay": False},
                ],
            }
        ],
    }
    config = validate_config(config, registry)
    stage = _stage_configs(config)[0][2]
    assert [layer["id"] for layer in stage["_annotation_reference_layers"]] == [
        "second"
    ]
    stage_figure, _ = build_figure(stage, engine, registry)
    stage_peak = next(
        text
        for text in stage_figure.texts
        if text.get_text() == "Peak"
    )
    assert stage_peak.get_position() == (2, 20)


def test_standard_color_cycle(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    pl.DataFrame({"x": [1, 2], "y": [3, 4]}).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    config = default_config()
    config["layers"] = [
        {
            "id": f"line-{index}",
            "label": f"Line {index}",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "y",
            "aggregation": "sum",
        }
        for index in range(5)
    ]
    figure, _ = build_figure(config, engine, registry)
    assert [mcolors.to_hex(line.get_color()) for line in figure.axes[0].lines] == [
        "#375e97",
        "#fb6542",
        "#c1195c",
        "#37975e",
        "#375e97",
    ]


def test_builtin_json_options_are_validated_and_exported(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    pl.DataFrame({"x": [1, 2], "y": [3, 4]}).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    step = {
        "id": "step",
        "label": "Step",
        "source": "sample",
        "plot_type": "step",
        "x_column": "x",
        "y_column": "y",
        "aggregation": "none",
        "options": {"where": "pre"},
    }
    assert validate_layer(step, catalog, registry)["options"] == {"where": "pre"}
    config = default_config()
    config["sources"] = catalog.specs()
    config["layers"] = [step]
    figure, _ = build_figure(config, engine, registry)
    assert figure.axes[0].lines[0].get_drawstyle() == "steps-pre"
    script = generate_script(config, catalog, registry)
    assert "where='pre'" in script

    invalid_quantile = {
        **step,
        "plot_type": "line",
        "aggregation": "quantile",
        "aggregation_options": {"quantile": 1.5},
    }
    try:
        validate_layer(invalid_quantile, catalog, registry)
    except ConfigurationError as error:
        assert "between 0 and 1" in str(error)
    else:
        raise AssertionError("invalid quantile was accepted")


def test_http_source_and_render_round_trip(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    pl.DataFrame({"x": [1, 2], "y": [3, 4]}).write_csv(data)
    catalog = DataCatalog()
    registry = builtins()
    state = ApplicationState(catalog, registry, QueryCache(tmp_path / "cache"), tmp_path / "out")
    client = create_app(state).test_client()
    response = client.post(
        "/api/sources",
        json={"name": "sample", "path": str(data), "format": "auto", "options": {}},
    )
    assert response.status_code == 201
    config = default_config()
    config["sources"] = [response.get_json()]
    config["layers"] = [
        {
            "id": "line",
            "source": "sample",
            "plot_type": "scatter",
            "x_column": "x",
            "y_column": "y",
            "aggregation": "none",
            "filters": [],
        }
    ]
    response = client.post("/api/render", json=config)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["previews"][0]["image"].startswith("data:image/png;base64,")
    assert payload["config"]["sources"][0]["name"] == "sample"
    config["layers"][0]["label"] = "Restyled"
    config["layers"][0]["style"] = {"color": "#ff0000", "linewidth": 3}
    response = client.post("/api/render", json=config)
    assert response.status_code == 200
    assert response.get_json()["cache"][0]["cache"] == "memory"
    response = client.post("/api/save", json=config)
    assert response.status_code == 200
    saved = response.get_json()
    assert saved["previews"][0]["image"].startswith("data:image/png;base64,")
    assert Path(saved["files"][0]).is_file()
    response = client.post("/api/download", json=config)
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment;")


def test_path_autocomplete_and_system_stats(tmp_path: Path, monkeypatch) -> None:
    data_directory = tmp_path / "measurements"
    data_directory.mkdir()
    data = data_directory / "packets.parquet"
    pl.DataFrame({"x": [1]}).write_parquet(data)
    module = tmp_path / "test_paths.py"
    module.write_text(f"from pathlib import Path\nDATA_PATH = Path({str(tmp_path)!r})\n")
    monkeypatch.chdir(tmp_path)

    catalog = DataCatalog("test_paths")
    registry = builtins()
    state = ApplicationState(catalog, registry, QueryCache(tmp_path / "cache"), tmp_path / "out")
    client = create_app(state).test_client()

    response = client.get("/api/path-suggestions", query_string={"value": "${DAT"})
    assert response.status_code == 200
    assert response.get_json()["suggestions"][0]["value"] == "${DATA_PATH}/"
    response = client.get("/api/path-suggestions", query_string={"value": "$"})
    assert response.get_json()["suggestions"][0]["value"] == "${DATA_PATH}/"

    response = client.get("/api/path-suggestions", query_string={"value": "${DATA_PATH}"})
    result = response.get_json()
    assert result["resolved_path"] == str(tmp_path)
    assert result["suggestions"][0]["value"] == "${DATA_PATH}/"

    response = client.get(
        "/api/path-suggestions", query_string={"value": "${DATA_PATH}/measurements/pa"}
    )
    result = response.get_json()
    assert result["resolved_path"] == str(data_directory / "pa")
    assert result["suggestions"][0]["value"] == "${DATA_PATH}/measurements/packets.parquet"

    monkeypatch.chdir(data_directory)
    response = client.get("/api/path-suggestions", query_string={"value": "pa"})
    result = response.get_json()
    assert result["resolved_path"] == str(data_directory / "pa")
    assert result["suggestions"][0]["value"] == "packets.parquet"

    response = client.get("/api/system-stats")
    assert response.status_code == 200
    stats = response.get_json()
    assert set(("cpu_percent", "ram_percent", "ram_used_bytes", "ram_total_bytes")) <= stats.keys()
    if stats["cpu_percent"] is not None:
        assert 0 <= stats["cpu_percent"] <= 100
    if stats["ram_percent"] is not None:
        assert 0 <= stats["ram_percent"] <= 100
    page = client.get("/").get_data(as_text=True)
    assert 'id="show-system-stats"' in page
    assert 'id="source-path-suggestions"' in page
    assert 'id="broken-y-enabled"' in page
    assert 'id="broken-y-ranges"' in page
    assert 'id="x-value-ticks"' in page
    assert 'id="x-datetime-format"' in page
    assert 'id="grid-opacity"' in page
    assert 'id="config-panel"' in page
    assert 'id="panel-resizer"' in page


def test_time_bins_absolute_and_relative_counts(tmp_path: Path) -> None:
    data = tmp_path / "packets.parquet"
    pl.DataFrame(
        {
            "timestamp": [
                datetime(2026, 4, 1, 0, 0, 1, tzinfo=timezone.utc),
                datetime(2026, 4, 1, 0, 0, 20, tzinfo=timezone.utc),
                datetime(2026, 4, 1, 0, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 4, 1, 0, 1, 30, tzinfo=timezone.utc),
            ],
            "ttl": [235, 48, 49, 241],
        }
    ).write_parquet(data)
    catalog = DataCatalog()
    catalog.add(SourceSpec("packets", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    base = {
        "source": "packets",
        "plot_type": "line",
        "x_column": "timestamp",
        "y_column": None,
        "time_bin": "1m",
        "sort": "x_ascending",
    }

    total, _, _ = engine.execute({**base, "id": "total", "aggregation": "count"})
    matching, _, _ = engine.execute(
        {
            **base,
            "id": "matching",
            "aggregation": "count",
            "filters": [{"column": "ttl", "operator": "gt", "value": 200}],
        }
    )
    relative, layer, _ = engine.execute(
        {
            **base,
            "id": "relative",
            "aggregation": "relative_count",
            "aggregation_options": {"scale": "percent"},
            "filters": [{"column": "ttl", "operator": "gt", "value": 200}],
        }
    )

    assert total.get_column("_y").to_list() == [2, 2]
    assert matching.get_column("_y").to_list() == [1, 1]
    assert relative.get_column("_y").to_list() == [50.0, 50.0]
    assert layer["time_bin"] == "1m"

    limited, _, _ = engine.execute(
        {
            **base,
            "id": "limited",
            "aggregation": "relative_count",
            "limit": 2,
            "sort": "x_descending",
            "filters": [{"column": "ttl", "operator": "gt", "value": 200}],
        }
    )
    assert limited.height == 1
    assert limited.get_column("_y").to_list() == [0.5]
    assert limited.get_column("_x")[0].minute == 0

    figure_config = default_config()
    figure_config["axes"]["y_engineering"] = True
    figure_config["layers"] = [{**base, "id": "total", "aggregation": "count"}]
    figure, states = build_figure(figure_config, engine, registry)
    assert isinstance(figure.axes[0].xaxis.get_major_formatter(), mdates.ConciseDateFormatter)
    assert isinstance(figure.axes[0].yaxis.get_major_formatter(), mticker.EngFormatter)
    assert states[0]["grouping"] == "group_by_dynamic"
    assert states[0]["aggregation"] == "count"
    assert states[0]["every"] == "1m"
    assert states[0]["rows"] == 2

    single_bin_config = default_config()
    single_bin_config["layers"] = [
        {**base, "id": "single", "aggregation": "count", "limit": 2}
    ]
    single_bin_figure, _ = build_figure(single_bin_config, engine, registry)
    lower, upper = single_bin_figure.axes[0].get_xlim()
    assert upper - lower <= 1 / 1440 + 1e-9
    assert lower < mdates.date2num(datetime(2026, 4, 1, tzinfo=timezone.utc)) < upper

    custom_date_config = default_config()
    custom_date_config["axes"]["x_datetime_format"] = "%b %d, %Y %H:%M"
    custom_date_config["layers"] = [
        {**base, "id": "dated", "aggregation": "count"}
    ]
    custom_date_config["annotations"] = [
        {
            "id": "dated-note",
            "label": "Datetime note",
            "kind": "text",
            "text": "Capture start",
            "x": "2026-04-01T00:00:00+00:00",
            "y": 1,
        }
    ]
    custom_date_config = validate_config(custom_date_config, registry)
    custom_date_figure, _ = build_figure(custom_date_config, engine, registry)
    date_formatter = custom_date_figure.axes[0].xaxis.get_major_formatter()
    assert isinstance(date_formatter, mdates.DateFormatter)
    assert date_formatter(
        mdates.date2num(datetime(2026, 4, 1, tzinfo=timezone.utc))
    ) == "Apr 01, 2026 00:00"
    dated_note = next(
        text
        for text in custom_date_figure.texts
        if text.get_text() == "Capture start"
    )
    assert dated_note.get_position() == (
        mdates.date2num(datetime(2026, 4, 1, tzinfo=timezone.utc)),
        1,
    )
    assert dated_note.get_zorder() == 1000

    config = default_config()
    config["axes"]["x_datetime_format"] = "%b %d, %Y"
    config["sources"] = catalog.specs()
    config["layers"] = [{**base, "id": "relative", "aggregation": "relative_count"}]
    script = generate_script(config, catalog, registry)
    compile(script, "plot.py", "exec")
    assert ".dt.truncate('1m')" in script
    assert "_denominator" in script
    assert "mdates.ConciseDateFormatter" in script
    assert "'%b %d, %Y'" in script
    assert "_automatic_x_limits" in script
    assert "mticker.EngFormatter(sep='')" in script
    assert "plot_axis.set_axisbelow(True)" in script
    assert 'STD_COLORS = ["#375E97", "#FB6542", "#c1195c", "#37975e"]' in script


def test_fonts_structured_annotations_and_stages(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    pl.DataFrame({"x": [1, 2, 3], "first": [1, 2, 3], "second": [3, 2, 1]}).write_parquet(
        data
    )
    catalog = DataCatalog()
    catalog.add(SourceSpec("sample", str(data)))
    registry = builtins()
    engine = QueryEngine(catalog, registry, QueryCache(tmp_path / "cache"))
    config = default_config()
    config["sources"] = catalog.specs()
    config["figure"]["font_size"] = 11
    config["axes"].update(
        xlabel="Packets",
        label_font_size_override=True,
        label_font_size=15,
        tick_font_size_override=True,
        tick_font_size=13,
    )
    config["legend"].update(
        font_size_override=True,
        font_size=12,
        loc="upper left",
        ncols=2,
        bbox_enabled=True,
        bbox_x=0.25,
        bbox_y=0.75,
        handlelength=4,
        opacity=0.4,
    )
    config["layers"] = [
        {
            "id": "first",
            "label": "First",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "first",
            "aggregation": "sum",
            "style": {"alpha": 0.8},
        },
        {
            "id": "second",
            "label": "Second",
            "source": "sample",
            "plot_type": "line",
            "x_column": "x",
            "y_column": "second",
            "aggregation": "sum",
        },
    ]
    config["annotations"] = [
        {
            "id": "note",
            "kind": "text",
            "text": "Look here",
            "x": 2,
            "y": 2,
            "fontsize": 17,
        }
    ]
    config = validate_config(config, registry)
    figure, _ = build_figure(config, engine, registry)
    assert figure.axes[0].xaxis.label.get_fontsize() == 15
    assert figure.axes[0].get_xticklabels()[0].get_fontsize() == 13
    legend = figure.axes[0].get_legend()
    assert legend.get_texts()[0].get_fontsize() == 12
    assert legend._ncols == 2
    assert legend.handlelength == 4
    assert legend.get_frame().get_alpha() == 0.4
    assert legend.get_bbox_to_anchor()._bbox.bounds == (0.25, 0.75, 0.0, 0.0)
    annotation_text = next(
        text for text in figure.texts if text.get_text() == "Look here"
    )
    assert annotation_text.get_fontsize() == 17
    script = generate_script(config, catalog, registry)
    compile(script, "annotation-plot.py", "exec")
    assert "_foreground_text(plot_axis" in script
    assert "clip_on=False, zorder=1000" in script

    config["stages"]["enabled"] = True
    config = validate_config(config, registry)
    assert len(config["stages"]["steps"]) == 3
    views = _stage_configs(config)
    assert len(views) == 3
    assert views[1][2]["layers"][0]["style"]["alpha"] == 0.2
    artifacts, states = render_artifacts(config, engine, registry, ["png", "json"])
    assert len([name for name in artifacts if name.endswith(".png")]) == 3
    assert f"{config['filename']}.json" in artifacts
    assert any(item.get("stage") == "Stage 3" for item in states)
