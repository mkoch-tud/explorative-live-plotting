"""Axis labels positioned relative to a whole plot, including broken axes."""

import matplotlib.transforms as mtransforms


class PlotYLabelTransform(mtransforms.Transform):
    """Keep automatic label X placement and span all of a plot's Y panels."""

    input_dims = output_dims = 2
    is_separable = True

    def __init__(self, axes):
        super().__init__()
        self._plot_axes = tuple(axes)

    def transform_non_affine(self, values):
        # Use current display bounds so layout, resizing, DPI changes, and
        # frozen stage positions all keep the label centered without callbacks.
        lower = min(axis.bbox.y0 for axis in self._plot_axes)
        upper = max(axis.bbox.y1 for axis in self._plot_axes)
        result = values.astype(float, copy=True)
        result[:, 1] = lower + values[:, 1] * (upper - lower)
        return result


class PlotYAxisLayout:
    """Reserve label width without reserving a gap above its owning panel."""

    def __init__(self, get_tightbbox, label, panel_transform):
        self._get_tightbbox = get_tightbbox
        self._label = label
        self._panel_transform = panel_transform

    def __call__(self, renderer=None, *, for_layout_only=False):
        if not for_layout_only:
            return self._get_tightbbox(renderer)
        transform = self._label.get_transform()
        # Constrained layout measures decorations relative to each panel. A
        # label in the middle of the entire plot could otherwise inflate the
        # gap above the bottom panel until the data panels collapse.
        self._label.set_transform(self._panel_transform)
        try:
            return self._get_tightbbox(renderer, for_layout_only=True)
        finally:
            self._label.set_transform(transform)


def set_plot_ylabel(fig, axes, text, fontsize):
    """Use the same label placement for ordinary and broken Y axes."""
    primary = axes[-1]
    label = primary.set_ylabel(text, fontsize=fontsize)
    if len(axes) > 1:
        primary.yaxis.get_tightbbox = PlotYAxisLayout(
            primary.yaxis.get_tightbbox, label, label.get_transform()
        )
    label.set_transform(PlotYLabelTransform(axes))
    label.set_y(0.5)
    # Leave room for the widest tick labels across all broken-axis panels.
    fig.align_ylabels(axes)
    return label
