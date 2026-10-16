"""Grid layout and shared-axis helpers for figures containing multiple plots."""

from copy import deepcopy

from .errors import ConfigurationError


def normalize_plots(raw):
    if not isinstance(raw, list) or not raw:
        raise ConfigurationError("plots must be a non-empty array")
    plots, ids, cells = [], set(), set()
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            raise ConfigurationError("each plot must be an object")
        plot = deepcopy(value)
        plot["id"] = str(plot.get("id") or f"plot-{index + 1}")
        plot["label"] = str(plot.get("label") or f"Plot {index + 1}")
        plot["title"] = str(plot.get("title") or "")
        plot.setdefault("row", index)
        plot.setdefault("column", 0)
        for field in ("row", "column"):
            if (
                isinstance(plot[field], bool)
                or not isinstance(plot[field], int)
                or not 0 <= plot[field] < 50
            ):
                raise ConfigurationError(f"plot {field} must be an integer between 0 and 49")
        cell = (plot["row"], plot["column"])
        if plot["id"] in ids:
            raise ConfigurationError("plot IDs must be unique")
        if cell in cells:
            raise ConfigurationError("plots cannot occupy the same grid cell")
        ids.add(plot["id"])
        cells.add(cell)
        for field in ("show_x_axis", "show_y_axis"):
            plot[field] = bool(plot.get(field, True))
        for field in ("share_x", "share_y"):
            plot[field] = plot.get(field) or None
            if plot[field] is not None and not isinstance(plot[field], str):
                raise ConfigurationError(f"{field} must reference another plot")
        plots.append(plot)
    for plot in plots:
        for field in ("share_x", "share_y"):
            if plot[field] and (plot[field] not in ids or plot[field] == plot["id"]):
                raise ConfigurationError(f"{field} must reference another plot")
    return plots


def plot_config(config, plot):
    """Project a whole-figure configuration onto one plot."""
    result = {**config, "plots": []}
    for section in ("axes", "legend", "broken_y_axis"):
        result[section] = {**config[section], **plot.get(section, {})}
    default_id = config["plots"][0]["id"]
    for section in (
        "layers",
        "annotations",
        "_stage_legend_layers",
        "_stage_legend_annotations",
        "_annotation_reference_layers",
    ):
        if section in config:
            result[section] = [
                item for item in config[section] if item.get("plot_id", default_id) == plot["id"]
            ]
    anchors = config.get("_fixed_x_anchor_layers", {})
    if plot["id"] in anchors:
        result["_fixed_x_anchor_layer"] = anchors[plot["id"]]
    return result


def finish_plot_grid(fig, plot_axes, plots, reference=None):
    """Link axis groups, fix stage geometry, and apply per-plot visibility."""
    if reference:
        fig.set_layout_engine(None)
        for plot in plots:
            for axis, position, limits in zip(
                plot_axes[plot["id"]],
                reference["plots"][plot["id"]]["axes"],
                reference["plots"][plot["id"]]["limits"],
                strict=True,
            ):
                axis.set_position(position)
                axis.set_xlim(limits[0])
                axis.set_ylim(limits[1])
    for dimension in ("x", "y"):
        groups = {plot["id"]: {plot["id"]} for plot in plots}
        for plot in plots:
            target = plot.get(f"share_{dimension}")
            if target:
                merged = groups[plot["id"]] | groups[target]
                for identifier in merged:
                    groups[identifier] = merged
        seen = set()
        for plot in plots:
            identifiers = groups[plot["id"]]
            if plot["id"] in seen or len(identifiers) < 2:
                continue
            seen.update(identifiers)
            axes = [plot_axes[item["id"]][0] for item in plots if item["id"] in identifiers]
            bounds = [getattr(axis, f"get_{dimension}lim")() for axis in axes]
            lower, upper = min(min(pair) for pair in bounds), max(max(pair) for pair in bounds)
            if bounds[0][0] > bounds[0][1]:
                lower, upper = upper, lower
            for axis in axes[1:]:
                getattr(axis, f"share{dimension}")(axes[0])
            getattr(axes[0], f"set_{dimension}lim")(lower, upper)
    for plot in plots:
        for axis in plot_axes[plot["id"]]:
            axis.set_visible(plot.get("_visible", True))
            for dimension, spines in (("x", ("bottom", "top")), ("y", ("left", "right"))):
                if not plot.get(f"show_{dimension}_axis", True):
                    getattr(axis, f"{dimension}axis").set_visible(False)
                    for spine in spines:
                        axis.spines[spine].set_visible(False)
    if not reference:
        # Keep the default legend above the title even in short grid cells.
        # Constrained layout then reserves the additional space for every plot.
        for _ in range(2):
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            for plot in plots:
                options = plot.get("legend", {})
                if not (
                    options.get("enabled", True)
                    and options.get("loc") == "upper center"
                    and options.get("bbox_enabled")
                    and options.get("bbox_x") == 0.5
                    and options.get("bbox_y") == 1.2
                ):
                    continue
                axis = next((axis for axis in plot_axes[plot["id"]] if axis.get_legend()), None)
                if axis is None:
                    continue
                legend = axis.get_legend()
                bounds = axis.get_window_extent(renderer)
                top = max(bounds.y1, axis.title.get_window_extent(renderer).y1)
                rise = max(0, top + fig.dpi * 4 / 72 - legend.get_window_extent(renderer).y0)
                if rise:
                    current = axis.transAxes.inverted().transform(legend.get_bbox_to_anchor().p0)
                    legend.set_bbox_to_anchor(
                        (0.5, current[1] + rise / bounds.height), transform=axis.transAxes
                    )
