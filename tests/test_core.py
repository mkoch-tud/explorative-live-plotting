from pathlib import Path

import matplotlib
import polars as pl

matplotlib.use("Agg")

from explorative_live_plotting.cache import QueryCache
from explorative_live_plotting.data import DataCatalog, SourceSpec
from explorative_live_plotting.plotting import build_figure, default_config, validate_config
from explorative_live_plotting.query import QueryEngine
from explorative_live_plotting.registry import builtins
from explorative_live_plotting.server import ApplicationState, create_app


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
    assert first.sort(first.columns).equals(second.sort(second.columns))
    assert first_state == "computed"
    assert second_state == "memory"


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
    config["axes"].update(custom_x_ticks=[1, 3], custom_x_tick_labels=["first", "last"])
    config = validate_config(config, registry)
    figure, states = build_figure(config, engine, registry)
    assert len(figure.axes) == 2
    assert len(figure.axes[0].patches) == 3
    assert len(figure.axes[1].lines) == 1
    assert [item.get_text() for item in figure.axes[0].get_xticklabels()] == ["first", "last"]
    assert all(item["cache"] == "computed" for item in states)


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
