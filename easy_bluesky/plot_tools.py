"""plot_tools.py — Shared pyqtgraph helpers: crosshair, point tooltip, 2D map."""

try:
    import pyqtgraph as pg
    import numpy as np
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox, QPushButton,
)
from PyQt6.QtCore import Qt, QObject, QEvent, QRectF, pyqtSignal


_COORD_PLACEHOLDER = "X: —        Y: —"


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
            self._label.setText(_COORD_PLACEHOLDER)
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

    coord_label.setText(_COORD_PLACEHOLDER)

    def on_mouse_moved(pos):
        if not plot_widget.sceneBoundingRect().contains(pos):
            vline.hide(); hline.hide(); tooltip.hide()
            coord_label.setText(_COORD_PLACEHOLDER)
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


# ── 2D map helpers ─────────────────────────────────────────────────────────────

def build_2d_map(xs, ys, zs):
    """Convert flat (x, y, z) triplets to a 2D image array.

    Returns (img[ny, nx], x_unique, y_unique).
    img[iy, ix] = z value at grid position (x_unique[ix], y_unique[iy]).
    Missing pixels are NaN.  Raises ValueError if data is insufficient.
    """
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    zs = np.asarray(zs, dtype=float)
    n  = min(len(xs), len(ys), len(zs))
    if n < 2:
        raise ValueError("Not enough points for a 2D map (need ≥ 2)")
    xs, ys, zs = xs[:n], ys[:n], zs[:n]

    def _unique_tol(arr):
        s   = np.sort(arr)
        tol = max(abs(float(s[-1]) - float(s[0])) * 1e-4, 1e-10)
        u   = [float(s[0])]
        for v in s[1:]:
            if abs(float(v) - u[-1]) > tol:
                u.append(float(v))
        return np.array(u)

    x_u = _unique_tol(xs)
    y_u = _unique_tol(ys)
    nx, ny = len(x_u), len(y_u)
    if nx < 2:
        raise ValueError("Need ≥ 2 unique X values for a 2D map")

    img   = np.full((ny, nx), np.nan)
    x_tol = max((x_u[-1] - x_u[0]) * 5e-4, 1e-10)
    y_tol = max((y_u[-1] - y_u[0]) * 5e-4, 1e-10)

    for xi, yi, zi in zip(xs, ys, zs):
        ix = int(np.searchsorted(x_u, xi))
        iy = int(np.searchsorted(y_u, yi))
        ix = max(0, min(ix, nx - 1))
        iy = max(0, min(iy, ny - 1))
        if ix > 0 and abs(x_u[ix] - xi) > abs(x_u[ix - 1] - xi):
            ix -= 1
        if iy > 0 and abs(y_u[iy] - yi) > abs(y_u[iy - 1] - yi):
            iy -= 1
        if abs(x_u[ix] - xi) <= x_tol and abs(y_u[iy] - yi) <= y_tol:
            img[iy, ix] = zi

    return img, x_u, y_u


class TwoDMapWidget(QWidget):
    """Reusable 2D heatmap (pixel-map) widget backed by pyqtgraph ImageItem.

    Usage::
        w = TwoDMapWidget()
        w.set_columns(cols, x_col, motors, detectors)   # populate combos
        w.selection_changed.connect(my_replot_slot)      # replot when Y/Z change
        w.replot(xs, ys, zs, x_label, y_label, z_label) # draw
    """

    selection_changed = pyqtSignal()

    _CMAPS     = ['viridis', 'inferno', 'plasma', 'coolwarm', 'gray']
    _NAN_COLOR = np.array([60, 60, 60], dtype=np.uint8)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_cmap  = None
        self._hist          = None   # pg.HistogramLUTItem (replaces static colorbar)
        self._scan_x_range  = None   # (min, max) from plan parameters
        self._scan_y_range  = None
        self._last_raw      = None   # (xs, ys, zs, x_label, y_label) from last replot
        self._cl_overlay    = None   # pg.PlotDataItem for centerline overlay
        self._build()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)

        ctrl.addWidget(QLabel("Y motor:"))
        self._y_combo = QComboBox()
        self._y_combo.setMinimumWidth(120)
        self._y_combo.setMaximumWidth(220)
        self._y_combo.currentTextChanged.connect(self.selection_changed)
        ctrl.addWidget(self._y_combo)

        ctrl.addSpacing(8)
        ctrl.addWidget(QLabel("Z (intensity):"))
        self._z_combo = QComboBox()
        self._z_combo.setMinimumWidth(120)
        self._z_combo.setMaximumWidth(220)
        self._z_combo.currentTextChanged.connect(self.selection_changed)
        ctrl.addWidget(self._z_combo)

        ctrl.addSpacing(8)
        ctrl.addWidget(QLabel("Colormap:"))
        self._cmap_combo = QComboBox()
        for c in self._CMAPS:
            self._cmap_combo.addItem(c)
        self._cmap_combo.currentTextChanged.connect(self._on_cmap_changed)
        ctrl.addWidget(self._cmap_combo)

        self._log_z_cb = QCheckBox("Log Z")
        self._log_z_cb.stateChanged.connect(self.selection_changed)
        ctrl.addWidget(self._log_z_cb)

        self._btn_centerline = QPushButton("Centerline…")
        self._btn_centerline.setFixedHeight(26)
        self._btn_centerline.setToolTip(
            "Extract the channel centerline from the current 2D intensity map"
        )
        self._btn_centerline.setVisible(False)
        self._btn_centerline.clicked.connect(self._open_centerline_dialog)
        ctrl.addWidget(self._btn_centerline)

        ctrl.addStretch()
        self._ctrl_row = ctrl
        lay.addLayout(ctrl)

        if PG_AVAILABLE:
            self._glw = pg.GraphicsLayoutWidget()
            self._glw.setBackground('#1e1e1e')
            self._plot = self._glw.addPlot(row=0, col=0)
            self._plot.setAspectLocked(False)
            self._img_item = pg.ImageItem()
            self._plot.addItem(self._img_item)
            # Histogram with draggable level handles for min/max clipping
            try:
                self._hist = pg.HistogramLUTItem()
                self._glw.addItem(self._hist, row=0, col=1)
                self._hist.setImageItem(self._img_item)
                # Limit the histogram column width so the image gets most space
                self._glw.ci.layout.setColumnPreferredWidth(1, 120)
                self._glw.ci.layout.setColumnMaximumWidth(1, 160)
            except Exception:
                self._hist = None
            self._set_cmap(self._CMAPS[0])
            lay.addWidget(self._glw, 1)
        else:
            self._img_item = None
            lay.addWidget(QLabel("pyqtgraph not available"), 1)

    def _on_cmap_changed(self, name):
        self._set_cmap(name)
        self.selection_changed.emit()

    def _set_cmap(self, name):
        if not PG_AVAILABLE:
            return
        for src in (None, 'matplotlib', 'colorcet'):
            try:
                kw = {} if src is None else {'source': src}
                self._current_cmap = pg.colormap.get(name, **kw)
                break
            except Exception:
                continue
        if self._current_cmap is None:
            try:
                self._current_cmap = pg.colormap.get('viridis')
            except Exception:
                return
        if self._hist is not None:
            try:
                self._hist.gradient.setColorMap(self._current_cmap)
            except Exception:
                pass

    # ── Public API ────────────────────────────────────────────────────────────

    def set_columns(self, cols, x_col="", motors=None, detectors=None):
        """Populate Y-motor and Z combos; auto-select from motor/detector hints."""
        motors    = list(motors    or [])
        detectors = list(detectors or [])
        det_set   = set(detectors)
        col_set   = set(cols)

        saved_y = self._y_combo.currentText()
        saved_z = self._z_combo.currentText()

        for combo in (self._y_combo, self._z_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(cols)
            combo.blockSignals(False)

        # Restore previous selections when the same columns are present again
        if saved_y in col_set and saved_z in col_set:
            self._y_combo.blockSignals(True)
            self._y_combo.setCurrentText(saved_y)
            self._y_combo.blockSignals(False)
            self._z_combo.blockSignals(True)
            self._z_combo.setCurrentText(saved_z)
            self._z_combo.blockSignals(False)
            return

        # Auto-select Y motor: prefer explicit motor hints, then non-x non-detector columns
        y_cands = [m for m in motors if m != x_col and m in cols]
        if not y_cands:
            # Exclude known detectors from the fallback so we pick a motor-like column
            y_cands = [c for c in cols
                       if c != x_col and c not in ("time", "seq_num") and c not in det_set]
        if not y_cands:
            # Last resort: anything that's not x
            y_cands = [c for c in cols if c != x_col and c not in ("time", "seq_num")]
        if y_cands:
            self._y_combo.setCurrentText(y_cands[0])

        # Z: first detector, or first column not used by x or y
        z_cands = [d for d in detectors if d in cols]
        if not z_cands:
            y_sel = self._y_combo.currentText()
            z_cands = [c for c in cols
                       if c not in (x_col, y_sel, "time", "seq_num")]
        if z_cands:
            self._z_combo.setCurrentText(z_cands[0])

    def get_y_signal(self) -> str:
        return self._y_combo.currentText()

    def get_z_signal(self) -> str:
        return self._z_combo.currentText()

    def add_to_ctrl_row(self, widget, prepend_spacing: int = 8):
        """Insert *widget* into the control row, before the trailing stretch."""
        idx = self._ctrl_row.count() - 1   # stretch occupies last slot
        if prepend_spacing:
            self._ctrl_row.insertSpacing(idx, prepend_spacing)
            idx += 1
        self._ctrl_row.insertWidget(idx, widget)

    def set_scan_range(self, x_min, x_max, y_min, y_max):
        """Lock the view to the planned motor extents (call once from start doc)."""
        self._scan_x_range = (float(x_min), float(x_max))
        self._scan_y_range = (float(y_min), float(y_max))
        if PG_AVAILABLE and self._plot is not None:
            self._plot.setRange(
                xRange=self._scan_x_range,
                yRange=self._scan_y_range,
                padding=0.05,
            )

    def set_y_ticks(self, n_scans, labels=None):
        """Set integer-only Y-axis ticks; labels default to 0..n_scans-1."""
        if not PG_AVAILABLE or self._plot is None:
            return
        if labels is None:
            labels = [str(i) for i in range(n_scans)]
        ticks = [(i, str(lbl)) for i, lbl in enumerate(labels[:n_scans])]
        self._plot.getAxis('left').setTicks([ticks])

    def replot(self, xs, ys, zs, x_label="X", y_label="Y", z_label="Z"):
        """Build and display the 2D intensity map from flat (x, y, z) arrays."""
        if not PG_AVAILABLE or self._img_item is None or self._current_cmap is None:
            return
        self._last_raw = (
            np.asarray(xs, dtype=float), np.asarray(ys, dtype=float),
            np.asarray(zs, dtype=float), x_label, y_label,
        )
        self._btn_centerline.setVisible(True)
        self._plot.getAxis('left').setTicks(None)
        try:
            img, x_vals, y_vals = build_2d_map(xs, ys, zs)
        except Exception:
            return

        if self._log_z_cb.isChecked():
            with np.errstate(divide='ignore', invalid='ignore'):
                img = np.log10(np.where(img > 0, img, np.nan))

        nan_mask = np.isnan(img)
        valid    = img[~nan_mask]
        if len(valid) == 0:
            return

        vmin, vmax = float(valid.min()), float(valid.max())
        if vmax == vmin:
            vmax = vmin + 1.0

        # Place NaN pixels just below the data minimum so they are clamped to
        # the lowest LUT entry by the histogram's level handles.
        sentinel          = vmin - 0.1 * (vmax - vmin)
        img_disp          = img.copy()
        img_disp[nan_mask] = sentinel

        # pg.ImageItem expects (nx, ny) — transpose from our (ny, nx) array
        self._img_item.setImage(img_disp.T, autoLevels=False,
                                levels=(sentinel, vmax))

        # Position the image in data-space coordinates
        nx, ny = len(x_vals), len(y_vals)
        dx = (float(x_vals[-1]) - float(x_vals[0])) / max(nx - 1, 1) if nx > 1 else 1.0
        dy = (float(y_vals[-1]) - float(y_vals[0])) / max(ny - 1, 1) if ny > 1 else 1.0
        self._img_item.setRect(QRectF(
            float(x_vals[0])  - dx / 2,
            float(y_vals[0])  - dy / 2,
            float(x_vals[-1]) - float(x_vals[0]) + dx,
            float(y_vals[-1]) - float(y_vals[0]) + dy,
        ))

        # Initialise histogram level handles at the actual data range.
        # The histogram axis is zoomed to the data range to avoid the spike
        # from NaN sentinel values appearing on the left edge.
        if self._hist is not None:
            try:
                pad = 0.05 * (vmax - vmin)
                self._hist.setHistogramRange(vmin - pad, vmax + pad)
                self._hist.setLevels(vmin, vmax)
            except Exception:
                pass

        # Use planned motor extents if available; otherwise auto-range to data
        if self._scan_x_range is not None and self._scan_y_range is not None:
            self._plot.setRange(
                xRange=self._scan_x_range,
                yRange=self._scan_y_range,
                padding=0.05,
            )
        else:
            self._plot.autoRange(items=[self._img_item])

        self._plot.setLabel('bottom', x_label)
        self._plot.setLabel('left',   y_label)
        title = (f"log₁₀({z_label})" if self._log_z_cb.isChecked() else z_label)
        self._plot.setTitle(title, color='#aaaaaa', size='10pt')

    def overlay_centerline(self, cx, cy):
        """Draw (or replace) the centerline overlay on the 2D map."""
        if not PG_AVAILABLE or self._plot is None:
            return
        if self._cl_overlay is not None:
            try:
                self._plot.removeItem(self._cl_overlay)
            except Exception:
                pass
        self._cl_overlay = pg.PlotDataItem(
            np.asarray(cx, dtype=float), np.asarray(cy, dtype=float),
            pen=pg.mkPen('#ff4444', width=2),
            symbol='o', symbolSize=6,
            symbolBrush='#ff4444', symbolPen=None,
        )
        self._plot.addItem(self._cl_overlay)

    def _open_centerline_dialog(self):
        """Open the CenterlineDialog with the current map data."""
        if self._last_raw is None:
            return
        xs, ys, zs, x_label, y_label = self._last_raw
        from .centerline_dialog import CenterlineDialog
        dlg = CenterlineDialog(xs, ys, zs, x_label, y_label, parent=self)
        dlg.centerline_ready.connect(self.overlay_centerline)
        dlg.exec()

    def clear(self):
        self._scan_x_range = None
        self._scan_y_range = None
        self._last_raw = None
        self._btn_centerline.setVisible(False)
        if PG_AVAILABLE and self._img_item is not None:
            self._img_item.clear()
            self._plot.setTitle("")
            self._plot.getAxis('left').setTicks(None)
        if self._cl_overlay is not None:
            try:
                self._plot.removeItem(self._cl_overlay)
            except Exception:
                pass
            self._cl_overlay = None
