"""centerline_dialog.py — Microfluidic channel centerline extraction from a 2D intensity map."""

import numpy as np

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from PyQt6.QtCore import Qt, QRectF, pyqtSignal

try:
    import pyqtgraph as pg
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

try:
    from scipy.interpolate import griddata as _griddata
    from scipy.ndimage import uniform_filter1d as _smooth1d
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


# ── Arc-length resampling ────────────────────────────────────────────────────

def _resample_equal_spacing(cx: np.ndarray, cy: np.ndarray, spacing: float):
    """Return (cx_r, cy_r) resampled at equal arc-length intervals *spacing*."""
    if len(cx) < 2 or spacing <= 0:
        return cx, cy
    ds = np.sqrt(np.diff(cx) ** 2 + np.diff(cy) ** 2)
    s = np.concatenate([[0.0], np.cumsum(ds)])
    total = s[-1]
    if total <= 0:
        return cx, cy
    n_pts = max(2, int(total / spacing) + 1)
    s_new = np.linspace(0.0, total, n_pts)
    return np.interp(s_new, s, cx), np.interp(s_new, s, cy)


# ── Dialog ───────────────────────────────────────────────────────────────────

class CenterlineDialog(QDialog):
    """Extract the centerline of a microfluidic channel from a 2D intensity map.

    Algorithm
    ---------
    1. Grid the flat (xs, ys, zs) scatter data to a regular 2D array via
       scipy griddata (linear interpolation).
    2. Threshold to produce a binary channel mask (bright or dark channel).
    3. For every x-column of the grid, compute the intensity-weighted centroid
       along y — this gives one (x, y_center) point per column.
    4. Optionally smooth the y_center curve with a uniform filter.
    5. Resample the resulting curve at equal arc-length spacing.

    Signals
    -------
    centerline_ready(cx_array, cy_array) — emitted when "Overlay on Map" is
        clicked, so the caller can draw the centerline on the 2D map widget.
    """

    centerline_ready = pyqtSignal(object, object)

    def __init__(self, xs, ys, zs, x_label="X", y_label="Y", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Centerline Extraction")
        self.resize(700, 580)
        self._xs = np.asarray(xs, dtype=float).ravel()
        self._ys = np.asarray(ys, dtype=float).ravel()
        self._zs = np.asarray(zs, dtype=float).ravel()
        self._x_label = x_label
        self._y_label = y_label
        self._grid: np.ndarray | None = None
        self._xi = self._yi = None
        self._zmin = self._zmax = 0.0
        self._cx = self._cy = None
        self._setup_ui()
        self._compute_grid()
        self._update_preview()

    # ── Construction ──────────────────────────────────────────────────────────

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        # ── Parameter group ───────────────────────────────────────────────
        grp = QGroupBox("Extraction parameters")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # Threshold row
        thr = QHBoxLayout()
        thr.addWidget(QLabel("Threshold:"))
        self._thr_slider = QSlider(Qt.Orientation.Horizontal)
        self._thr_slider.setRange(0, 1000)
        self._thr_slider.setValue(500)
        self._thr_slider.setMinimumWidth(160)
        thr.addWidget(self._thr_slider, 1)
        self._thr_spin = QDoubleSpinBox()
        self._thr_spin.setDecimals(4)
        self._thr_spin.setMinimumWidth(110)
        thr.addWidget(self._thr_spin)
        thr.addSpacing(10)
        thr.addWidget(QLabel("Channel is:"))
        self._sense_combo = QComboBox()
        self._sense_combo.addItems(["Brighter than threshold", "Darker than threshold"])
        thr.addWidget(self._sense_combo)
        grp_lay.addLayout(thr)

        # Smooth + spacing row
        sp = QHBoxLayout()
        self._smooth_cb = QCheckBox("Smooth")
        self._smooth_cb.setChecked(True)
        sp.addWidget(self._smooth_cb)
        self._smooth_spin = QSpinBox()
        self._smooth_spin.setRange(1, 99)
        self._smooth_spin.setValue(5)
        self._smooth_spin.setSuffix(" pts")
        sp.addWidget(self._smooth_spin)
        sp.addSpacing(16)
        sp.addWidget(QLabel("Output spacing:"))
        self._spacing_spin = QDoubleSpinBox()
        self._spacing_spin.setDecimals(5)
        self._spacing_spin.setRange(1e-9, 1e9)
        self._spacing_spin.setValue(0.1)
        self._spacing_spin.setMinimumWidth(100)
        sp.addWidget(self._spacing_spin)
        sp.addStretch()
        btn_extract = QPushButton("Extract")
        btn_extract.setDefault(True)
        btn_extract.clicked.connect(self._extract)
        sp.addWidget(btn_extract)
        grp_lay.addLayout(sp)
        lay.addWidget(grp)

        # ── Preview: threshold mask + centerline overlay ───────────────────
        if PG_AVAILABLE:
            self._glw = pg.GraphicsLayoutWidget()
            self._glw.setBackground('#1e1e1e')
            self._glw.setMinimumHeight(210)
            self._prev_plot = self._glw.addPlot()
            self._prev_plot.setAspectLocked(False)
            self._mask_item = pg.ImageItem()
            self._prev_plot.addItem(self._mask_item)
            self._prev_plot.setLabel('bottom', self._x_label)
            self._prev_plot.setLabel('left', self._y_label)
            self._cl_item = pg.PlotDataItem(
                pen=pg.mkPen('#ff4444', width=2),
                symbol='o', symbolSize=5,
                symbolBrush='#ff4444', symbolPen=None,
            )
            self._prev_plot.addItem(self._cl_item)
            lay.addWidget(self._glw, 1)
        else:
            self._glw = self._mask_item = self._cl_item = None

        # ── Result table ───────────────────────────────────────────────────
        res_grp = QGroupBox("Centerline points")
        res_lay = QVBoxLayout(res_grp)
        res_lay.setSpacing(2)
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels([self._x_label, self._y_label])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMaximumHeight(140)
        res_lay.addWidget(self._table)
        self._status_lbl = QLabel("Adjust threshold and click Extract.")
        res_lay.addWidget(self._status_lbl)
        lay.addWidget(res_grp)

        # ── Bottom buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._btn_copy = QPushButton("Copy to Clipboard")
        self._btn_copy.setEnabled(False)
        self._btn_copy.clicked.connect(self._copy)
        btn_row.addWidget(self._btn_copy)
        self._btn_save = QPushButton("Save CSV…")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save_csv)
        btn_row.addWidget(self._btn_save)
        btn_row.addStretch()
        self._btn_overlay = QPushButton("Overlay on Map")
        self._btn_overlay.setEnabled(False)
        self._btn_overlay.clicked.connect(self._emit_overlay)
        btn_row.addWidget(self._btn_overlay)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        # Wire threshold controls together
        self._thr_slider.valueChanged.connect(self._on_slider_changed)
        self._thr_spin.valueChanged.connect(self._on_spin_changed)
        self._sense_combo.currentIndexChanged.connect(self._update_preview)

    # ── Computation ───────────────────────────────────────────────────────────

    def _compute_grid(self):
        xs, ys, zs = self._xs, self._ys, self._zs
        n_ux = len(np.unique(np.round(xs, 8)))
        n_uy = len(np.unique(np.round(ys, 8)))
        nx = max(50, min(300, n_ux))
        ny = max(50, min(300, n_uy))
        xi = np.linspace(xs.min(), xs.max(), nx)
        yi = np.linspace(ys.min(), ys.max(), ny)
        self._xi, self._yi = xi, yi

        if SCIPY_AVAILABLE:
            grid = _griddata((xs, ys), zs, (xi[None, :], yi[:, None]), method='linear')
            nan_mask = np.isnan(grid)
            if nan_mask.any():
                grid[nan_mask] = float(np.nanmin(zs))
            self._grid = grid  # shape (ny, nx)
        else:
            self._grid = np.zeros((ny, nx))

        self._zmin = float(np.nanmin(zs))
        self._zmax = float(np.nanmax(zs))
        mid = (self._zmin + self._zmax) / 2.0
        step = (self._zmax - self._zmin) / 100.0 or 1.0

        self._thr_spin.blockSignals(True)
        self._thr_spin.setRange(self._zmin, self._zmax)
        self._thr_spin.setSingleStep(step)
        self._thr_spin.setValue(mid)
        self._thr_spin.blockSignals(False)

        # Default spacing = 1 % of x range
        x_range = float(xs.max() - xs.min())
        self._spacing_spin.setValue(max(1e-9, x_range / 100.0))

    def _threshold_mask(self) -> np.ndarray:
        thresh = self._thr_spin.value()
        bright = self._sense_combo.currentIndex() == 0
        return (self._grid > thresh) if bright else (self._grid < thresh)

    def _on_slider_changed(self, val: int):
        thresh = self._zmin + (self._zmax - self._zmin) * val / 1000.0
        self._thr_spin.blockSignals(True)
        self._thr_spin.setValue(thresh)
        self._thr_spin.blockSignals(False)
        self._update_preview()

    def _on_spin_changed(self, val: float):
        if self._zmax > self._zmin:
            frac = (val - self._zmin) / (self._zmax - self._zmin)
            self._thr_slider.blockSignals(True)
            self._thr_slider.setValue(int(max(0, min(1000, frac * 1000))))
            self._thr_slider.blockSignals(False)
        self._update_preview()

    def _update_preview(self):
        if self._grid is None or self._mask_item is None:
            return
        mask = self._threshold_mask()
        xi, yi = self._xi, self._yi
        dx = (xi[-1] - xi[0]) / max(len(xi) - 1, 1)
        dy = (yi[-1] - yi[0]) / max(len(yi) - 1, 1)
        # pg.ImageItem expects (nx, ny) → transpose the (ny, nx) mask
        self._mask_item.setImage(mask.T.astype(np.uint8) * 200, autoLevels=True)
        self._mask_item.setRect(QRectF(
            float(xi[0]) - dx / 2,
            float(yi[0]) - dy / 2,
            float(xi[-1] - xi[0]) + dx,
            float(yi[-1] - yi[0]) + dy,
        ))

    def _extract(self):
        if self._grid is None:
            return
        mask = self._threshold_mask()
        xi, yi = self._xi, self._yi  # shapes (nx,), (ny,)

        cx_list: list[float] = []
        cy_list: list[float] = []
        for j, x_val in enumerate(xi):
            col = mask[:, j].astype(float)
            total = col.sum()
            if total < 2:
                continue
            # Intensity-weighted centroid along y
            cy_list.append(float(np.dot(yi, col) / total))
            cx_list.append(float(x_val))

        if len(cx_list) < 2:
            self._status_lbl.setText("No channel detected — adjust threshold.")
            if self._cl_item is not None:
                self._cl_item.setData([], [])
            return

        cx = np.array(cx_list)
        cy = np.array(cy_list)

        if self._smooth_cb.isChecked() and SCIPY_AVAILABLE:
            w = self._smooth_spin.value()
            if w > 1:
                cy = _smooth1d(cy, size=w, mode='nearest')

        cx, cy = _resample_equal_spacing(cx, cy, self._spacing_spin.value())
        self._cx, self._cy = cx, cy

        self._table.setRowCount(len(cx))
        for i, (x, y) in enumerate(zip(cx, cy)):
            self._table.setItem(i, 0, QTableWidgetItem(f"{x:.6g}"))
            self._table.setItem(i, 1, QTableWidgetItem(f"{y:.6g}"))

        self._status_lbl.setText(f"{len(cx)} centerline points extracted.")
        self._btn_copy.setEnabled(True)
        self._btn_save.setEnabled(True)
        self._btn_overlay.setEnabled(True)

        if self._cl_item is not None:
            self._cl_item.setData(cx, cy)

    # ── Output ────────────────────────────────────────────────────────────────

    def _copy(self):
        if self._cx is None:
            return
        lines = [f"{self._x_label}\t{self._y_label}"]
        lines += [f"{x:.6g}\t{y:.6g}" for x, y in zip(self._cx, self._cy)]
        QApplication.clipboard().setText("\n".join(lines))

    def _save_csv(self):
        if self._cx is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Centerline CSV", "centerline.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        lines = [f"{self._x_label},{self._y_label}"]
        lines += [f"{x:.6g},{y:.6g}" for x, y in zip(self._cx, self._cy)]
        with open(path, "w") as f:
            f.write("\n".join(lines))
        self._status_lbl.setText(f"Saved {len(self._cx)} points to {path}")

    def _emit_overlay(self):
        if self._cx is not None and self._cy is not None:
            self.centerline_ready.emit(self._cx, self._cy)
