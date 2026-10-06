"""mca_viewer.py — IOC-native MCA viewer using EPICS ROI PVs directly."""

import json
import re
from pathlib import Path

import numpy as np
from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtGui import QCloseEvent, QColor
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QDoubleSpinBox, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

try:
    from scipy.optimize import curve_fit as _curve_fit
    from scipy.signal import find_peaks as _find_peaks
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

_FE55_KA_EV = 5895.0   # Mn Kα (eV)
_FE55_KB_EV = 6490.4   # Mn Kβ1 (eV)

try:
    import pyqtgraph as pg
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

_MCA_SETTINGS_PATH = Path.home() / ".easy_bluesky" / "mca_viewer_settings.json"

_N_ROIS = 16   # fallback when pv_map carries no ROI signals

# Semi-transparent RGBA colors for ROI bands
_ROI_COLORS = [
    (255, 100, 100, 60),
    (100, 200, 100, 60),
    (100, 150, 255, 60),
    (255, 200,  50, 60),
    (200, 100, 255, 60),
    ( 50, 220, 220, 60),
    (255, 150,  50, 60),
    (150, 255, 150, 60),
    (255,  80, 200, 60),
    ( 80, 200, 255, 60),
    (255, 255, 100, 60),
    (200, 200, 200, 60),
    (255, 130, 130, 60),
    (130, 255, 130, 60),
    (130, 130, 255, 60),
    (255, 200, 130, 60),
]


def _load_settings() -> dict:
    try:
        if _MCA_SETTINGS_PATH.exists():
            return json.loads(_MCA_SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settings(settings: dict):
    try:
        _MCA_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MCA_SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass


def extract_mca_prefix(pv_map: dict) -> str | None:
    """Return the MCA record base prefix by matching any ROI count PV.

    Handles both real mca records (separator '.') and pydev records
    (separator '_', used when the EPICS DB parser rejects dots in names).
    """
    pat = re.compile(r'^(.+)[._]R\d+$')
    for pv in pv_map.values():
        if not pv:
            continue
        m = pat.match(pv)
        if m:
            return m.group(1)
    return None


def _detect_sep(pv_map: dict) -> str:
    """Return the field separator used by this device's MCA PVs: '.' or '_'."""
    for pv in pv_map.values():
        if pv and re.search(r'_R\d+$', pv):
            return '_'
    return '.'


def detect_n_rois(pv_map: dict) -> int:
    """Count ROI slots from the ophyd pv_map (PVs ending in .R{N} or _R{N}).

    Returns max(N)+1 so the viewer only subscribes to the ROIs the device
    actually exposes.  Falls back to _N_ROIS if no ROI PVs are found.
    """
    roi_pat = re.compile(r'[._]R(\d+)$')
    indices = set()
    for pv in pv_map.values():
        if not pv:
            continue
        m = roi_pat.search(pv)
        if m:
            indices.add(int(m.group(1)))
    return max(indices) + 1 if indices else _N_ROIS


class MCAViewerWindow(QMainWindow):
    """Floating live MCA viewer driven by IOC ROI PVs — no PyMCA required."""

    _spectrum_received = Signal(object)           # np.ndarray
    _roi_cb_received   = Signal(int, object)      # idx, partial dict
    _status_received   = Signal(float, float, bool)  # ertm, eltm, acqg
    _cal_received      = Signal(float, float)     # calo (eV), cals (eV/ch)
    _hdf_pv_received   = Signal(str, object)      # field, value

    def __init__(self, device_name: str, mca_prefix: str = "",
                 pv_map: dict | None = None, parent=None):
        super().__init__(parent)
        self._device_name = device_name
        self._prefix      = mca_prefix.strip()
        self._pv_map      = pv_map or {}
        self._alive       = True
        self._live        = True
        self._n_rois      = detect_n_rois(self._pv_map)
        # '.' for real mca records; '_' for pydev records (dots not allowed in
        # EPICS DB parser record/alias names on some base versions)
        self._sep         = _detect_sep(self._pv_map)

        # CA PV handles — strong references to prevent GC
        self._pvs: list = []
        self._hdf_pvs: list = []
        self._hdf_prefix: str = ""
        self._hdf_num_capture: int = 0

        # Per-ROI reverse lookup: pvname → (idx, field)
        self._roi_pv_idx: dict = {}   # pvname → (idx, 'lo'|'hi'|'nm'|'counts')

        # ROI state — one slot per ROI exposed by the ophyd device
        self._rois: list[dict] = [
            {'lo': 0, 'hi': 0, 'name': '', 'counts': 0.0}
            for _ in range(self._n_rois)
        ]

        # pyqtgraph ROI region items — idx → LinearRegionItem
        self._regions: dict = {}
        self._roi_visible: dict = {}              # idx → bool (default True)
        self._roi_colors: dict = {}               # idx → (r, g, b)

        # Fitted Gaussian curves — list of (popt_channels, (r,g,b))
        self._fit_params: list = []
        self._fit_curve_items: list = []

        # Energy calibration (eV): E = _calo + _cals * channel
        self._calo: float = 0.0
        self._cals: float = 0.0   # 0 = uncalibrated

        self._last_counts: np.ndarray | None = None
        self._updating_roi = False   # suppress write-back during programmatic updates
        self._pending_cal_gain:   float = 0.0
        self._pending_cal_offset: float = 0.0

        # Cached status/cal values — updated directly from CA callbacks
        self._ertm: float = 0.0
        self._eltm: float = 0.0
        self._acqg: bool  = False
        self._calo_ioc: float = 0.0
        self._cals_ioc: float = 0.0

        self._spectrum_received.connect(self._on_new_spectrum)
        self._roi_cb_received.connect(self._on_roi_update)
        self._status_received.connect(self._on_status_update)
        self._cal_received.connect(self._on_cal_update)
        self._hdf_pv_received.connect(self._on_hdf_pv_update)

        self._build_ui()
        self._restore_settings()

        if self._prefix:
            QTimer.singleShot(200, lambda: self._connect_mca(self._prefix))

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle(f"MCA Viewer — {self._device_name}")
        self.resize(1300, 750)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        root.addWidget(self._build_ctrl())

        right_split = QSplitter(Qt.Orientation.Vertical)

        if _HAS_PG:
            self._plot_widget = pg.PlotWidget()
            self._plot_widget.setBackground('#1e1e1e')
            self._plot_widget.showGrid(x=True, y=True, alpha=0.2)
            self._plot_widget.setLabel('left', 'Counts')
            self._plot_widget.setLabel('bottom', 'Channel')
            self._curve = pg.PlotCurveItem(pen=pg.mkPen('w', width=1))
            self._plot_widget.addItem(self._curve)

            # Cursor line
            self._cursor_line = pg.InfiniteLine(pos=0, angle=90, movable=False,
                                                pen=pg.mkPen('#888888', width=1, style=Qt.PenStyle.DashLine))
            self._plot_widget.addItem(self._cursor_line, ignoreBounds=True)
            self._plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)

            right_split.addWidget(self._plot_widget)
        else:
            no_pg = QLabel("<b>pyqtgraph not installed.</b><br>pip install pyqtgraph")
            no_pg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            right_split.addWidget(no_pg)

        right_split.addWidget(self._build_roi_table())
        right_split.setStretchFactor(0, 2)
        right_split.setStretchFactor(1, 1)

        root.addWidget(right_split, stretch=1)

        self._status_lbl = QLabel("○ Not connected")
        self.statusBar().addWidget(self._status_lbl, 1)
        self._cursor_lbl = QLabel("")
        self.statusBar().addPermanentWidget(self._cursor_lbl)

    def _build_ctrl(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(230)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(2, 4, 4, 4)
        lay.setSpacing(8)

        # MCA Prefix
        gp = QGroupBox("MCA Prefix")
        gpl = QVBoxLayout(gp)
        gpl.setSpacing(4)
        self._prefix_edit = QLineEdit(self._prefix)
        self._prefix_edit.setPlaceholderText("e.g. IOC:mca1")
        self._prefix_edit.returnPressed.connect(self._on_connect_clicked)
        gpl.addWidget(self._prefix_edit)
        btn_conn = QPushButton("Connect")
        btn_conn.clicked.connect(self._on_connect_clicked)
        gpl.addWidget(btn_conn)
        lay.addWidget(gp)

        # Acquire
        ga = QGroupBox("Acquire")
        gal = QVBoxLayout(ga)
        gal.setSpacing(5)
        row = QHBoxLayout()
        self._btn_erase = _btn("⏮  Erase+Start", "#1a3a1a", "#6ddc6d")
        self._btn_stop  = _btn("■  Stop",          "#3a1a1a", "#dc6d6d")
        self._btn_erase.clicked.connect(self._on_erase_start)
        self._btn_stop.clicked.connect(self._on_stop)
        row.addWidget(self._btn_erase)
        row.addWidget(self._btn_stop)
        gal.addLayout(row)
        self._btn_start = _btn("▶  Start", "#1a2a3a", "#6db0dc")
        self._btn_start.clicked.connect(self._on_start)
        gal.addWidget(self._btn_start)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Preset:"))
        self._spin_preset = QDoubleSpinBox()
        self._spin_preset.setRange(0.0, 86400.0)
        self._spin_preset.setDecimals(2)
        self._spin_preset.setSuffix(" s")
        self._spin_preset.setValue(10.0)
        self._spin_preset.wheelEvent = lambda e: e.ignore()
        self._spin_preset.editingFinished.connect(
            lambda: self._ca_put('.PRTM', self._spin_preset.value()))
        r2.addWidget(self._spin_preset)
        gal.addLayout(r2)
        lay.addWidget(ga)

        # Display
        gd = QGroupBox("Display")
        gdl = QVBoxLayout(gd)
        self._chk_live = QCheckBox("Live update")
        self._chk_live.setChecked(True)
        self._chk_live.toggled.connect(self._on_live_toggled)
        gdl.addWidget(self._chk_live)
        btn_read = QPushButton("Read Now")
        btn_read.clicked.connect(self._on_read_now)
        gdl.addWidget(btn_read)
        self._chk_logy = QCheckBox("Log Y")
        self._chk_logy.toggled.connect(self._on_logy_toggled)
        gdl.addWidget(self._chk_logy)
        self._chk_kev = QCheckBox("Show keV")
        self._chk_kev.setEnabled(False)
        self._chk_kev.toggled.connect(self._on_kev_toggled)
        gdl.addWidget(self._chk_kev)
        lay.addWidget(gd)

        # Add ROI
        btn_add = QPushButton("+ Add ROI")
        btn_add.clicked.connect(self._on_add_roi)
        lay.addWidget(btn_add)

        lay.addWidget(self._build_calibration_panel())
        lay.addWidget(self._build_hdf_panel())

        lay.addStretch()
        return panel

    def _build_hdf_panel(self) -> QGroupBox:
        gh = QGroupBox("HDF File")
        ghl = QVBoxLayout(gh)
        ghl.setSpacing(3)

        row = QHBoxLayout()
        row.addWidget(QLabel("Prefix:"))
        self._hdf_prefix_edit = QLineEdit()
        self._hdf_prefix_edit.setPlaceholderText("e.g. IOC:HDF1:")
        self._hdf_prefix_edit.returnPressed.connect(self._on_hdf_connect)
        row.addWidget(self._hdf_prefix_edit)
        ghl.addLayout(row)

        btn_hdf = QPushButton("Connect HDF")
        btn_hdf.clicked.connect(self._on_hdf_connect)
        ghl.addWidget(btn_hdf)

        dim = "color:#aaa; font-size:10px;"
        lbl_style = "font-size:10px;"

        ghl.addWidget(QLabel("Path:"))
        self._hdf_path_lbl = QLabel("—")
        self._hdf_path_lbl.setStyleSheet(dim)
        self._hdf_path_lbl.setWordWrap(True)
        ghl.addWidget(self._hdf_path_lbl)

        ghl.addWidget(QLabel("File:"))
        self._hdf_name_lbl = QLabel("—")
        self._hdf_name_lbl.setStyleSheet(dim)
        self._hdf_name_lbl.setWordWrap(True)
        ghl.addWidget(self._hdf_name_lbl)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Mode:"))
        self._hdf_mode_lbl = QLabel("—")
        self._hdf_mode_lbl.setStyleSheet(lbl_style)
        r2.addWidget(self._hdf_mode_lbl)
        r2.addStretch()
        ghl.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Status:"))
        self._hdf_status_lbl = QLabel("—")
        self._hdf_status_lbl.setStyleSheet(lbl_style)
        r3.addWidget(self._hdf_status_lbl)
        r3.addStretch()
        ghl.addLayout(r3)

        r4 = QHBoxLayout()
        r4.addWidget(QLabel("Saved:"))
        self._hdf_captured_lbl = QLabel("—")
        self._hdf_captured_lbl.setStyleSheet(lbl_style)
        r4.addWidget(self._hdf_captured_lbl)
        r4.addStretch()
        ghl.addLayout(r4)

        return gh

    def _build_calibration_panel(self) -> QGroupBox:
        gc = QGroupBox("Calibrate — Fe-55")
        gcl = QVBoxLayout(gc)
        gcl.setSpacing(5)

        gcl.addWidget(QLabel(f"Mn Kα  ({_FE55_KA_EV:.1f} eV)"))
        self._spin_ka = QDoubleSpinBox()
        self._spin_ka.setRange(0, 65535)
        self._spin_ka.setDecimals(1)
        self._spin_ka.setSuffix(" ch")
        self._spin_ka.setValue(0.0)
        self._spin_ka.wheelEvent = lambda e: e.ignore()
        self._spin_ka.valueChanged.connect(self._on_cal_channels_changed)
        gcl.addWidget(self._spin_ka)

        gcl.addWidget(QLabel(f"Mn Kβ  ({_FE55_KB_EV:.1f} eV)"))
        self._spin_kb = QDoubleSpinBox()
        self._spin_kb.setRange(0, 65535)
        self._spin_kb.setDecimals(1)
        self._spin_kb.setSuffix(" ch")
        self._spin_kb.setValue(0.0)
        self._spin_kb.wheelEvent = lambda e: e.ignore()
        self._spin_kb.valueChanged.connect(self._on_cal_channels_changed)
        gcl.addWidget(self._spin_kb)

        btn_fit = QPushButton("Auto-fit peaks")
        btn_fit.setToolTip("Fit Gaussians to Mn Kα/Kβ peaks (requires scipy)")
        btn_fit.clicked.connect(self._on_autofit_fe55)
        gcl.addWidget(btn_fit)

        self._chk_show_fit = QCheckBox("Show fit overlay")
        self._chk_show_fit.setChecked(True)
        self._chk_show_fit.toggled.connect(self._on_show_fit_toggled)
        gcl.addWidget(self._chk_show_fit)

        self._cal_result_lbl = QLabel("Gain:   —\nOffset: —")
        self._cal_result_lbl.setStyleSheet("color:#aaa; font-size:11px;")
        gcl.addWidget(self._cal_result_lbl)

        self._fit_report_lbl = QLabel("")
        self._fit_report_lbl.setStyleSheet("color:#888; font-size:10px;")
        self._fit_report_lbl.setWordWrap(True)
        gcl.addWidget(self._fit_report_lbl)

        row = QHBoxLayout()
        self._btn_apply_cal = QPushButton("Apply")
        self._btn_apply_cal.setEnabled(False)
        self._btn_apply_cal.clicked.connect(self._on_apply_calibration)
        self._btn_write_ioc = QPushButton("→ IOC")
        self._btn_write_ioc.setEnabled(False)
        self._btn_write_ioc.setToolTip("Write calibration to IOC CALO/CALS PVs")
        self._btn_write_ioc.clicked.connect(self._on_write_cal_to_ioc)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._on_clear_calibration)
        row.addWidget(self._btn_apply_cal)
        row.addWidget(self._btn_write_ioc)
        row.addWidget(btn_clear)
        gcl.addLayout(row)

        return gc

    def _build_roi_table(self) -> QTableWidget:
        cols = ["#", "Show", "Color", "Name", "Lo Ch", "Hi Ch", "Lo keV", "Hi keV", "Counts", "Del"]
        self._roi_table = QTableWidget(0, len(cols))
        self._roi_table.setHorizontalHeaderLabels(cols)
        self._roi_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self._roi_table.horizontalHeader().setStretchLastSection(False)
        self._roi_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._roi_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.SelectedClicked)
        self._roi_table.itemChanged.connect(self._on_table_name_edited)
        return self._roi_table

    # ── Connection ────────────────────────────────────────────────────────────

    def _on_connect_clicked(self):
        prefix = self._prefix_edit.text().strip()
        if prefix:
            self._connect_mca(prefix)

    def _connect_mca(self, prefix: str):
        try:
            import epics
        except ImportError:
            self._set_status("⚠ pyepics not installed", "#e05050")
            return

        # Tear down old subscriptions
        for pv in self._pvs:
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()
        self._roi_pv_idx.clear()
        self._regions.clear()
        self._fit_params.clear()
        self._fit_curve_items.clear()
        if _HAS_PG:
            self._plot_widget.clear()
            self._plot_widget.addItem(self._curve)
            self._plot_widget.addItem(self._cursor_line, ignoreBounds=True)

        self._prefix = prefix
        self._prefix_edit.setText(prefix)
        self._set_status(f"● Connecting to {prefix}…", "#888888")

        def _mk(pvname, cb, **kw):
            pv = epics.PV(pvname, auto_monitor=True, callback=cb, **kw)
            self._pvs.append(pv)
            return pv

        sep = self._sep
        # For real mca records sep='.', the VAL field IS the record itself.
        # For pydev records sep='_', .VAL is the waveform record directly.
        val_pv = prefix if sep == '_' else f"{prefix}.VAL"
        _mk(val_pv,               self._on_spectrum_cb)
        _mk(f"{prefix}{sep}ERTM", self._on_status_cb)
        _mk(f"{prefix}{sep}ELTM", self._on_status_cb)
        _mk(f"{prefix}{sep}ACQG", self._on_status_cb)
        _mk(f"{prefix}{sep}CALO", self._on_cal_cb)
        _mk(f"{prefix}{sep}CALS", self._on_cal_cb)

        for n in range(self._n_rois):
            for field, key in (
                (f"{prefix}{sep}R{n}",    'counts'),
                (f"{prefix}{sep}R{n}LO",  'lo'),
                (f"{prefix}{sep}R{n}HI",  'hi'),
                (f"{prefix}{sep}R{n}NM",  'nm'),
            ):
                self._roi_pv_idx[field] = (n, key)
                _mk(field, self._on_roi_cb)

    # ── CA callbacks (background thread) ─────────────────────────────────────

    def _on_spectrum_cb(self, pvname='', value=None, **kw):
        if not self._alive or value is None or not self._live:
            return
        self._spectrum_received.emit(np.asarray(value).copy())

    def _on_status_cb(self, pvname='', value=None, **kw):
        if not self._alive or value is None:
            return
        field = pvname.rsplit('.', 1)[-1] if '.' in pvname else pvname
        if field == 'ERTM':
            self._ertm = float(value)
        elif field == 'ELTM':
            self._eltm = float(value)
        elif field == 'ACQG':
            self._acqg = bool(value)
        self._status_received.emit(self._ertm, self._eltm, self._acqg)

    def _on_cal_cb(self, pvname='', value=None, **kw):
        if not self._alive or value is None:
            return
        field = pvname.rsplit('.', 1)[-1] if '.' in pvname else pvname
        if field == 'CALO':
            self._calo_ioc = float(value)
        elif field == 'CALS':
            self._cals_ioc = float(value)
        self._cal_received.emit(self._calo_ioc, self._cals_ioc)

    def _on_roi_cb(self, pvname='', value=None, **kw):
        if not self._alive or value is None:
            return
        entry = self._roi_pv_idx.get(pvname)
        if entry is None:
            return
        idx, key = entry
        if key == 'nm':
            self._roi_cb_received.emit(idx, {'name': str(value)})
        elif key == 'counts':
            self._roi_cb_received.emit(idx, {'counts': float(value)})
        else:
            self._roi_cb_received.emit(idx, {key: int(value)})

    # ── Qt-thread slots ───────────────────────────────────────────────────────

    def _on_new_spectrum(self, counts: np.ndarray):
        if counts.ndim != 1 or counts.size == 0:
            return
        self._last_counts = counts
        if _HAS_PG:
            x = self._channels_or_kev(np.arange(len(counts), dtype=np.float64))
            self._curve.setData(x, counts.astype(np.float64))
        n = len(counts)
        total = int(counts.sum())
        self._set_status(
            f"● {self._prefix}  |  {n} ch  |  {total:,} cts", "#2ca02c")

    def _on_roi_update(self, idx: int, update: dict):
        self._rois[idx].update(update)
        self._refresh_rois()

    def _on_status_update(self, ertm: float, eltm: float, acqg: bool):
        indicator = "⏺ Acquiring" if acqg else "◼ Idle"
        color = "#e8c44a" if acqg else "#888888"
        extra = self._status_lbl.text()
        # Only update the timing part; leave spectrum info if present
        if "●" in extra:
            base = extra.split("  |")[0]
            self._set_status(
                f"{base}  |  {indicator}  |  RT {ertm:.2f} s  LT {eltm:.2f} s",
                "#2ca02c" if not acqg else color,
            )

    def _on_cal_update(self, calo: float, cals: float):
        self._calo = calo
        self._cals = cals
        has_cal = abs(cals) > 1e-9
        self._chk_kev.setEnabled(has_cal)
        if not has_cal:
            self._chk_kev.setChecked(False)
        # Replot with updated calibration
        if self._last_counts is not None:
            self._on_new_spectrum(self._last_counts)
        self._refresh_rois()

    # ── ROI display ───────────────────────────────────────────────────────────

    def _refresh_rois(self):
        if not _HAS_PG:
            self._rebuild_table()
            return

        active = [(i, r) for i, r in enumerate(self._rois) if r['hi'] > r['lo']]

        # Remove regions for now-inactive ROIs
        for idx in list(self._regions.keys()):
            if idx not in {i for i, _ in active}:
                try:
                    self._plot_widget.removeItem(self._regions.pop(idx))
                except Exception:
                    pass

        show_kev = self._chk_kev.isChecked() and abs(self._cals) > 1e-9

        self._updating_roi = True
        for idx, roi in active:
            lo = roi['lo']
            hi = roi['hi']
            x_lo = self._ch_to_x(lo) if show_kev else float(lo)
            x_hi = self._ch_to_x(hi) if show_kev else float(hi)

            if idx in self._regions:
                self._regions[idx].setRegion((x_lo, x_hi))
            else:
                r, g, b = self._roi_color(idx)
                region = pg.LinearRegionItem(
                    values=(x_lo, x_hi),
                    brush=pg.mkBrush(r, g, b, 60),
                    pen=pg.mkPen(r, g, b, 180),
                    movable=True,
                )
                label = pg.InfLineLabel(
                    region.lines[0],
                    text=roi['name'] or f"ROI{idx}",
                    position=0.9,
                    color=(r, g, b),
                )
                region.sigRegionChangeFinished.connect(
                    lambda reg, i=idx: self._on_region_moved(i, reg))
                region.setVisible(self._roi_visible.get(idx, True))
                self._plot_widget.addItem(region)
                self._regions[idx] = region
        self._updating_roi = False

        self._rebuild_table()

    def _on_region_moved(self, idx: int, region):
        if self._updating_roi:
            return
        lo_x, hi_x = region.getRegion()
        show_kev = self._chk_kev.isChecked() and abs(self._cals) > 1e-9
        if show_kev:
            lo_ch = int(round(self._x_to_ch(lo_x)))
            hi_ch = int(round(self._x_to_ch(hi_x)))
        else:
            lo_ch = int(round(lo_x))
            hi_ch = int(round(hi_x))
        lo_ch = max(0, lo_ch)
        hi_ch = max(lo_ch + 1, hi_ch)
        self._rois[idx]['lo'] = lo_ch
        self._rois[idx]['hi'] = hi_ch
        self._ca_put(f'.R{idx}LO', lo_ch)
        self._ca_put(f'.R{idx}HI', hi_ch)
        self._rebuild_table()

    def _rebuild_table(self):
        self._roi_table.blockSignals(True)
        active = [(i, r) for i, r in enumerate(self._rois) if r['hi'] > r['lo']]
        self._roi_table.setRowCount(len(active))
        has_cal = abs(self._cals) > 1e-9

        for row, (idx, roi) in enumerate(active):
            lo, hi = roi['lo'], roi['hi']
            lo_kev = self._ch_to_x(lo) if has_cal else None
            hi_kev = self._ch_to_x(hi) if has_cal else None

            def _ro(text, align=Qt.AlignmentFlag.AlignCenter) -> QTableWidgetItem:
                it = QTableWidgetItem(text)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                it.setTextAlignment(align)
                return it

            self._roi_table.setItem(row, 0, _ro(str(idx)))

            # Show/hide checkbox
            chk_widget = QWidget()
            chk_lay = QHBoxLayout(chk_widget)
            chk_lay.setContentsMargins(0, 0, 0, 0)
            chk_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk = QCheckBox()
            chk.setChecked(self._roi_visible.get(idx, True))
            chk.toggled.connect(lambda checked, i=idx: self._on_roi_visibility_toggled(i, checked))
            chk_lay.addWidget(chk)
            self._roi_table.setCellWidget(row, 1, chk_widget)

            # Color picker button
            r, g, b = self._roi_color(idx)
            col_btn = QPushButton()
            col_btn.setFixedSize(24, 18)
            col_btn.setStyleSheet(
                f"background: rgb({r},{g},{b}); border: 1px solid #555; border-radius: 2px;")
            col_btn.setToolTip("Click to change ROI colour")
            col_btn.clicked.connect(lambda _c, i=idx: self._on_roi_color_clicked(i))
            col_w = QWidget()
            col_lay = QHBoxLayout(col_w)
            col_lay.setContentsMargins(2, 1, 2, 1)
            col_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_lay.addWidget(col_btn)
            self._roi_table.setCellWidget(row, 2, col_w)

            name_item = QTableWidgetItem(roi['name'])
            name_item.setData(Qt.ItemDataRole.UserRole, idx)
            self._roi_table.setItem(row, 3, name_item)
            self._roi_table.setItem(row, 4, _ro(str(lo)))
            self._roi_table.setItem(row, 5, _ro(str(hi)))
            self._roi_table.setItem(row, 6, _ro(f"{lo_kev:.3f}" if lo_kev is not None else "—"))
            self._roi_table.setItem(row, 7, _ro(f"{hi_kev:.3f}" if hi_kev is not None else "—"))
            self._roi_table.setItem(row, 8, _ro(f"{roi['counts']:.0f}"))

            del_btn = QPushButton("✕")
            del_btn.setStyleSheet("color:#dc6d6d; font-weight:bold; padding:0px 4px;")
            del_btn.setFixedWidth(28)
            del_btn.clicked.connect(lambda _c, i=idx: self._delete_roi(i))
            self._roi_table.setCellWidget(row, 9, del_btn)

        self._roi_table.blockSignals(False)

    def _on_roi_visibility_toggled(self, idx: int, visible: bool):
        self._roi_visible[idx] = visible
        if _HAS_PG and idx in self._regions:
            self._regions[idx].setVisible(visible)

    def _roi_color(self, idx: int) -> tuple:
        if idx not in self._roi_colors:
            r, g, b, _ = _ROI_COLORS[idx % len(_ROI_COLORS)]
            self._roi_colors[idx] = (r, g, b)
        return self._roi_colors[idx]

    def _on_roi_color_clicked(self, idx: int):
        r, g, b = self._roi_color(idx)
        initial = QColor(r, g, b)
        color = QColorDialog.getColor(initial, self, f"ROI {idx} colour")
        if not color.isValid():
            return
        self._roi_colors[idx] = (color.red(), color.green(), color.blue())
        if _HAS_PG and idx in self._regions:
            self._apply_region_color(idx)
        self._rebuild_table()

    def _apply_region_color(self, idx: int):
        r, g, b = self._roi_color(idx)
        region = self._regions[idx]
        region.setBrush(pg.mkBrush(r, g, b, 60))
        pen = pg.mkPen(r, g, b, 180)
        for line in region.lines:
            line.setPen(pen)

    def _on_table_name_edited(self, item: QTableWidgetItem):
        if item.column() != 3:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        name = item.text()
        self._rois[idx]['name'] = name
        self._ca_put(f'.R{idx}NM', name)
        # Update region label
        if _HAS_PG and idx in self._regions:
            for line in self._regions[idx].lines:
                for label in line.getViewBox().allChildren():
                    if isinstance(label, pg.InfLineLabel) and label.line is line:
                        label.setText(name or f"ROI{idx}")

    def _delete_roi(self, idx: int):
        self._rois[idx] = {'lo': 0, 'hi': 0, 'name': '', 'counts': 0.0}
        self._ca_put(f'.R{idx}LO', 0)
        self._ca_put(f'.R{idx}HI', 0)
        self._ca_put(f'.R{idx}NM', '')
        if _HAS_PG and idx in self._regions:
            try:
                self._plot_widget.removeItem(self._regions.pop(idx))
            except Exception:
                pass
        self._rebuild_table()

    # ── Add ROI ───────────────────────────────────────────────────────────────

    def _on_add_roi(self):
        # Find first inactive slot
        slot = next((i for i, r in enumerate(self._rois) if r['hi'] <= r['lo']), None)
        if slot is None:
            self._set_status(f"⚠ All {self._n_rois} ROI slots are in use", "#e05050")
            return

        name, ok = QInputDialog.getText(self, "Add ROI", f"Name for ROI {slot}:")
        if not ok:
            return

        if _HAS_PG:
            vr = self._plot_widget.viewRange()[0]
            center = (vr[0] + vr[1]) / 2.0
        else:
            center = 512.0

        show_kev = self._chk_kev.isChecked() and abs(self._cals) > 1e-9
        if show_kev:
            lo_ch = max(0, int(round(self._x_to_ch(center - 0.05))))
            hi_ch = max(lo_ch + 1, int(round(self._x_to_ch(center + 0.05))))
        else:
            lo_ch = max(0, int(round(center)) - 50)
            hi_ch = max(lo_ch + 1, int(round(center)) + 50)

        self._rois[slot] = {'lo': lo_ch, 'hi': hi_ch,
                            'name': name, 'counts': 0.0}
        self._ca_put(f'.R{slot}LO', lo_ch)
        self._ca_put(f'.R{slot}HI', hi_ch)
        self._ca_put(f'.R{slot}NM', name)
        self._refresh_rois()

    # ── Cursor ────────────────────────────────────────────────────────────────

    def _on_mouse_moved(self, pos):
        if not _HAS_PG:
            return
        vb = self._plot_widget.getPlotItem().getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            self._cursor_lbl.setText("")
            return
        mp = vb.mapSceneToView(pos)
        x = mp.x()
        x_min, x_max = vb.viewRange()[0]
        x = max(x_min, min(x_max, x))
        self._cursor_line.setPos(x)

        counts_str = ""
        if self._last_counts is not None:
            show_kev = self._chk_kev.isChecked() and abs(self._cals) > 1e-9
            ch = int(round(self._x_to_ch(x))) if show_kev else int(round(x))
            if 0 <= ch < len(self._last_counts):
                counts_str = f"  counts: {int(self._last_counts[ch])}"

        show_kev = self._chk_kev.isChecked() and abs(self._cals) > 1e-9
        if show_kev:
            ch = int(round(self._x_to_ch(x)))
            self._cursor_lbl.setText(f"ch: {ch}  keV: {x:.3f}{counts_str}")
        else:
            ch = int(round(x))
            kev_str = ""
            if abs(self._cals) > 1e-9:
                kev_str = f"  keV: {self._ch_to_x(ch):.3f}"
            self._cursor_lbl.setText(f"ch: {ch}{kev_str}{counts_str}")

    # ── Display toggles ───────────────────────────────────────────────────────

    def _on_live_toggled(self, checked: bool):
        self._live = checked

    def _on_read_now(self):
        for pv in self._pvs:
            val_pv = self._prefix if self._sep == '_' else f"{self._prefix}.VAL"
            if pv.pvname == val_pv:
                try:
                    val = pv.get(timeout=2.0)
                    if val is not None:
                        self._spectrum_received.emit(np.asarray(val).copy())
                except Exception:
                    pass
                break

    def _on_logy_toggled(self, checked: bool):
        if not _HAS_PG:
            return
        self._plot_widget.setLogMode(y=checked)

    def _on_kev_toggled(self, _checked: bool):
        if self._last_counts is not None:
            self._on_new_spectrum(self._last_counts)
        self._refresh_rois()
        if self._fit_params:
            self._draw_fit_curves()
        label = "Energy (keV)" if _checked else "Channel"
        if _HAS_PG:
            self._plot_widget.setLabel('bottom', label)

    # ── Acquire controls ──────────────────────────────────────────────────────

    def _on_erase_start(self):  self._ca_put('.ERST', 1)
    def _on_start(self):        self._ca_put('.STRT', 1)
    def _on_stop(self):         self._ca_put('.STOP', 1)

    def _ca_put(self, field: str, value):
        """field may start with '.' (e.g. '.R0LO'); sep is applied automatically."""
        try:
            import epics
            bare = field.lstrip('.')
            epics.caput(f"{self._prefix}{self._sep}{bare}", value, wait=False)
        except Exception:
            pass

    # ── Calibration helpers ───────────────────────────────────────────────────

    def _ch_to_x(self, ch: float) -> float:
        """Channel → keV (if calibrated)."""
        return (self._calo + self._cals * ch) / 1000.0

    def _x_to_ch(self, x: float) -> float:
        """keV → channel (if calibrated)."""
        if abs(self._cals) < 1e-12:
            return x
        return (x * 1000.0 - self._calo) / self._cals

    def _channels_or_kev(self, channels: np.ndarray) -> np.ndarray:
        if self._chk_kev.isChecked() and abs(self._cals) > 1e-9:
            return (self._calo + self._cals * channels) / 1000.0
        return channels

    # ── Settings ──────────────────────────────────────────────────────────────

    def _restore_settings(self):
        saved = _load_settings().get(self._device_name, {})
        prefix = saved.get('prefix', '')
        if prefix and not self._prefix:
            self._prefix = prefix
            self._prefix_edit.setText(prefix)
        if 'preset' in saved:
            self._spin_preset.setValue(float(saved['preset']))
        if saved.get('log_y'):
            self._chk_logy.setChecked(True)
        hdf_prefix = saved.get('hdf_prefix', '')
        if hdf_prefix:
            self._hdf_prefix_edit.setText(hdf_prefix)
            QTimer.singleShot(300, lambda: self._connect_hdf(hdf_prefix))

    def _save_settings(self):
        settings = _load_settings()
        settings.setdefault(self._device_name, {}).update({
            'prefix':     self._prefix,
            'preset':     self._spin_preset.value(),
            'log_y':      self._chk_logy.isChecked(),
            'show_kev':   self._chk_kev.isChecked(),
            'hdf_prefix': self._hdf_prefix,
        })
        _save_settings(settings)

    # ── Fe-55 calibration ─────────────────────────────────────────────────────

    def _fit_gaussian_centroid(self, counts: np.ndarray, center: int,
                               half_win: int = 60) -> tuple:
        """Return (centroid_ch, popt) or (None, None) on failure.
        popt = [amplitude, mu_ch, sigma_ch]."""
        lo = max(0, center - half_win)
        hi = min(len(counts), center + half_win)
        x = np.arange(lo, hi, dtype=float)
        y = counts[lo:hi].astype(float)
        if y.max() < 5:
            return None, None
        try:
            def _gauss(x, amp, mu, sig):
                return amp * np.exp(-0.5 * ((x - mu) / sig) ** 2)
            p0 = [y.max(), float(center), 10.0]
            bounds = ([0, lo, 0.5], [y.max() * 2, hi, half_win])
            popt, _ = _curve_fit(_gauss, x, y, p0=p0, bounds=bounds, maxfev=5000)
            return float(popt[1]), popt
        except Exception:
            return None, None

    def _on_autofit_fe55(self):
        if not _HAS_SCIPY:
            self._set_status("⚠ scipy not installed — pip install scipy", "#e05050")
            return
        if self._last_counts is None:
            self._set_status("⚠ No spectrum loaded", "#e05050")
            return
        counts = self._last_counts
        # Use relaxed thresholds — Mn Kβ is only ~13.5 % of Kα intensity
        peaks, props = _find_peaks(counts, height=counts.max() * 0.05,
                                   distance=20, prominence=counts.max() * 0.02)
        if len(peaks) < 2:
            self._set_status("⚠ Could not find two peaks for Fe-55 calibration",
                             "#e05050")
            return
        # Take the two tallest peaks
        heights = props['peak_heights']
        top2 = peaks[np.argsort(heights)[-2:]]
        ka_ch, kb_ch = sorted(top2)
        ka_fit, ka_popt = self._fit_gaussian_centroid(counts, ka_ch)
        kb_fit, kb_popt = self._fit_gaussian_centroid(counts, kb_ch)

        # Block spinbox signals while updating both values to avoid
        # _on_cal_channels_changed firing with a partially-updated state
        self._spin_ka.blockSignals(True)
        self._spin_kb.blockSignals(True)
        if ka_fit is not None:
            self._spin_ka.setValue(ka_fit)
        if kb_fit is not None:
            self._spin_kb.setValue(kb_fit)
        self._spin_ka.blockSignals(False)
        self._spin_kb.blockSignals(False)
        self._on_cal_channels_changed()   # fire once with both values set

        self._fit_params = []
        if ka_popt is not None:
            self._fit_params.append((ka_popt, self._color_for_channel(ka_fit)))
        if kb_popt is not None:
            self._fit_params.append((kb_popt, self._color_for_channel(kb_fit)))
        self._draw_fit_curves()
        self._update_fit_report(ka_popt, kb_popt)

    def _update_fit_report(self, ka_popt, kb_popt):
        _FWHM = 2.3548  # 2*sqrt(2*ln2)
        lines = []
        for label, popt in (("Kα", ka_popt), ("Kβ", kb_popt)):
            if popt is None:
                lines.append(f"Mn {label}: fit failed")
                continue
            amp, mu, sig = popt
            sig = abs(sig)
            fwhm = _FWHM * sig
            lines.append(f"Mn {label}:  μ={mu:.1f} ch  σ={sig:.1f} ch  FWHM={fwhm:.1f} ch")
        self._fit_report_lbl.setText("\n".join(lines))

    def _color_for_channel(self, ch: float) -> tuple:
        """Return (r, g, b) of the ROI region that contains ch, else a fallback."""
        for idx, roi in enumerate(self._rois):
            lo, hi = roi['lo'], roi['hi']
            if lo < hi and lo <= ch <= hi:
                rgb = self._roi_colors.get(idx, _ROI_COLORS[idx % len(_ROI_COLORS)])
                return rgb[:3]
        return (255, 220, 50)   # golden fallback

    def _clear_fit_curves(self):
        if not _HAS_PG:
            return
        for item in self._fit_curve_items:
            try:
                self._plot_widget.removeItem(item)
            except Exception:
                pass
        self._fit_curve_items.clear()

    def _on_show_fit_toggled(self, checked: bool):
        for item in self._fit_curve_items:
            item.setVisible(checked)

    def _draw_fit_curves(self):
        if not _HAS_PG:
            return
        self._clear_fit_curves()
        for popt, rgb in self._fit_params:
            amp, mu_ch, sigma_ch = popt
            r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])

            # Gaussian envelope
            half_win = max(60, abs(sigma_ch) * 4)
            ch_x = np.linspace(mu_ch - half_win, mu_ch + half_win, 300)
            y = amp * np.exp(-0.5 * ((ch_x - mu_ch) / sigma_ch) ** 2)
            x = self._channels_or_kev(ch_x)
            curve = pg.PlotCurveItem(
                x, y,
                pen=pg.mkPen(color=(r, g, b, 220), width=1.5,
                             style=Qt.PenStyle.DashLine))
            self._plot_widget.addItem(curve, ignoreBounds=True)
            self._fit_curve_items.append(curve)

            # Vertical line at peak centroid
            mu_x = float(self._channels_or_kev(np.array([mu_ch]))[0])
            vline = pg.InfiniteLine(
                pos=mu_x, angle=90, movable=False,
                pen=pg.mkPen(color=(r, g, b, 255), width=1.5))
            self._plot_widget.addItem(vline, ignoreBounds=True)
            self._fit_curve_items.append(vline)

        visible = self._chk_show_fit.isChecked()
        for item in self._fit_curve_items:
            item.setVisible(visible)

    def _on_cal_channels_changed(self):
        ka_ch = self._spin_ka.value()
        kb_ch = self._spin_kb.value()
        if ka_ch <= 0 or kb_ch <= 0 or abs(kb_ch - ka_ch) < 1:
            self._cal_result_lbl.setText("Gain:   —\nOffset: —")
            self._btn_apply_cal.setEnabled(False)
            self._btn_write_ioc.setEnabled(False)
            return
        # Two-point linear fit: E(ch) = offset + gain * ch  (in eV)
        gain   = (_FE55_KB_EV - _FE55_KA_EV) / (kb_ch - ka_ch)
        offset = _FE55_KA_EV - gain * ka_ch
        self._pending_cal_gain   = gain
        self._pending_cal_offset = offset
        self._cal_result_lbl.setText(
            f"Gain:   {gain:.4f} eV/ch\nOffset: {offset:.2f} eV"
        )
        self._btn_apply_cal.setEnabled(True)
        self._btn_write_ioc.setEnabled(bool(self._prefix))

    def _on_apply_calibration(self):
        self._calo = self._pending_cal_offset
        self._cals = self._pending_cal_gain
        self._chk_kev.setEnabled(True)
        if self._last_counts is not None:
            self._on_new_spectrum(self._last_counts)
        self._refresh_rois()

    def _on_write_cal_to_ioc(self):
        self._ca_put('.CALO', self._pending_cal_offset)
        self._ca_put('.CALS', self._pending_cal_gain)

    def _on_clear_calibration(self):
        self._spin_ka.setValue(0.0)
        self._spin_kb.setValue(0.0)
        self._cal_result_lbl.setText("Gain:   —\nOffset: —")
        self._btn_apply_cal.setEnabled(False)
        self._btn_write_ioc.setEnabled(False)
        self._fit_params.clear()
        self._clear_fit_curves()
        self._fit_report_lbl.setText("")
        # Revert to IOC calibration
        self._calo = self._calo_ioc
        self._cals = self._cals_ioc
        has_cal = abs(self._cals) > 1e-9
        self._chk_kev.setEnabled(has_cal)
        if not has_cal:
            self._chk_kev.setChecked(False)
        if self._last_counts is not None:
            self._on_new_spectrum(self._last_counts)
        self._refresh_rois()

    # ── HDF file info ─────────────────────────────────────────────────────────

    _HDF_MODE_NAMES = {0: "Single", 1: "Capture", 2: "Stream"}

    @staticmethod
    def _decode_epics_str(value) -> str:
        """EPICS char-waveform PVs arrive as int arrays — decode to ASCII string."""
        if isinstance(value, str):
            return value.rstrip('\x00').strip()
        try:
            return bytes(int(v) for v in value if v).decode('ascii', errors='replace').strip()
        except Exception:
            return str(value)

    def _on_hdf_connect(self):
        prefix = self._hdf_prefix_edit.text().strip()
        if not prefix:
            return
        self._connect_hdf(prefix)

    def _connect_hdf(self, prefix: str):
        for pv in self._hdf_pvs:
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._hdf_pvs.clear()
        self._hdf_prefix = prefix
        self._hdf_num_capture = 0

        try:
            import epics
        except ImportError:
            return

        _fields = [
            "FilePath_RBV",
            "FullFileName_RBV",
            "FileWriteMode_RBV",
            "Capture_RBV",
            "NumCaptured_RBV",
            "NumCapture",
        ]
        for field in _fields:
            pvname = prefix + field

            def _cb(pvname='', value=None, field=field, **kw):
                if not self._alive or value is None:
                    return
                self._hdf_pv_received.emit(field, value)

            pv = epics.PV(pvname, auto_monitor=True, callback=_cb)
            self._hdf_pvs.append(pv)

    def _on_hdf_pv_update(self, field: str, value):
        if field == "FilePath_RBV":
            path = self._decode_epics_str(value)
            self._hdf_path_lbl.setText(path or "—")
            self._hdf_path_lbl.setToolTip(path)
        elif field == "FullFileName_RBV":
            name = self._decode_epics_str(value)
            self._hdf_name_lbl.setText(name or "—")
            self._hdf_name_lbl.setToolTip(name)
        elif field == "FileWriteMode_RBV":
            try:
                mode = int(value)
            except (TypeError, ValueError):
                mode = -1
            self._hdf_mode_lbl.setText(self._HDF_MODE_NAMES.get(mode, str(value)))
        elif field == "Capture_RBV":
            capturing = bool(int(value))
            if capturing:
                self._hdf_status_lbl.setText("● Capturing")
                self._hdf_status_lbl.setStyleSheet("color:#6ddc6d; font-size:10px;")
            else:
                self._hdf_status_lbl.setText("Idle")
                self._hdf_status_lbl.setStyleSheet("font-size:10px;")
        elif field == "NumCapture":
            try:
                self._hdf_num_capture = int(value)
            except (TypeError, ValueError):
                self._hdf_num_capture = 0
            self._hdf_captured_lbl.setText(
                f"— / {self._hdf_num_capture}" if self._hdf_num_capture else "—")
        elif field == "NumCaptured_RBV":
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 0
            total = self._hdf_num_capture
            self._hdf_captured_lbl.setText(
                f"{n} / {total}" if total else str(n))

    # ── Status label ─────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = "#888888"):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(f"color:{color};")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent):
        self._alive = False
        for pv in self._pvs + self._hdf_pvs:
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()
        self._hdf_pvs.clear()
        self._save_settings()
        super().closeEvent(event)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _btn(text: str, bg: str, fg: str) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(f"background:{bg}; color:{fg}; font-weight:bold;")
    return b
