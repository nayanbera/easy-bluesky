"""ad_viewer.py — Live area-detector image viewer via PVAccess + pyqtgraph."""

import json
import threading
import time
from pathlib import Path

import numpy as np

# ── Per-device persistent settings ──────────────────────────────────────────────

_AD_SETTINGS_PATH = Path.home() / ".easy_bluesky" / "ad_viewer_settings.json"


def load_ad_settings() -> dict:
    """Return the full settings dict from disk, or {} on any error."""
    try:
        if _AD_SETTINGS_PATH.exists():
            return json.loads(_AD_SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def save_ad_settings(settings: dict):
    """Write the full settings dict to disk atomically."""
    try:
        _AD_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _AD_SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

try:
    import pyqtgraph as pg
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

try:
    from p4p.client.thread import Context as _PVAContext  # noqa: F401
    _HAS_P4P = True
except ImportError:
    _HAS_P4P = False

_COLORMAPS    = ["viridis", "inferno", "plasma", "gray", "CET-R4"]
_MAX_FPS      = 20
_MIN_INTERVAL = 1.0 / _MAX_FPS


# ── PVA monitor thread ───────────────────────────────────────────────────────────

class _PVAMonitorThread(QThread):
    """Background thread that owns a p4p Context and emits frame signals."""

    new_frame          = pyqtSignal(object, object)   # (np.ndarray, dict)
    connection_changed = pyqtSignal(bool)
    error_occurred     = pyqtSignal(str)

    def __init__(self, pva_pv: str, pva_host: str = "", parent=None):
        super().__init__(parent)
        self._pva_pv    = pva_pv
        self._pva_host  = pva_host.strip()
        self._stop_evt  = threading.Event()
        self._last_emit = 0.0
        self._fps       = 0.0

    def run(self):
        import os
        from p4p.client.thread import Context

        # Prefer the explicit beamline host (from connection profile) for PVA
        # unicast routing.  CA uses broadcast (.255) addresses that p4p rejects;
        # the profile's 'host' field is the real unicast IP we need.
        conf = {}
        addr = (self._pva_host
                or os.environ.get('EPICS_PVA_ADDR_LIST', '').strip())
        if addr:
            conf['EPICS_PVA_ADDR_LIST']      = addr
            conf['EPICS_PVA_AUTO_ADDR_LIST'] = 'NO'

        ctx = Context('pva', conf=conf) if conf else Context('pva')
        try:
            sub = ctx.monitor(self._pva_pv, self._on_value, notify_disconnect=True)
            try:
                self._stop_evt.wait()
            finally:
                sub.close()
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            ctx.close()

    def stop_monitor(self):
        self._stop_evt.set()
        if not self.wait(1500):   # 1.5 s for sub.close() + ctx.close()
            self.terminate()      # force-kill if p4p ctx.close() hung
            self.wait(500)

    def _on_value(self, value):
        if value is None or isinstance(value, Exception):
            self.connection_changed.emit(False)
            return
        now = time.monotonic()
        if now - self._last_emit < _MIN_INTERVAL:
            return
        dt = now - self._last_emit
        if self._last_emit > 0 and dt > 0:
            self._fps = 0.8 * self._fps + 0.2 / dt
        self._last_emit = now
        arr, err = _extract_ndarray(value)
        if arr is None:
            self.error_occurred.emit(f"Frame decode: {err}  (type={type(value).__name__})")
            return
        uid = 0
        try:
            uid = int(value['uniqueId'])
        except Exception:
            pass
        self.connection_changed.emit(True)
        self.new_frame.emit(arr, {'unique_id': uid, 'shape': arr.shape,
                                  'dtype': str(arr.dtype), 'fps': self._fps})


class _CAInitThread(QThread):
    """Background thread: reads initial CA values once PVs have connected.

    All blocking pv.get() calls run here so the main thread is never stalled.
    Retries up to _MAX_ATTEMPTS times (≈15 s total) before giving up.
    """
    init_done = pyqtSignal(object, object, dict)  # (exp_time, exp_period, mode_info)

    _MAX_ATTEMPTS = 10

    def __init__(self, ca_pvs: dict, parent=None):
        super().__init__(parent)
        self._ca_pvs = ca_pvs

    def run(self):
        c = self._ca_pvs
        _MODES = {
            'image_mode':   ('image_mode_rbv',  'Image Mode:'),
            'trigger_mode': ('trigger_mode_rbv', 'Trigger Mode:'),
        }
        for attempt in range(self._MAX_ATTEMPTS):
            if self.isInterruptionRequested():
                return

            # Interruption-check between every blocking pv.get() so the thread
            # responds within 1 s regardless of which call it's in.
            exp_time = _pv_get(c.get('acquire_time_rbv'))
            if self.isInterruptionRequested():
                return
            exp_period = _pv_get(c.get('acquire_period_rbv'))
            if self.isInterruptionRequested():
                return

            connected: dict = {}
            for key, (rbv_key, lbl) in _MODES.items():
                if self.isInterruptionRequested():
                    return
                pv = c.get(key)
                strs = list(getattr(pv, 'enum_strs', None) or []) if pv else []
                if strs:
                    current = _pv_get(c.get(rbv_key), as_string=True)
                    if self.isInterruptionRequested():
                        return
                    connected[key] = (rbv_key, lbl, strs, current)

            if connected:
                self.init_done.emit(exp_time, exp_period, connected)
                return

            # Wait 1.5 s in short chunks so interruption is responsive
            for _ in range(15):
                if self.isInterruptionRequested():
                    return
                time.sleep(0.1)

        # Timed out — emit with whatever we gathered
        self.init_done.emit(None, None, {})


def _extract_ndarray(value) -> tuple:
    """Return (np.ndarray, "") on success or (None, error_str) on failure.

    Tries two strategies:
    1. np.asarray(value) — works when p4p already wraps NTNDArray as ntndarray
       (the case for both get() and monitor() with nt=True in recent p4p versions).
    2. Field access value['value'] / value['dimension'] — raw p4p Value fallback.
    """
    # Strategy 1: value is already array-like (ntndarray)
    try:
        arr = np.asarray(value)
        if arr.ndim >= 2 and arr.size > 0:
            return arr.copy(), ""
    except Exception:
        pass

    # Strategy 2: raw p4p Value with explicit NTNDArray field access
    try:
        dims = value['dimension']
        if not dims or len(dims) < 1:
            return None, f"no dimension field (dims={dims!r})"
        nx = int(dims[0]['size'])
        ny = int(dims[1]['size']) if len(dims) > 1 else 1
        if nx == 0 or ny == 0:
            return None, f"zero dimension nx={nx} ny={ny}"
        data = np.asarray(value['value']).ravel()
        if data.size < nx * ny:
            return None, f"data size {data.size} < expected {nx*ny}"
        return data[: nx * ny].reshape(ny, nx).copy(), ""
    except Exception as exc:
        return None, str(exc)


# ── Main viewer window ───────────────────────────────────────────────────────────

class ADViewerWindow(QMainWindow):
    """Floating live-view window for one EPICS area detector via PVAccess."""

    # CA-thread → main-thread signals for ROI1 RBV and Stats1 live values
    _sig_roi1_rbv = pyqtSignal(int, int, int, int)  # minx, miny, sizex, sizey
    _sig_stats1   = pyqtSignal(dict)                 # {stat_key: float}

    def __init__(
        self,
        device_name: str,
        prefix: str,       # e.g. "15PS1:" — must include trailing colon
        pv_map: dict,      # {sig_name: pvname} for this device (informational)
        pva_host: str = "",  # beamline unicast host for PVA routing (profile 'host')
        parent=None,
    ):
        super().__init__(parent)
        if not _HAS_PG:
            raise RuntimeError("pyqtgraph is required for ADViewerWindow")

        self._device_name = device_name
        self._prefix      = prefix
        self._cam_pfx     = f"{prefix}cam1:"
        self._pva_pv      = f"{prefix}Pva1:Image"

        self._arr: np.ndarray | None = None
        self._log_scale = False
        self._transpose = False
        self._roi_on    = False
        self._frame_cnt = 0

        self._pva_host = pva_host.strip()
        self._ca_pvs: dict                      = {}
        self._thread: _PVAMonitorThread | None  = None
        self._ca_init_thread: _CAInitThread | None = None
        self._crosshair_on = False
        self._active_mode_key = 'image_mode'   # updated in _read_ca_initial
        self._mask_enabled   = False
        self._mask_threshold: float | None = None

        self._cam1_pvs = _resolve_cam1_pvs(pv_map)

        self._roi_updating    = False   # True while syncing overlay from CA (suppress write-back)
        self._stats1_vals: dict    = {}  # latest Stats1 readings {key: float}
        self._roi1_rbv_cache: dict = {}  # partial RBV values collected across 4 callbacks

        self._sig_roi1_rbv.connect(self._apply_roi1_from_ca)
        self._sig_stats1.connect(self._on_stats1_update)

        self._roi_debounce_timer = QTimer(self)
        self._roi_debounce_timer.setSingleShot(True)
        self._roi_debounce_timer.setInterval(150)
        self._roi_debounce_timer.timeout.connect(self._write_roi_to_ad)

        self._build_ui()
        self._restore_display_settings()   # apply saved colormap/log/transpose
        self._connect_ca_pvs()

        if _HAS_P4P:
            self._start_pva()
        else:
            self._set_status(
                "⚠ p4p not installed — install with: pip install p4p", "#e05050")

    # ── UI construction ──────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle(f"AD Viewer — {self._device_name}")
        self.resize(1100, 720)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        root.addWidget(self._build_ctrl())

        # Image view with x/y pixel axes
        self._plot_item = pg.PlotItem()
        self._plot_item.setLabel('bottom', 'x (px)')
        self._plot_item.setLabel('left', 'y (px)')
        self._img_view = pg.ImageView(view=self._plot_item)
        self._img_view.ui.roiBtn.setVisible(False)
        self._img_view.ui.menuBtn.setVisible(False)
        root.addWidget(self._img_view, stretch=1)

        # Crosshair overlay
        _ch_pen = pg.mkPen('y', width=1, style=Qt.PenStyle.DashLine)
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=_ch_pen)
        self._hline = pg.InfiniteLine(angle=0,  movable=False, pen=_ch_pen)
        self._img_view.addItem(self._vline)
        self._img_view.addItem(self._hline)
        self._vline.setVisible(False)
        self._hline.setVisible(False)
        self._mouse_proxy = pg.SignalProxy(
            self._img_view.scene.sigMouseMoved,
            rateLimit=60,
            slot=self._on_mouse_moved,
        )

        # Draggable rectangular ROI (hidden by default)
        self._roi = pg.RectROI(
            [50, 50], [100, 100],
            pen=pg.mkPen('r', width=2),
            removable=False,
        )
        self._roi.addScaleHandle([1, 1], [0, 0])
        self._roi.addScaleHandle([0, 0], [1, 1])
        self._roi.addScaleHandle([1, 0], [0, 1])
        self._roi.addScaleHandle([0, 1], [1, 0])
        self._roi.sigRegionChanged.connect(self._on_roi_region_changed)
        self._img_view.addItem(self._roi)
        self._roi.setVisible(False)

        self._status_lbl = QLabel("● Connecting…")
        self.statusBar().addWidget(self._status_lbl, 1)
        self._crosshair_lbl = QLabel("")
        self._crosshair_lbl.setStyleSheet(
            "font-family:monospace; color:#1a1a1a; min-width:180px;")
        self.statusBar().addPermanentWidget(self._crosshair_lbl)
        self._fps_lbl = QLabel("—")
        self.statusBar().addPermanentWidget(self._fps_lbl)

    def _build_ctrl(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(230)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(2, 4, 4, 4)
        lay.setSpacing(8)

        # ── PVA stream PV + detector host ─────────────────────────────────
        gp = QGroupBox("PVA Connection")
        gpl = QVBoxLayout(gp)
        gpl.setSpacing(4)

        gpl.addWidget(QLabel("Image PV:"))
        self._pv_edit = QLineEdit(self._pva_pv)
        self._pv_edit.setPlaceholderText("e.g. 15PS1:Pva1:Image")
        self._pv_edit.setToolTip("PVAccess PV for the image stream")
        self._pv_edit.returnPressed.connect(self._on_pva_reconnect)
        gpl.addWidget(self._pv_edit)

        gpl.addWidget(QLabel("Detector host:"))
        self._host_edit = QLineEdit(self._pva_host)
        self._host_edit.setPlaceholderText("e.g. 164.54.169.50")
        self._host_edit.setToolTip("Host IP/hostname for PVAccess unicast routing")
        self._host_edit.returnPressed.connect(self._on_pva_reconnect)
        gpl.addWidget(self._host_edit)

        btn_connect = QPushButton("Connect")
        btn_connect.clicked.connect(self._on_pva_reconnect)
        gpl.addWidget(btn_connect)
        lay.addWidget(gp)

        # ── Acquire ──────────────────────────────────────────────────────
        g = QGroupBox("Acquire")
        gl = QVBoxLayout(g)
        gl.setSpacing(5)

        row = QHBoxLayout()
        self._btn_acq  = _btn("▶  Acquire", "#1a3a1a", "#6ddc6d")
        self._btn_stop = _btn("■  Stop",    "#3a1a1a", "#dc6d6d")
        self._btn_acq.clicked.connect(self._on_acquire)
        self._btn_stop.clicked.connect(self._on_stop)
        row.addWidget(self._btn_acq)
        row.addWidget(self._btn_stop)
        gl.addLayout(row)

        # Label and enum strings are set dynamically in _read_ca_initial after
        # determining which mode PV (ImageMode vs TriggerMode) actually connects.
        self._mode_lbl_widget = QLabel("Mode:")
        gl.addWidget(self._mode_lbl_widget)
        self._cmb_mode = QComboBox()
        self._cmb_mode.addItems(["(connecting…)"])
        self._cmb_mode.setEnabled(False)
        self._cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        gl.addWidget(self._cmb_mode)

        for lbl_text, attr in [("Exposure:", "_spin_exp"), ("Period:", "_spin_per")]:
            r = QHBoxLayout()
            r.addWidget(QLabel(lbl_text))
            sb = _spinbox(0.0001, 3600.0, 4, " s", 0.1)
            setattr(self, attr, sb)
            r.addWidget(sb)
            gl.addLayout(r)
        self._spin_exp.editingFinished.connect(self._on_exp_changed)
        self._spin_per.editingFinished.connect(self._on_per_changed)
        lay.addWidget(g)

        # ── Display ──────────────────────────────────────────────────────
        g2 = QGroupBox("Display")
        g2l = QVBoxLayout(g2)
        g2l.setSpacing(5)

        self._chk_log   = QCheckBox("Log scale  (log₁₊ₓ)")
        self._chk_xps   = QCheckBox("Transpose image")
        self._chk_xhair = QCheckBox("Crosshair")
        self._chk_log.toggled.connect(self._on_log_toggled)
        self._chk_xps.toggled.connect(self._on_transpose_toggled)
        self._chk_xhair.toggled.connect(self._on_crosshair_toggled)
        g2l.addWidget(self._chk_log)
        g2l.addWidget(self._chk_xps)
        g2l.addWidget(self._chk_xhair)

        crow = QHBoxLayout()
        crow.addWidget(QLabel("Colormap:"))
        self._cmb_cmap = QComboBox()
        self._cmb_cmap.addItems(_COLORMAPS)
        self._cmb_cmap.currentTextChanged.connect(self._apply_colormap)
        crow.addWidget(self._cmb_cmap)
        g2l.addLayout(crow)

        for lbl_txt, attr in [("Min:", "_edit_lev_min"), ("Max:", "_edit_lev_max")]:
            r = QHBoxLayout()
            r.addWidget(QLabel(lbl_txt))
            ed = QLineEdit()
            ed.setPlaceholderText("auto")
            ed.returnPressed.connect(self._on_levels_changed)
            setattr(self, attr, ed)
            r.addWidget(ed)
            g2l.addLayout(r)

        self._chk_auto_levels = QCheckBox("Auto Levels")
        self._chk_auto_levels.setChecked(True)
        self._chk_auto_levels.toggled.connect(self._on_auto_levels_toggled)
        g2l.addWidget(self._chk_auto_levels)

        btn_zoom = QPushButton("Reset Zoom")
        btn_zoom.clicked.connect(lambda: self._plot_item.getViewBox().autoRange())
        g2l.addWidget(btn_zoom)
        lay.addWidget(g2)

        # ── ROI ──────────────────────────────────────────────────────────
        g3 = QGroupBox("ROI")
        g3l = QVBoxLayout(g3)
        g3l.setSpacing(4)

        self._chk_roi = QCheckBox("Enable ROI")
        self._chk_roi.toggled.connect(self._on_roi_toggled)
        g3l.addWidget(self._chk_roi)

        # ROI coordinate spinboxes (enabled only when ROI is on)
        fl = QFormLayout()
        fl.setContentsMargins(0, 2, 0, 2)
        fl.setSpacing(2)
        fl.setHorizontalSpacing(4)
        for attr, lo, label in [
            ('_spin_roi_minx',  0, 'MinX:'),
            ('_spin_roi_miny',  0, 'MinY:'),
            ('_spin_roi_sizex', 1, 'SizeX:'),
            ('_spin_roi_sizey', 1, 'SizeY:'),
        ]:
            sb = QSpinBox()
            sb.setRange(lo, 65535)
            sb.setEnabled(False)
            sb.wheelEvent = lambda e: e.ignore()
            sb.editingFinished.connect(self._on_roi_spinbox_changed)
            setattr(self, attr, sb)
            fl.addRow(label, sb)
        g3l.addLayout(fl)

        self._roi_lbl = QLabel("")
        self._roi_lbl.setWordWrap(True)
        self._roi_lbl.setStyleSheet("font-family:monospace; font-size:10px;")
        g3l.addWidget(self._roi_lbl)
        lay.addWidget(g3)

        # ── Bad Pixel Mask ────────────────────────────────────────────
        g4 = QGroupBox("Bad Pixel Mask")
        g4l = QVBoxLayout(g4)
        g4l.setSpacing(4)

        self._chk_mask = QCheckBox("Enable")
        self._chk_mask.toggled.connect(self._on_mask_toggled)
        g4l.addWidget(self._chk_mask)

        mr = QHBoxLayout()
        mr.addWidget(QLabel("Max value:"))
        self._edit_mask_thresh = QLineEdit()
        self._edit_mask_thresh.setPlaceholderText("e.g. 1e6")
        self._edit_mask_thresh.setToolTip(
            "Pixels with intensity above this value are set to 0 before display")
        self._edit_mask_thresh.returnPressed.connect(self._on_mask_threshold_changed)
        self._edit_mask_thresh.editingFinished.connect(self._on_mask_threshold_changed)
        mr.addWidget(self._edit_mask_thresh)
        g4l.addLayout(mr)
        lay.addWidget(g4)

        lay.addStretch()
        return panel

    # ── CA controls ──────────────────────────────────────────────────────────────

    def _connect_ca_pvs(self):
        try:
            import epics
        except ImportError:
            return
        p = self._cam_pfx
        r = self._cam1_pvs

        def _pv(role: str, fallback: str) -> str:
            return r.get(role, f"{p}{fallback}")

        # Connect to BOTH TriggerMode and ImageMode — _apply_ca_initial will probe
        # which one has valid enum strings and use that as the active mode control.
        for key, pvname in {
            'acquire':            _pv('acquire',        'Acquire'),
            'acquire_rbv':        _pv('acquire',        'Acquire') + '_RBV',
            'acquire_time':       _pv('acquire_time',   'AcquireTime'),
            'acquire_time_rbv':   _pv('acquire_time',   'AcquireTime') + '_RBV',
            'acquire_period':     _pv('acquire_period', 'AcquirePeriod'),
            'acquire_period_rbv': _pv('acquire_period', 'AcquirePeriod') + '_RBV',
            'trigger_mode':       _pv('trigger_mode',   'TriggerMode'),
            'trigger_mode_rbv':   _pv('trigger_mode',   'TriggerMode') + '_RBV',
            'image_mode':         _pv('image_mode',     'ImageMode'),
            'image_mode_rbv':     _pv('image_mode',     'ImageMode') + '_RBV',
        }.items():
            self._ca_pvs[key] = epics.PV(pvname)
        # ROI1/Stats1 PVs are created lazily when the user enables the ROI overlay
        # (see _ensure_roi_pvs).  Creating them eagerly would generate continuous CA
        # search broadcasts for potentially non-existent PVs.

        # Defer CA init to a background thread — pv.get(timeout=1.0) would block
        # the main thread if the detector PVs are unreachable (SimDetector, etc.)
        QTimer.singleShot(1500, self._start_ca_init_thread)

    def _start_ca_init_thread(self):
        """Launch _CAInitThread to read CA initial values without blocking the UI."""
        # Hold the reference via self._ca_init_thread.  Clear it only from the
        # finished signal — not from init_done — so the QThread object is never
        # garbage-collected while Qt's post-run() cleanup is still in progress.
        # (Destroying a QThread before finished fires causes "QThread: Destroyed
        # while thread is still running" and an abort on macOS.)
        self._ca_init_thread = _CAInitThread(self._ca_pvs)
        self._ca_init_thread.init_done.connect(self._apply_ca_initial)
        self._ca_init_thread.finished.connect(self._on_ca_init_finished)
        self._ca_init_thread.start()

    def _on_ca_init_finished(self):
        """Clear the _CAInitThread reference only after Qt signals it is truly done."""
        self._ca_init_thread = None

    def _apply_ca_initial(self, exp_time, exp_period, connected: dict):
        """Apply CA initial values received from _CAInitThread (called in main thread)."""
        if exp_time is not None:
            _block_set(self._spin_exp,
                       lambda w=self._spin_exp, v=float(exp_time): w.setValue(v))
        if exp_period is not None:
            _block_set(self._spin_per,
                       lambda w=self._spin_per, v=float(exp_period): w.setValue(v))

        if not connected:
            return  # PVs unreachable — mode combo stays disabled

        # Pick winner: whichever has "Continuous" in its enum strings.
        # If neither has "Continuous" (e.g. Eiger), prefer TriggerMode.
        winner_key = None
        for key in ('image_mode', 'trigger_mode'):
            if key in connected:
                _, _, strs, _ = connected[key]
                if any('continuous' in s.lower() for s in strs):
                    winner_key = key
                    break
        if winner_key is None:
            winner_key = ('trigger_mode' if 'trigger_mode' in connected
                          else 'image_mode')

        rbv_key, lbl, enum_strs, current = connected[winner_key]
        self._active_mode_key = winner_key
        self._mode_lbl_widget.setText(lbl)
        self._cmb_mode.blockSignals(True)
        self._cmb_mode.clear()
        self._cmb_mode.addItems(enum_strs)
        self._cmb_mode.setEnabled(True)
        self._cmb_mode.blockSignals(False)
        if current:
            idx = self._cmb_mode.findText(current)
            if idx >= 0:
                _block_set(self._cmb_mode,
                           lambda i=idx: self._cmb_mode.setCurrentIndex(i))

    def _on_acquire(self):      _pv_put(self._ca_pvs.get('acquire'), 1)
    def _on_stop(self):         _pv_put(self._ca_pvs.get('acquire'), 0)
    def _on_mode_changed(self, idx): _pv_put(self._ca_pvs.get(self._active_mode_key), idx)
    def _on_exp_changed(self):  _pv_put(self._ca_pvs.get('acquire_time'), self._spin_exp.value())
    def _on_per_changed(self):  _pv_put(self._ca_pvs.get('acquire_period'), self._spin_per.value())

    # ── PVA image feed ───────────────────────────────────────────────────────────

    def _on_pva_reconnect(self):
        new_pv   = self._pv_edit.text().strip()
        new_host = self._host_edit.text().strip()
        if not new_pv:
            return
        if self._thread and self._thread.isRunning():
            self._thread.stop_monitor()
            self._thread = None
        self._pva_pv   = new_pv
        self._pva_host = new_host
        self._arr      = None
        self._frame_cnt = 0
        if _HAS_P4P:
            self._start_pva()

    def _start_pva(self):
        import os
        addr = (self._pva_host
                or os.environ.get('EPICS_PVA_ADDR_LIST', '').strip())
        host_info = f"  host: {addr}" if addr else "  host: (broadcast)"
        self._set_status(
            f"● Subscribing to {self._pva_pv}{host_info} …", "#888888")
        self._thread = _PVAMonitorThread(self._pva_pv, self._pva_host, self)
        self._thread.new_frame.connect(self._on_new_frame)
        self._thread.connection_changed.connect(self._on_pva_connected)
        self._thread.error_occurred.connect(self._on_pva_error)
        self._thread.start()
        # Warn if no frame arrives within 6 s (PVA not reachable / plugin not enabled)
        self._no_frame_timer = QTimer(self)
        self._no_frame_timer.setSingleShot(True)
        self._no_frame_timer.setInterval(6000)
        self._no_frame_timer.timeout.connect(self._on_no_frame_timeout)
        self._no_frame_timer.start()

    def _on_no_frame_timeout(self):
        if self._arr is None:
            self._set_status(
                f"⚠ No frames received from {self._pva_pv} — "
                "check that the Pva1 plugin is enabled and the detector is acquiring.",
                "#e8a44a",
            )

    def _on_new_frame(self, arr: np.ndarray, meta: dict):
        if hasattr(self, '_no_frame_timer'):
            self._no_frame_timer.stop()
        self._arr = arr
        self._frame_cnt += 1
        first_frame = self._frame_cnt == 1

        auto = self._chk_auto_levels.isChecked()
        self._img_view.setImage(self._prepare(arr), autoRange=False,
                                autoLevels=auto, autoHistogramRange=auto)
        if auto:
            QTimer.singleShot(80, self._sync_level_edits)
        if self._roi_on:
            self._update_roi_stats()

        uid  = meta.get('unique_id', self._frame_cnt)
        h, w = arr.shape[:2]
        raw_min, raw_max = arr.min(), arr.max()
        self._set_status(
            f"● Connected  frame #{uid}  |  {w}×{h}  {meta.get('dtype', '')}  "
            f"raw:[{raw_min}, {raw_max}]", "#2ca02c")
        fps = meta.get('fps', 0.0)
        self._fps_lbl.setText(f"{fps:.1f} fps" if fps > 0 else "—")

    def _on_pva_connected(self, ok: bool):
        if not ok:
            self._set_status("○ PVA disconnected", "#888888")
            self._fps_lbl.setText("—")

    def _on_pva_error(self, msg: str):
        self._set_status(f"⚠ {msg[:120]}", "#e05050")

    # ── Display helpers ──────────────────────────────────────────────────────────

    def _prepare(self, arr: np.ndarray) -> np.ndarray:
        """Apply sentinel handling → bad-pixel mask → log scale → transpose; returns float32."""
        # Dectris gap (-1) / dead (-2) sentinels are stored as large uint values
        # (PVA uint32: 0xFFFFFFFF=-1, 0xFFFFFFFE=-2). Reinterpret bits as signed so
        # they stay at -1/-2 in the float display — making gap lines visible in the image.
        if arr.dtype.kind == 'u' and arr.dtype.itemsize >= 4:
            # Dectris sentinel convention (uint32/uint64 only): 0xFFFFFFFF=-1 (gap), 0xFFFFFFFE=-2 (dead).
            # Reinterpret bits as signed so gap lines stay at -1 in the float display.
            signed_dtype = np.dtype(f"int{arr.dtype.itemsize * 8}")
            out = np.ascontiguousarray(arr).view(signed_dtype).astype(np.float32)
        else:
            # uint8 / uint16 (Mono8, Mono16, RGB cameras): plain unsigned, no sentinels.
            out = arr.astype(np.float32)
        if self._mask_enabled and self._mask_threshold is not None:
            out[out > self._mask_threshold] = 0.0
        if self._log_scale:
            np.clip(out, 0, None, out=out)   # log undefined for negatives; clips sentinels to 0
            out = np.log1p(out)
        return out.T if self._transpose else out

    def _on_mask_toggled(self, checked: bool):
        self._on_mask_threshold_changed()   # parse the field now in case Enter was never pressed
        self._mask_enabled = checked
        self._refresh_display()

    def _on_mask_threshold_changed(self):
        text = self._edit_mask_thresh.text().strip()
        try:
            self._mask_threshold = float(text) if text else None
        except ValueError:
            return
        if self._mask_enabled:
            self._refresh_display()

    def _refresh_display(self):
        if self._arr is not None:
            auto = self._chk_auto_levels.isChecked()
            self._img_view.setImage(self._prepare(self._arr),
                                    autoRange=False, autoLevels=auto,
                                    autoHistogramRange=auto)
            if auto:
                QTimer.singleShot(80, self._sync_level_edits)
            if self._roi_on:
                self._update_roi_stats()

    def _on_log_toggled(self, checked: bool):
        self._log_scale = checked
        self._refresh_display()

    def _on_transpose_toggled(self, checked: bool):
        self._transpose = checked
        self._refresh_display()

    def _apply_colormap(self, name: str):
        if name.lower() in ('gray', 'grey'):
            # Build grayscale directly — pg.colormap.get('gray') needs matplotlib
            cmap = pg.ColorMap(
                pos=np.array([0.0, 1.0]),
                color=np.array([[0, 0, 0, 255], [255, 255, 255, 255]], dtype=np.ubyte),
            )
        else:
            try:
                cmap = pg.colormap.get(name)
            except Exception:
                return
        if cmap is not None:
            self._img_view.setColorMap(cmap)

    # ── Lazy ROI1/Stats1 PV management ──────────────────────────────────────────

    _ROI_PV_KEYS = [
        'roi1_minx', 'roi1_minx_rbv', 'roi1_miny', 'roi1_miny_rbv',
        'roi1_sizex', 'roi1_sizex_rbv', 'roi1_sizey', 'roi1_sizey_rbv',
        'roi1_enable',
        'stats1_total', 'stats1_net', 'stats1_mean',
        'stats1_sigma', 'stats1_max', 'stats1_min', 'stats1_enable',
    ]

    def _ensure_roi_pvs(self):
        """Create ROI1/Stats1 epics.PV objects on first ROI enable (lazy init)."""
        if 'roi1_minx' in self._ca_pvs:
            return
        try:
            import epics
        except ImportError:
            return
        roi_pfx   = f"{self._prefix}ROI1:"
        stats_pfx = f"{self._prefix}Stats1:"
        for key, pvname in {
            'roi1_minx':      f"{roi_pfx}MinX",
            'roi1_minx_rbv':  f"{roi_pfx}MinX_RBV",
            'roi1_miny':      f"{roi_pfx}MinY",
            'roi1_miny_rbv':  f"{roi_pfx}MinY_RBV",
            'roi1_sizex':     f"{roi_pfx}SizeX",
            'roi1_sizex_rbv': f"{roi_pfx}SizeX_RBV",
            'roi1_sizey':     f"{roi_pfx}SizeY",
            'roi1_sizey_rbv': f"{roi_pfx}SizeY_RBV",
            'roi1_enable':    f"{roi_pfx}EnableCallbacks",
            'stats1_total':   f"{stats_pfx}Total_RBV",
            'stats1_net':     f"{stats_pfx}Net_RBV",
            'stats1_mean':    f"{stats_pfx}MeanValue_RBV",
            'stats1_sigma':   f"{stats_pfx}Sigma_RBV",
            'stats1_max':     f"{stats_pfx}MaxValue_RBV",
            'stats1_min':     f"{stats_pfx}MinValue_RBV",
            'stats1_enable':  f"{stats_pfx}EnableCallbacks",
        }.items():
            self._ca_pvs[key] = epics.PV(pvname)

    def _release_roi_pvs(self):
        """Disconnect and remove ROI1/Stats1 PVs to stop CA search broadcasts."""
        for key in self._ROI_PV_KEYS:
            pv = self._ca_pvs.pop(key, None)
            if pv:
                try:
                    pv.clear_callbacks()
                    pv.disconnect()
                except Exception:
                    pass

    def _on_roi_toggled(self, checked: bool):
        self._roi_on = checked
        self._roi.setVisible(checked)
        if checked:
            # Create ROI1/Stats1 PV objects now (lazy — not at window open time)
            self._ensure_roi_pvs()
            # Enable AD ROI1 and Stats1 plugins
            _pv_put(self._ca_pvs.get('roi1_enable'),  1)
            _pv_put(self._ca_pvs.get('stats1_enable'), 1)
            # Initialize overlay from cached ROI1 RBV values (non-blocking).
            # pv.value holds the last monitor value without making a new CA request.
            # Falls back to a centered region when PVs are not yet connected.
            minx  = getattr(self._ca_pvs.get('roi1_minx_rbv'),  'value', None)
            miny  = getattr(self._ca_pvs.get('roi1_miny_rbv'),  'value', None)
            sizex = getattr(self._ca_pvs.get('roi1_sizex_rbv'), 'value', None)
            sizey = getattr(self._ca_pvs.get('roi1_sizey_rbv'), 'value', None)
            if (None not in (minx, miny, sizex, sizey)
                    and int(sizex) > 0 and int(sizey) > 0):
                pos, sz = self._ad_to_overlay_coords(
                    int(minx), int(miny), int(sizex), int(sizey))
            elif self._arr is not None:
                disp = self._prepare(self._arr)
                h, w = disp.shape[:2]
                pos, sz = [w // 4, h // 4], [w // 2, h // 2]
            else:
                pos, sz = None, None
            if pos is not None:
                self._roi_updating = True
                self._roi.setPos(pos)
                self._roi.setSize(sz)
                self._roi_updating = False
            for sb in (self._spin_roi_minx, self._spin_roi_miny,
                       self._spin_roi_sizex, self._spin_roi_sizey):
                sb.setEnabled(True)
            self._update_roi_spinboxes()
            # Subscribe to ROI1 RBV changes for future IOC-side updates
            self._roi1_rbv_cache.clear()
            for key_full, key_short in [
                ('roi1_minx_rbv',  'minx'),
                ('roi1_miny_rbv',  'miny'),
                ('roi1_sizex_rbv', 'sizex'),
                ('roi1_sizey_rbv', 'sizey'),
            ]:
                pv = self._ca_pvs.get(key_full)
                if pv:
                    def _make_rbv_cb(k):
                        def _cb(value, **kw):
                            self._on_roi1_rbv_ca(k, value)
                        return _cb
                    pv.add_callback(_make_rbv_cb(key_short))
            # Subscribe to Stats1 live values
            for key in ('stats1_total', 'stats1_net', 'stats1_mean',
                        'stats1_sigma', 'stats1_max', 'stats1_min'):
                pv = self._ca_pvs.get(key)
                if pv:
                    def _make_cb(k):
                        def _cb(value, **kw):
                            self._on_stats1_ca(k, value)
                        return _cb
                    pv.add_callback(_make_cb(key))
            if self._arr is not None:
                self._update_roi_stats()
        else:
            # Disconnect and delete ROI1/Stats1 PVs — stops CA search broadcasts
            self._release_roi_pvs()
            self._stats1_vals.clear()
            self._roi1_rbv_cache.clear()
            self._roi_lbl.setText("")
            self._roi_debounce_timer.stop()
            for sb in (self._spin_roi_minx, self._spin_roi_miny,
                       self._spin_roi_sizex, self._spin_roi_sizey):
                sb.setEnabled(False)

    def _overlay_to_ad_coords(self):
        """Map current overlay position to AD pixel coords (minx, miny, sizex, sizey)."""
        pos = self._roi.pos()
        sz  = self._roi.size()
        rx  = int(round(pos.x()))
        ry  = int(round(pos.y()))
        sx  = max(1, int(round(sz.x())))
        sy  = max(1, int(round(sz.y())))
        if self._transpose:
            # display = arr.T: pg-x = AD col (MinX), pg-y = AD row (MinY)
            return rx, ry, sx, sy
        else:
            # display = arr: pg-x = AD row (MinY), pg-y = AD col (MinX)
            return ry, rx, sy, sx

    def _ad_to_overlay_coords(self, minx: int, miny: int, sizex: int, sizey: int):
        """Map AD ROI coords to overlay (pos, size) in pyqtgraph item space."""
        if self._transpose:
            return (minx, miny), (sizex, sizey)
        else:
            return (miny, minx), (sizey, sizex)

    def _on_roi_region_changed(self):
        """Called whenever the overlay is dragged/resized."""
        if self._roi_on:
            self._update_roi_stats()
            self._update_roi_spinboxes()
        if not self._roi_updating:
            self._roi_debounce_timer.start()   # restarts on every event; fires 150 ms after last

    def _write_roi_to_ad(self):
        """Write current overlay position to the AD ROI1 plugin (debounced)."""
        if not self._roi_on:
            return
        minx, miny, sizex, sizey = self._overlay_to_ad_coords()
        _pv_put(self._ca_pvs.get('roi1_minx'),  minx)
        _pv_put(self._ca_pvs.get('roi1_miny'),  miny)
        _pv_put(self._ca_pvs.get('roi1_sizex'), sizex)
        _pv_put(self._ca_pvs.get('roi1_sizey'), sizey)

    def _on_roi1_rbv_ca(self, key: str, value):
        """CA callback for one ROI1 RBV PV — caches value, emits when all 4 ready."""
        try:
            self._roi1_rbv_cache[key] = int(value)
            c = self._roi1_rbv_cache
            if all(k in c for k in ('minx', 'miny', 'sizex', 'sizey')):
                self._sig_roi1_rbv.emit(c['minx'], c['miny'], c['sizex'], c['sizey'])
        except Exception:
            pass

    def _apply_roi1_from_ca(self, minx: int, miny: int, sizex: int, sizey: int):
        """Slot: update overlay and spinboxes from AD ROI1 RBV (main thread)."""
        if not self._roi_on:
            return
        pos, sz = self._ad_to_overlay_coords(minx, miny, sizex, sizey)
        self._roi_updating = True
        self._roi.setPos(pos)
        self._roi.setSize(sz)
        self._roi_updating = False
        for sb, val in [
            (self._spin_roi_minx,  minx),
            (self._spin_roi_miny,  miny),
            (self._spin_roi_sizex, sizex),
            (self._spin_roi_sizey, sizey),
        ]:
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)

    def _update_roi_spinboxes(self):
        """Sync spinboxes to the current overlay position (no signals emitted)."""
        if not self._roi_on:
            return
        minx, miny, sizex, sizey = self._overlay_to_ad_coords()
        for sb, val in [
            (self._spin_roi_minx,  minx),
            (self._spin_roi_miny,  miny),
            (self._spin_roi_sizex, sizex),
            (self._spin_roi_sizey, sizey),
        ]:
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)

    def _on_roi_spinbox_changed(self):
        """User edited a ROI spinbox — move the overlay and write to AD."""
        if self._roi_updating or not self._roi_on:
            return
        minx  = self._spin_roi_minx.value()
        miny  = self._spin_roi_miny.value()
        sizex = self._spin_roi_sizex.value()
        sizey = self._spin_roi_sizey.value()
        pos, sz = self._ad_to_overlay_coords(minx, miny, sizex, sizey)
        self._roi_updating = True
        self._roi.setPos(pos)
        self._roi.setSize(sz)
        self._roi_updating = False
        _pv_put(self._ca_pvs.get('roi1_minx'),  minx)
        _pv_put(self._ca_pvs.get('roi1_miny'),  miny)
        _pv_put(self._ca_pvs.get('roi1_sizex'), sizex)
        _pv_put(self._ca_pvs.get('roi1_sizey'), sizey)

    def _on_stats1_ca(self, key: str, value):
        """CA callback for one Stats1 RBV PV — marshals to main thread via signal."""
        try:
            self._stats1_vals[key] = float(value)
            self._sig_stats1.emit(dict(self._stats1_vals))
        except Exception:
            pass

    def _on_stats1_update(self, vals: dict):
        """Slot: update ROI label with live AD Stats1 values (main thread)."""
        if not self._roi_on:
            return
        order = [
            ('stats1_total', 'Total'),
            ('stats1_net',   'Net  '),
            ('stats1_mean',  'Mean '),
            ('stats1_sigma', 'Sigma'),
            ('stats1_max',   'Max  '),
            ('stats1_min',   'Min  '),
        ]
        lines = [f"{lbl}: {vals[k]:.4g}" for k, lbl in order if k in vals]
        if lines:
            lines.append("(AD Stats1)")
            self._roi_lbl.setText('\n'.join(lines))

    def _update_roi_stats(self):
        """Compute stats from the local display array (fallback when Stats1 not connected)."""
        if self._arr is None or not self._roi_on:
            return
        if self._stats1_vals:
            return   # AD Stats1 callbacks are providing live values; don't overwrite
        try:
            disp   = self._prepare(self._arr).astype(np.float64)
            region = self._roi.getArrayRegion(disp, self._img_view.getImageItem())
            if region is None or region.size == 0:
                return
            h_px, w_px = region.shape[:2]
            self._roi_lbl.setText(
                f"Mean: {region.mean():.4g}\n"
                f"Max:  {region.max():.4g}\n"
                f"Min:  {region.min():.4g}\n"
                f"Sum:  {region.sum():.4g}\n"
                f"Std:  {region.std():.4g}\n"
                f"Size: {w_px}×{h_px} px\n"
                f"(local)"
            )
        except Exception:
            pass

    # ── Manual levels ────────────────────────────────────────────────────────────

    def _on_levels_changed(self):
        try:
            lo_txt = self._edit_lev_min.text().strip()
            hi_txt = self._edit_lev_max.text().strip()
            lo = float(lo_txt) if lo_txt else None
            hi = float(hi_txt) if hi_txt else None
        except ValueError:
            return
        if lo is not None and hi is not None and lo < hi:
            self._chk_auto_levels.blockSignals(True)
            self._chk_auto_levels.setChecked(False)
            self._chk_auto_levels.blockSignals(False)
            self._img_view.setLevels(lo, hi)
            self._img_view.ui.histogram.setHistogramRange(lo, hi)

    def _on_auto_levels_toggled(self, checked: bool):
        if checked and self._arr is not None:
            self._img_view.autoLevels()
            QTimer.singleShot(80, self._sync_level_edits)

    def _sync_level_edits(self):
        try:
            lo, hi = self._img_view.getLevels()
            self._edit_lev_min.setText(f"{lo:.6g}")
            self._edit_lev_max.setText(f"{hi:.6g}")
        except Exception:
            pass

    # ── Crosshair ────────────────────────────────────────────────────────────────

    def _on_crosshair_toggled(self, checked: bool):
        self._crosshair_on = checked
        self._vline.setVisible(checked)
        self._hline.setVisible(checked)
        if not checked:
            self._crosshair_lbl.setText("")

    def _on_mouse_moved(self, evt):
        if not self._crosshair_on:
            return
        pos = evt[0]
        img_item = self._img_view.getImageItem()
        if not img_item.sceneBoundingRect().contains(pos):
            self._vline.setVisible(False)
            self._hline.setVisible(False)
            return
        self._vline.setVisible(True)
        self._hline.setVisible(True)
        pt = img_item.mapFromScene(pos)
        ix = int(pt.x())
        iy = int(pt.y())
        if self._arr is None:
            return
        disp = self._prepare(self._arr)
        if not (0 <= ix < disp.shape[0] and 0 <= iy < disp.shape[1]):
            self._crosshair_lbl.setText("")
            return
        self._vline.setPos(ix + 0.5)
        self._hline.setPos(iy + 0.5)
        val = disp[ix, iy]
        # Show integer if value is whole-number (counts), float otherwise (log scale)
        val_str = str(int(val)) if val == int(val) else f"{val:.4g}"
        self._crosshair_lbl.setText(f"x={ix}  y={iy}  I={val_str}")

    def _set_status(self, msg: str, color: str = "#888888"):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(f"color:{color};")

    # ── Persistent display settings ──────────────────────────────────────────────

    def _restore_display_settings(self):
        """Apply saved colormap / log-scale / transpose for this device."""
        saved = load_ad_settings().get(self._device_name, {})
        cmap = saved.get('colormap', 'viridis')
        idx = self._cmb_cmap.findText(cmap)
        if idx >= 0:
            self._cmb_cmap.blockSignals(True)
            self._cmb_cmap.setCurrentIndex(idx)
            self._cmb_cmap.blockSignals(False)
        self._apply_colormap(cmap)

        if saved.get('log_scale', False):
            self._chk_log.setChecked(True)

        if saved.get('transpose', False):
            self._chk_xps.setChecked(True)

        thresh = saved.get('mask_threshold', '')
        if thresh:
            self._edit_mask_thresh.setText(str(thresh))
            try:
                self._mask_threshold = float(thresh)
            except ValueError:
                pass
        if saved.get('mask_enabled', False):
            self._chk_mask.setChecked(True)   # triggers _on_mask_toggled

    def _save_display_settings(self):
        """Persist display settings for this device to disk."""
        settings = load_ad_settings()
        dev = settings.setdefault(self._device_name, {})
        dev['colormap']       = self._cmb_cmap.currentText()
        dev['log_scale']      = self._chk_log.isChecked()
        dev['transpose']      = self._chk_xps.isChecked()
        dev['mask_enabled']   = self._chk_mask.isChecked()
        dev['mask_threshold'] = self._edit_mask_thresh.text().strip()
        save_ad_settings(settings)

    # ── Lifecycle ────────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent):
        self._save_display_settings()
        # Stop CA init background thread.
        # With interruption checks between every pv.get(timeout=1s) call, the thread
        # will always stop within ~1 s of requestInterruption().  We wait up to 1.5 s
        # before giving up; no terminate() because that is unsafe on macOS pthreads.
        t = self._ca_init_thread
        self._ca_init_thread = None   # prevent _on_ca_init_finished from re-setting it
        if t is not None:
            t.requestInterruption()
            t.wait(1500)   # thread has at most one pv.get(1 s) left before checking
        # Stop pending timers before tearing down PVs
        self._roi_debounce_timer.stop()
        if hasattr(self, '_no_frame_timer'):
            self._no_frame_timer.stop()
        # Stop PVA monitor thread (non-blocking: terminates if ctx.close() hangs)
        if self._thread and self._thread.isRunning():
            self._thread.stop_monitor()
        for pv in self._ca_pvs.values():
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


# ── Small helpers ────────────────────────────────────────────────────────────────

def _btn(text: str, bg: str, fg: str) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(f"background:{bg}; color:{fg}; font-weight:bold;")
    return b


def _spinbox(lo: float, hi: float, decimals: int, suffix: str, val: float) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(decimals)
    sb.setSuffix(suffix)
    sb.setValue(val)
    sb.wheelEvent = lambda e: e.ignore()
    return sb


def _pv_get(pv, *, as_string: bool = False):
    if pv is None:
        return None
    try:
        return pv.get(as_string=as_string, timeout=1.0)
    except Exception:
        return None


def _pv_put(pv, value):
    if pv is None:
        return
    try:
        pv.put(value, wait=False)
    except Exception:
        pass


def _block_set(widget, fn):
    widget.blockSignals(True)
    try:
        fn()
    finally:
        widget.blockSignals(False)


# ── Public helpers used by DevicesPlansTab ───────────────────────────────────────

# (role, candidate cam1 PV suffixes to match in PV address values)
_CAM1_ROLE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ('acquire',        ('cam1:Acquire',)),
    ('image_mode',     ('cam1:ImageMode',)),
    ('trigger_mode',   ('cam1:TriggerMode',)),
    ('acquire_time',   ('cam1:AcquireTime',)),
    ('acquire_period', ('cam1:AcquirePeriod',)),
    ('num_images',     ('cam1:NumImages', 'cam1:NumExposures')),
]


def _resolve_cam1_pvs(pv_map: dict) -> dict:
    """Scan PV address values in *pv_map* for known cam1 suffixes.

    Returns {role: pvname} for each matched role, skipping _RBV readbacks.
    Works for any ophyd-wrapped AD detector regardless of signal naming
    conventions — detection is done on PV addresses, not signal name keys.
    """
    resolved: dict = {}
    for role, patterns in _CAM1_ROLE_PATTERNS:
        for pvname in pv_map.values():
            if not pvname or '_RBV' in pvname:
                continue
            for pat in patterns:
                if pat.lower() in pvname.lower():
                    resolved[role] = pvname
                    break
            if role in resolved:
                break
    return resolved


def extract_ad_prefix(pv_map_for_device: dict) -> str | None:
    """Given {sig_name: pvname} for one device, return the AD base prefix.

    Scans PV addresses (not signal names) for 'cam1:' and strips from there.
    Returns e.g. 'PS1:' or None if no cam1 PVs are found.
    """
    for pvname in pv_map_for_device.values():
        if not pvname:
            continue
        idx = pvname.lower().find('cam1:')
        if idx >= 0:
            return pvname[:idx]
    return None


def is_area_detector(pv_map_for_device: dict, classname: str = "") -> bool:
    """Return True if this device is likely an EPICS area detector."""
    if any('cam1:' in (pv or '').lower() for pv in pv_map_for_device.values()):
        return True
    return 'detector' in classname.lower()
