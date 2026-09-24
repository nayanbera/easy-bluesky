"""centerline_dialog.py — Microfluidic channel centerline extraction from a 2D intensity map.

Algorithm
---------
1. Grid the flat (xs, ys, zs) scatter data to a regular 2D array via
   scipy griddata (linear interpolation).
2. Threshold to produce a binary channel mask.
3. Preprocess the mask: fill internal holes, morphological opening to remove
   small noise regions, then keep only the largest connected component.
4. Skeletonize with vectorized Zhang-Suen thinning → 1-pixel-wide skeleton.
5. Prune short spur branches (iterative endpoint removal, user-controlled).
6. Trace a connected path through the skeleton starting from the user-
   supplied start point (click on preview or type coordinates).
7. Optionally smooth the path; resample at equal arc-length spacing.
"""

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
    from scipy.ndimage import (
        binary_fill_holes, binary_opening, generate_binary_structure,
        label as _label, uniform_filter1d as _smooth1d,
    )
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


# ── Mask preprocessing ───────────────────────────────────────────────────────

def _preprocess_mask(mask: np.ndarray) -> np.ndarray:
    """Fill holes → morphological opening → keep largest connected component."""
    if not SCIPY_AVAILABLE:
        return mask
    mask = binary_fill_holes(mask)
    struct = generate_binary_structure(2, 2)          # 8-connected
    mask = binary_opening(mask, structure=struct, iterations=2)
    labeled, n = _label(mask)
    if n > 1:
        sizes = np.array([(labeled == i).sum() for i in range(1, n + 1)])
        mask = (labeled == sizes.argmax() + 1)
    return mask.astype(bool)


# ── Skeletonization ──────────────────────────────────────────────────────────

def _zhang_suen_thin(mask: np.ndarray) -> np.ndarray:
    """Vectorized Zhang-Suen morphological thinning → 1-px skeleton (bool)."""
    img = mask.astype(np.uint8, copy=True)
    while True:
        prev = img.copy()
        for sub in range(2):
            p = np.pad(img, 1)
            P2 = p[:-2, 1:-1]; P3 = p[:-2, 2:]
            P4 = p[1:-1, 2:];  P5 = p[2:, 2:]
            P6 = p[2:, 1:-1];  P7 = p[2:, :-2]
            P8 = p[1:-1, :-2]; P9 = p[:-2, :-2]
            B = P2 + P3 + P4 + P5 + P6 + P7 + P8 + P9
            ns = np.stack([P2, P3, P4, P5, P6, P7, P8, P9, P2], axis=-1)
            A = ((ns[..., :-1] == 0) & (ns[..., 1:] == 1)).sum(-1)
            cond = (img == 1) & (B >= 2) & (B <= 6) & (A == 1)
            if sub == 0:
                cond &= (P2 * P4 * P6 == 0) & (P4 * P6 * P8 == 0)
            else:
                cond &= (P2 * P4 * P8 == 0) & (P2 * P6 * P8 == 0)
            img[cond] = 0
        if np.array_equal(img, prev):
            break
    return img.astype(bool)


def _prune_spurs(skel: np.ndarray, n_iter: int) -> np.ndarray:
    """Remove skeleton branches shorter than n_iter pixels.

    Each iteration removes all pixels that have exactly one 8-connected
    skeleton neighbour (endpoints).  Repeating this n_iter times trims
    branches shorter than n_iter pixels, leaving the main path intact.
    """
    img = skel.astype(np.uint8, copy=True)
    for _ in range(n_iter):
        p = np.pad(img, 1)
        nbr = (p[:-2, 1:-1] + p[:-2, 2:] + p[1:-1, 2:] + p[2:, 2:] +
               p[2:, 1:-1] + p[2:, :-2] + p[1:-1, :-2] + p[:-2, :-2])
        img[(img == 1) & (nbr == 1)] = 0
    return img.astype(bool)


# ── Path tracing ─────────────────────────────────────────────────────────────

def _trace_path(skel: np.ndarray,
                start_rc: tuple[int, int]) -> list[tuple[int, int]]:
    """Trace a connected path through *skel* starting at the skeleton pixel
    nearest to *start_rc* = (row, col).

    At branch points the algorithm continues in the direction most aligned
    with the current heading so it follows serpentine bends without doubling
    back.  Returns an ordered list of (row, col) tuples.
    """
    rows, cols = np.where(skel)
    if len(rows) == 0:
        return []

    pixel_set: set[tuple[int, int]] = set(zip(rows.tolist(), cols.tolist()))

    r0, c0 = start_rc
    dists = (rows - r0) ** 2 + (cols - c0) ** 2
    best = int(np.argmin(dists))
    start = (int(rows[best]), int(cols[best]))

    path = [start]
    visited: set[tuple[int, int]] = {start}

    while True:
        r, c = path[-1]
        nbrs = [
            (r + dr, c + dc)
            for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            if (dr or dc)
            and (r + dr, c + dc) in pixel_set
            and (r + dr, c + dc) not in visited
        ]
        if not nbrs:
            break
        if len(nbrs) == 1:
            nxt = nbrs[0]
        else:
            if len(path) >= 2:
                dr0 = r - path[-2][0]
                dc0 = c - path[-2][1]
                scores = [dr0 * (nr - r) + dc0 * (nc - c) for nr, nc in nbrs]
                nxt = nbrs[int(np.argmax(scores))]
            else:
                nxt = nbrs[0]
        path.append(nxt)
        visited.add(nxt)

    return path


# ── Arc-length resampling ────────────────────────────────────────────────────

def _resample_equal_spacing(cx: np.ndarray, cy: np.ndarray,
                            spacing: float) -> tuple[np.ndarray, np.ndarray]:
    if len(cx) < 2 or spacing <= 0:
        return cx, cy
    ds = np.sqrt(np.diff(cx) ** 2 + np.diff(cy) ** 2)
    s = np.concatenate([[0.0], np.cumsum(ds)])
    if s[-1] <= 0:
        return cx, cy
    n = max(2, int(s[-1] / spacing) + 1)
    s_new = np.linspace(0.0, s[-1], n)
    return np.interp(s_new, s, cx), np.interp(s_new, s, cy)


# ── Dialog ───────────────────────────────────────────────────────────────────

class CenterlineDialog(QDialog):
    """Interactive dialog for extracting a channel centerline from a 2D map."""

    centerline_ready = pyqtSignal(object, object)   # (cx_array, cy_array)

    def __init__(self, xs, ys, zs, x_label="X", y_label="Y", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Centerline Extraction")
        self.resize(760, 660)
        self._xs = np.asarray(xs, dtype=float).ravel()
        self._ys = np.asarray(ys, dtype=float).ravel()
        self._zs = np.asarray(zs, dtype=float).ravel()
        self._x_label = x_label
        self._y_label = y_label
        self._grid: np.ndarray | None = None
        self._xi = self._yi = None
        self._zmin = self._zmax = 0.0
        self._cx = self._cy = None
        self._prev_plot = None
        self._setup_ui()
        self._compute_grid()
        self._update_preview()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        grp = QGroupBox("Extraction parameters")
        gl = QVBoxLayout(grp)
        gl.setSpacing(4)

        # Row 1: threshold + polarity
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Threshold:"))
        self._thr_slider = QSlider(Qt.Orientation.Horizontal)
        self._thr_slider.setRange(0, 1000)
        self._thr_slider.setValue(500)
        self._thr_slider.setMinimumWidth(150)
        r1.addWidget(self._thr_slider, 1)
        self._thr_spin = QDoubleSpinBox()
        self._thr_spin.setDecimals(4)
        self._thr_spin.setMinimumWidth(110)
        r1.addWidget(self._thr_spin)
        r1.addSpacing(8)
        r1.addWidget(QLabel("Channel is:"))
        self._sense_combo = QComboBox()
        self._sense_combo.addItems(["Brighter than threshold", "Darker than threshold"])
        r1.addWidget(self._sense_combo)
        gl.addLayout(r1)

        # Row 2: start point
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Start point  X:"))
        self._x_start = QDoubleSpinBox()
        self._x_start.setDecimals(4)
        self._x_start.setMinimumWidth(100)
        r2.addWidget(self._x_start)
        r2.addWidget(QLabel("Y:"))
        self._y_start = QDoubleSpinBox()
        self._y_start.setDecimals(4)
        self._y_start.setMinimumWidth(100)
        r2.addWidget(self._y_start)
        hint = QLabel("  (or click on preview)")
        hint.setStyleSheet("color: #888888; font-size: 11px;")
        r2.addWidget(hint)
        r2.addStretch()
        gl.addLayout(r2)

        # Row 3: prune + smooth + spacing + Extract
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Prune spurs:"))
        self._prune_spin = QSpinBox()
        self._prune_spin.setRange(0, 100)
        self._prune_spin.setValue(15)
        self._prune_spin.setSuffix(" px")
        self._prune_spin.setToolTip(
            "Remove skeleton branches shorter than this many pixels.\n"
            "Increase if the skeleton has many spurious dead-ends."
        )
        r3.addWidget(self._prune_spin)
        r3.addSpacing(12)
        self._smooth_cb = QCheckBox("Smooth")
        self._smooth_cb.setChecked(True)
        r3.addWidget(self._smooth_cb)
        self._smooth_spin = QSpinBox()
        self._smooth_spin.setRange(1, 99)
        self._smooth_spin.setValue(5)
        self._smooth_spin.setSuffix(" pts")
        r3.addWidget(self._smooth_spin)
        r3.addSpacing(12)
        r3.addWidget(QLabel("Spacing:"))
        self._spacing_spin = QDoubleSpinBox()
        self._spacing_spin.setDecimals(5)
        self._spacing_spin.setRange(1e-9, 1e9)
        self._spacing_spin.setValue(0.1)
        self._spacing_spin.setMinimumWidth(90)
        r3.addWidget(self._spacing_spin)
        r3.addStretch()
        btn_extract = QPushButton("Extract")
        btn_extract.setDefault(True)
        btn_extract.clicked.connect(self._extract)
        r3.addWidget(btn_extract)
        gl.addLayout(r3)
        lay.addWidget(grp)

        # ── Preview ───────────────────────────────────────────────────────
        if PG_AVAILABLE:
            self._glw = pg.GraphicsLayoutWidget()
            self._glw.setBackground('#1e1e1e')
            self._glw.setMinimumHeight(230)
            self._prev_plot = self._glw.addPlot()
            self._prev_plot.setAspectLocked(False)

            self._mask_item = pg.ImageItem()   # cleaned mask (grey)
            self._prev_plot.addItem(self._mask_item)

            self._skel_scatter = pg.ScatterPlotItem(  # skeleton (cyan)
                size=2, pen=None, brush=pg.mkBrush('#00cccc')
            )
            self._prev_plot.addItem(self._skel_scatter)

            self._cl_item = pg.PlotDataItem(   # traced path (red)
                pen=pg.mkPen('#ff4444', width=2),
                symbol='o', symbolSize=4,
                symbolBrush='#ff4444', symbolPen=None,
            )
            self._prev_plot.addItem(self._cl_item)

            self._start_marker = pg.ScatterPlotItem(  # start point (yellow)
                size=14, pen=pg.mkPen('#ffcc00', width=2), brush=None,
            )
            self._prev_plot.addItem(self._start_marker)

            self._prev_plot.setLabel('bottom', self._x_label)
            self._prev_plot.setLabel('left', self._y_label)
            self._glw.scene().sigMouseClicked.connect(self._on_preview_click)
            lay.addWidget(self._glw, 1)
        else:
            self._glw = None
            self._mask_item = self._skel_scatter = self._cl_item = None
            self._start_marker = None

        # ── Result table ───────────────────────────────────────────────────
        res_grp = QGroupBox("Centerline points")
        rl = QVBoxLayout(res_grp)
        rl.setSpacing(2)
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels([self._x_label, self._y_label])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMaximumHeight(120)
        rl.addWidget(self._table)
        self._status_lbl = QLabel(
            "Set threshold and start point, then click Extract."
        )
        rl.addWidget(self._status_lbl)
        lay.addWidget(res_grp)

        # ── Bottom buttons ─────────────────────────────────────────────────
        br = QHBoxLayout()
        self._btn_copy = QPushButton("Copy to Clipboard")
        self._btn_copy.setEnabled(False)
        self._btn_copy.clicked.connect(self._copy)
        br.addWidget(self._btn_copy)
        self._btn_save = QPushButton("Save CSV…")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save_csv)
        br.addWidget(self._btn_save)
        br.addStretch()
        self._btn_overlay = QPushButton("Overlay on Map")
        self._btn_overlay.setEnabled(False)
        self._btn_overlay.clicked.connect(self._emit_overlay)
        br.addWidget(self._btn_overlay)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        br.addWidget(btn_close)
        lay.addLayout(br)

        # Wire controls
        self._thr_slider.valueChanged.connect(self._on_slider_changed)
        self._thr_spin.valueChanged.connect(self._on_spin_changed)
        self._sense_combo.currentIndexChanged.connect(self._update_preview)
        self._x_start.valueChanged.connect(self._update_start_marker)
        self._y_start.valueChanged.connect(self._update_start_marker)

    # ── Grid ──────────────────────────────────────────────────────────────────

    def _compute_grid(self):
        xs, ys, zs = self._xs, self._ys, self._zs
        n_ux = len(np.unique(np.round(xs, 8)))
        n_uy = len(np.unique(np.round(ys, 8)))
        nx = max(80, min(250, n_ux))
        ny = max(80, min(250, n_uy))
        xi = np.linspace(xs.min(), xs.max(), nx)
        yi = np.linspace(ys.min(), ys.max(), ny)
        self._xi, self._yi = xi, yi

        if SCIPY_AVAILABLE:
            grid = _griddata(
                (xs, ys), zs, (xi[None, :], yi[:, None]), method='linear'
            )
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

        self._spacing_spin.setValue(max(1e-9, (xs.max() - xs.min()) / 100.0))

        for sp, lo, hi, val in [
            (self._x_start, float(xs.min()), float(xs.max()), float(xs.min())),
            (self._y_start, float(ys.min()), float(ys.max()),
             float((ys.min() + ys.max()) / 2.0)),
        ]:
            sp.blockSignals(True)
            sp.setRange(lo, hi)
            sp.setSingleStep((hi - lo) / 100.0)
            sp.setValue(val)
            sp.blockSignals(False)

    # ── Controls ──────────────────────────────────────────────────────────────

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

    def _show_mask_in_preview(self, mask: np.ndarray):
        if self._mask_item is None:
            return
        xi, yi = self._xi, self._yi
        dx = (xi[-1] - xi[0]) / max(len(xi) - 1, 1)
        dy = (yi[-1] - yi[0]) / max(len(yi) - 1, 1)
        self._mask_item.setImage(mask.T.astype(np.uint8) * 180, autoLevels=True)
        self._mask_item.setRect(QRectF(
            float(xi[0]) - dx / 2, float(yi[0]) - dy / 2,
            float(xi[-1] - xi[0]) + dx, float(yi[-1] - yi[0]) + dy,
        ))

    def _update_preview(self):
        """Show raw threshold mask and clear stale skeleton/path overlays."""
        if self._grid is None:
            return
        self._show_mask_in_preview(self._threshold_mask())
        if self._skel_scatter is not None:
            self._skel_scatter.setData([], [])
        if self._cl_item is not None:
            self._cl_item.setData([], [])
        self._update_start_marker()

    def _update_start_marker(self):
        if self._start_marker is not None:
            self._start_marker.setData(
                [self._x_start.value()], [self._y_start.value()]
            )

    def _on_preview_click(self, event):
        if self._prev_plot is None:
            return
        pos = event.scenePos()
        if self._prev_plot.sceneBoundingRect().contains(pos):
            pt = self._prev_plot.vb.mapSceneToView(pos)
            self._x_start.setValue(pt.x())
            self._y_start.setValue(pt.y())

    # ── Extraction ────────────────────────────────────────────────────────────

    def _extract(self):
        if self._grid is None:
            return

        # 1. Threshold
        mask = self._threshold_mask()

        # 2. Preprocess: fill holes + opening + largest component
        self._status_lbl.setText("Preprocessing mask…")
        QApplication.processEvents()
        mask = _preprocess_mask(mask)

        # Update preview to show the CLEANED mask (not raw threshold)
        self._show_mask_in_preview(mask)
        QApplication.processEvents()

        # 3. Skeletonize
        self._status_lbl.setText("Skeletonizing…")
        QApplication.processEvents()
        skel = _zhang_suen_thin(mask)

        # 4. Prune short spurs
        n_prune = self._prune_spin.value()
        if n_prune > 0:
            self._status_lbl.setText(f"Pruning skeleton (≤{n_prune} px spurs)…")
            QApplication.processEvents()
            skel = _prune_spurs(skel, n_prune)

        # 5. Show skeleton (cyan scatter)
        if self._skel_scatter is not None:
            sr, sc = np.where(skel)
            self._skel_scatter.setData(self._xi[sc], self._yi[sr])

        if not skel.any():
            self._status_lbl.setText("Empty skeleton — try adjusting threshold or prune value.")
            return

        # 6. Locate start pixel in the skeleton
        xi, yi = self._xi, self._yi
        c0 = int(np.argmin(np.abs(xi - self._x_start.value())))
        r0 = int(np.argmin(np.abs(yi - self._y_start.value())))

        self._status_lbl.setText("Tracing path…")
        QApplication.processEvents()
        path = _trace_path(skel, (r0, c0))

        if len(path) < 2:
            self._status_lbl.setText(
                "No path found — try a different threshold, start point, or prune value."
            )
            return

        cx = np.array([xi[c] for _, c in path])
        cy = np.array([yi[r] for r, _ in path])

        # 7. Smooth
        if self._smooth_cb.isChecked() and SCIPY_AVAILABLE:
            w = self._smooth_spin.value()
            if w > 1:
                cx = _smooth1d(cx, size=w, mode='nearest')
                cy = _smooth1d(cy, size=w, mode='nearest')

        # 8. Resample
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
