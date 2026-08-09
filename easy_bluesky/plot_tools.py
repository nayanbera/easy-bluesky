"""plot_tools.py — Shared pyqtgraph helpers: crosshair, point tooltip."""

try:
    import pyqtgraph as pg
    import numpy as np
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

from PyQt6.QtCore import Qt, QObject, QEvent


class _LeaveFilter(QObject):
    """Event filter that hides crosshair items when mouse leaves the viewport."""
    def __init__(self, items, label, parent=None):
        super().__init__(parent)
        self._items = items
        self._label = label

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Leave:
            for item in self._items:
                item.hide()
            self._label.setText("")
        return False


_TOOLTIP_PX = 18   # pixel-space threshold for point-hover tooltip


def setup_crosshair(plot_widget, coord_label, get_curves_fn=None):
    """Attach crosshair lines + point-hover tooltip to a pyqtgraph PlotWidget.

    coord_label : QLabel updated with (X, Y) while mouse is over the plot.
    get_curves_fn : callable → dict {name: PlotDataItem}; used for tooltip.

    Returns a cleanup callable, or None when pyqtgraph is unavailable.

    NOTE: Callers must NOT call plot_widget.clear() — it removes the crosshair
    items. Instead remove individual PlotDataItems and call legend.clear().
    """
    if not PG_AVAILABLE:
        return None

    # Bright yellow, fully opaque, dashed
    xpen = pg.mkPen(color=(255, 220, 30, 230), width=1,
                    style=Qt.PenStyle.DashLine)
    vline   = pg.InfiniteLine(angle=90, movable=False, pen=xpen)
    hline   = pg.InfiniteLine(angle=0,  movable=False, pen=xpen)
    tooltip = pg.TextItem(color=(255, 220, 30), anchor=(0, 1))
    tooltip.setZValue(100)

    for item in (vline, hline, tooltip):
        plot_widget.addItem(item, ignoreBounds=True)
        item.hide()

    def on_mouse_moved(pos):
        if not plot_widget.sceneBoundingRect().contains(pos):
            vline.hide(); hline.hide(); tooltip.hide()
            coord_label.setText("")
            return

        vb  = plot_widget.getPlotItem().vb
        mp  = vb.mapSceneToView(pos)
        x, y = mp.x(), mp.y()

        vline.setPos(x); vline.show()
        hline.setPos(y); hline.show()
        coord_label.setText(f"  X: {x:.5g}   Y: {y:.5g}")

        if get_curves_fn is None:
            tooltip.hide()
            return

        curves = get_curves_fn()
        if not curves:
            tooltip.hide()
            return

        try:
            # Pixel-space distance using viewPixelSize
            px_size = vb.viewPixelSize()   # (x_units_per_pixel, y_units_per_pixel)
            if px_size[0] == 0 or px_size[1] == 0:
                tooltip.hide()
                return
            x_px_scale = 1.0 / px_size[0]   # pixels per x-unit
            y_px_scale = 1.0 / px_size[1]   # pixels per y-unit

            best_d_px = float("inf")
            best      = None

            for sig, curve in curves.items():
                if not hasattr(curve, "getData"):
                    continue
                xd, yd = curve.getData()
                if xd is None or len(xd) == 0:
                    continue
                # Subsample for performance on large datasets
                if len(xd) > 800:
                    step = len(xd) // 800
                    xd = xd[::step]; yd = yd[::step]
                dx_px = (xd - x) * x_px_scale
                dy_px = (yd - y) * y_px_scale
                d_px  = dx_px**2 + dy_px**2
                idx   = int(d_px.argmin())
                d     = float(d_px[idx]) ** 0.5
                if d < _TOOLTIP_PX and d < best_d_px:
                    best_d_px = d
                    best = (sig, float(xd[idx]), float(yd[idx]))

            if best:
                sig, px, py = best
                tooltip.setText(f"{sig}\n({px:.5g}, {py:.5g})")
                tooltip.setPos(px, py)
                tooltip.show()
            else:
                tooltip.hide()
        except Exception:
            tooltip.hide()

    plot_widget.scene().sigMouseMoved.connect(on_mouse_moved)

    # Install a Leave-event filter on the viewport so both lines hide
    # reliably when the mouse exits regardless of exit direction.
    _leave_filter = _LeaveFilter((vline, hline, tooltip), coord_label)
    plot_widget.viewport().installEventFilter(_leave_filter)

    def cleanup():
        try:
            plot_widget.scene().sigMouseMoved.disconnect(on_mouse_moved)
            plot_widget.viewport().removeEventFilter(_leave_filter)
            for item in (vline, hline, tooltip):
                plot_widget.removeItem(item)
        except Exception:
            pass

    # Keep a reference so the filter isn't garbage-collected
    cleanup._leave_filter = _leave_filter
    return cleanup


def smart_legend_position(plot_widget):
    """Move the legend to the quadrant with the least data density.

    Call after all curves are plotted.  Accepts a PlotWidget or PlotItem.
    Legends in pyqtgraph 0.12+ are already draggable — the user can override
    the automatic position by clicking and dragging it anywhere on the plot.
    """
    if not PG_AVAILABLE:
        return
    pi = plot_widget.getPlotItem() if hasattr(plot_widget, 'getPlotItem') else plot_widget
    legend = getattr(pi, 'legend', None)
    if legend is None or not pi.curves:
        return

    vr = pi.viewRange()
    if not vr or len(vr) < 2:
        return
    x_min, x_max = vr[0]
    y_min, y_max = vr[1]
    if x_max == x_min or y_max == y_min:
        return
    x_mid = (x_min + x_max) / 2.0
    y_mid = (y_min + y_max) / 2.0

    # Count data points in each corner quadrant: [TL, TR, BL, BR]
    scores = np.zeros(4, dtype=int)
    for curve in pi.curves:
        try:
            xd, yd = curve.getData()
        except Exception:
            continue
        if xd is None or len(xd) == 0:
            continue
        xd = np.asarray(xd, dtype=float)
        yd = np.asarray(yd, dtype=float)
        if len(xd) > 500:
            step = max(1, len(xd) // 500)
            xd, yd = xd[::step], yd[::step]
        left = xd < x_mid
        top  = yd >= y_mid
        scores[0] += int(np.sum( left &  top))   # top-left
        scores[1] += int(np.sum(~left &  top))   # top-right
        scores[2] += int(np.sum( left & ~top))   # bottom-left
        scores[3] += int(np.sum(~left & ~top))   # bottom-right

    # setOffset sign convention: positive → from left/top, negative → from right/bottom
    _offsets = [(10, 10), (-10, 10), (10, -10), (-10, -10)]
    legend.setOffset(_offsets[int(np.argmin(scores))])
