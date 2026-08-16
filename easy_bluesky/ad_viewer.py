"""ad_viewer.py — Live area-detector image viewer via PVAccess + pyqtgraph."""

import threading
import time

import numpy as np
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QVBoxLayout, QWidget,
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

    def __init__(self, pva_pv: str, parent=None):
        super().__init__(parent)
        self._pva_pv    = pva_pv
        self._stop_evt  = threading.Event()
        self._last_emit = 0.0

    def run(self):
        import os
        from p4p.client.thread import Context

        # Build PVA address config mirroring the CA addr list that apply_epics_env()
        # already placed in the process environment.  Without this, p4p relies on
        # multicast discovery which doesn't cross subnets to the beamline host.
        conf = {}
        pva_addr = os.environ.get('EPICS_PVA_ADDR_LIST', '').strip()
        ca_addr  = os.environ.get('EPICS_CA_ADDR_LIST',  '').strip()
        addr = pva_addr or ca_addr
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
        self.wait(3000)

    def _on_value(self, value):
        if value is None or isinstance(value, Exception):
            self.connection_changed.emit(False)
            return
        now = time.monotonic()
        if now - self._last_emit < _MIN_INTERVAL:
            return
        self._last_emit = now
        arr = _extract_ndarray(value)
        if arr is None:
            return
        uid = 0
        try:
            uid = int(value['uniqueId'])
        except Exception:
            pass
        self.connection_changed.emit(True)
        self.new_frame.emit(arr, {'unique_id': uid, 'shape': arr.shape, 'dtype': str(arr.dtype)})


def _extract_ndarray(value) -> np.ndarray | None:
    """Reshape a p4p NTNDArray Value into a 2-D numpy array (height × width)."""
    try:
        data = value['value']
        dims = value['dimension']
        if data is None or len(dims) == 0:
            return None
        nx = int(dims[0]['size'])
        ny = int(dims[1]['size']) if len(dims) > 1 else 1
        if nx == 0 or ny == 0:
            return None
        # AD stores data row-major: reshape to (height, width) = C order
        return data.reshape(ny, nx).copy()
    except Exception:
        return None


# ── Main viewer window ───────────────────────────────────────────────────────────

class ADViewerWindow(QMainWindow):
    """Floating live-view window for one EPICS area detector via PVAccess."""

    def __init__(
        self,
        device_name: str,
        prefix: str,       # e.g. "Pil300K:" — must include trailing colon
        pv_map: dict,      # {sig_name: pvname} for this device (informational)
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
        self._fps       = 0.0
        self._t_last    = 0.0
        self._frame_cnt = 0

        self._ca_pvs: dict                     = {}
        self._thread: _PVAMonitorThread | None = None

        self._build_ui()
        self._apply_colormap("viridis")
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

        # Image view (histogram panel on right side, built-in)
        self._img_view = pg.ImageView()
        self._img_view.ui.roiBtn.setVisible(False)
        self._img_view.ui.menuBtn.setVisible(False)
        root.addWidget(self._img_view, stretch=1)

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
        self._roi.sigRegionChanged.connect(self._update_roi_stats)
        self._img_view.addItem(self._roi)
        self._roi.setVisible(False)

        self._status_lbl = QLabel("● Connecting…")
        self.statusBar().addWidget(self._status_lbl, 1)
        self._fps_lbl = QLabel("—")
        self.statusBar().addPermanentWidget(self._fps_lbl)

    def _build_ctrl(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(230)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(2, 4, 4, 4)
        lay.setSpacing(8)

        # ── PVA stream PV ─────────────────────────────────────────────────
        gp = QGroupBox("PVA Image PV")
        gpl = QVBoxLayout(gp)
        gpl.setSpacing(4)
        self._pv_edit = QLineEdit(self._pva_pv)
        self._pv_edit.setPlaceholderText("e.g. 15PS1:Pva1:Image")
        self._pv_edit.setToolTip("PVAccess PV for the image stream (press Enter or click Connect)")
        gpl.addWidget(self._pv_edit)
        btn_connect = QPushButton("Connect")
        btn_connect.clicked.connect(self._on_pva_reconnect)
        self._pv_edit.returnPressed.connect(self._on_pva_reconnect)
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

        gl.addWidget(QLabel("Image Mode:"))
        self._cmb_mode = QComboBox()
        self._cmb_mode.addItems(["Single", "Multiple", "Continuous"])
        self._cmb_mode.setCurrentText("Continuous")
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

        self._chk_log = QCheckBox("Log scale  (log₁₊ₓ)")
        self._chk_xps = QCheckBox("Transpose image")
        self._chk_log.toggled.connect(self._on_log_toggled)
        self._chk_xps.toggled.connect(self._on_transpose_toggled)
        g2l.addWidget(self._chk_log)
        g2l.addWidget(self._chk_xps)

        crow = QHBoxLayout()
        crow.addWidget(QLabel("Colormap:"))
        self._cmb_cmap = QComboBox()
        self._cmb_cmap.addItems(_COLORMAPS)
        self._cmb_cmap.currentTextChanged.connect(self._apply_colormap)
        crow.addWidget(self._cmb_cmap)
        g2l.addLayout(crow)

        btn_auto = QPushButton("Auto Levels")
        btn_auto.clicked.connect(lambda: self._img_view.autoLevels())
        g2l.addWidget(btn_auto)
        lay.addWidget(g2)

        # ── ROI ──────────────────────────────────────────────────────────
        g3 = QGroupBox("ROI")
        g3l = QVBoxLayout(g3)
        g3l.setSpacing(4)

        self._chk_roi = QCheckBox("Enable ROI")
        self._chk_roi.toggled.connect(self._on_roi_toggled)
        g3l.addWidget(self._chk_roi)

        self._roi_lbl = QLabel("")
        self._roi_lbl.setWordWrap(True)
        self._roi_lbl.setStyleSheet(
            "font-family:monospace; font-size:10px; color:#cccccc;")
        g3l.addWidget(self._roi_lbl)
        lay.addWidget(g3)

        lay.addStretch()
        return panel

    # ── CA controls ──────────────────────────────────────────────────────────────

    def _connect_ca_pvs(self):
        try:
            import epics
        except ImportError:
            return
        p = self._cam_pfx
        for key, pvname in {
            'acquire':            f"{p}Acquire",
            'acquire_rbv':        f"{p}Acquire_RBV",
            'acquire_time':       f"{p}AcquireTime",
            'acquire_time_rbv':   f"{p}AcquireTime_RBV",
            'acquire_period':     f"{p}AcquirePeriod",
            'acquire_period_rbv': f"{p}AcquirePeriod_RBV",
            'image_mode':         f"{p}ImageMode",
            'image_mode_rbv':     f"{p}ImageMode_RBV",
        }.items():
            self._ca_pvs[key] = epics.PV(pvname)
        QTimer.singleShot(700, self._read_ca_initial)

    def _read_ca_initial(self):
        c = self._ca_pvs
        for pv_key, widget in [
            ('acquire_time_rbv',   self._spin_exp),
            ('acquire_period_rbv', self._spin_per),
        ]:
            val = _pv_get(c.get(pv_key))
            if val is not None:
                _block_set(widget, lambda w=widget, v=float(val): w.setValue(v))

        mode = _pv_get(c.get('image_mode_rbv'), as_string=True)
        if mode:
            idx = self._cmb_mode.findText(mode)
            if idx >= 0:
                _block_set(self._cmb_mode,
                           lambda i=idx: self._cmb_mode.setCurrentIndex(i))

    def _on_acquire(self):      _pv_put(self._ca_pvs.get('acquire'), 1)
    def _on_stop(self):         _pv_put(self._ca_pvs.get('acquire'), 0)
    def _on_mode_changed(self, idx): _pv_put(self._ca_pvs.get('image_mode'), idx)
    def _on_exp_changed(self):  _pv_put(self._ca_pvs.get('acquire_time'), self._spin_exp.value())
    def _on_per_changed(self):  _pv_put(self._ca_pvs.get('acquire_period'), self._spin_per.value())

    # ── PVA image feed ───────────────────────────────────────────────────────────

    def _on_pva_reconnect(self):
        new_pv = self._pv_edit.text().strip()
        if not new_pv or new_pv == self._pva_pv and (
            self._thread and self._thread.isRunning()
        ):
            return
        if self._thread and self._thread.isRunning():
            self._thread.stop_monitor()
            self._thread = None
        self._pva_pv  = new_pv
        self._arr     = None
        self._fps     = 0.0
        self._t_last  = 0.0
        self._frame_cnt = 0
        if _HAS_P4P:
            self._start_pva()

    def _start_pva(self):
        self._set_status(f"● Subscribing to {self._pva_pv} …", "#888888")
        self._thread = _PVAMonitorThread(self._pva_pv, self)
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
        now = time.monotonic()
        if self._t_last > 0 and (dt := now - self._t_last) > 0:
            self._fps = 0.8 * self._fps + 0.2 / dt
        self._t_last = now

        self._img_view.setImage(self._prepare(arr), autoRange=False, autoLevels=False)
        if self._roi_on:
            self._update_roi_stats()

        uid  = meta.get('unique_id', self._frame_cnt)
        h, w = arr.shape[:2]
        self._set_status(
            f"● Connected  frame #{uid}  |  {w}×{h}  {meta.get('dtype', '')}", "#2ca02c")
        self._fps_lbl.setText(f"{self._fps:.1f} fps")

    def _on_pva_connected(self, ok: bool):
        if not ok:
            self._set_status("○ PVA disconnected", "#888888")
            self._fps_lbl.setText("—")

    def _on_pva_error(self, msg: str):
        self._set_status(f"⚠ {msg[:120]}", "#e05050")

    # ── Display helpers ──────────────────────────────────────────────────────────

    def _prepare(self, arr: np.ndarray) -> np.ndarray:
        """Apply log / transpose for display; always returns float32."""
        out = np.log1p(arr.astype(np.float32)) if self._log_scale \
              else arr.astype(np.float32)
        return out.T if self._transpose else out

    def _refresh_display(self):
        if self._arr is not None:
            self._img_view.setImage(self._prepare(self._arr),
                                    autoRange=False, autoLevels=False)
            if self._roi_on:
                self._update_roi_stats()

    def _on_log_toggled(self, checked: bool):
        self._log_scale = checked
        self._refresh_display()

    def _on_transpose_toggled(self, checked: bool):
        self._transpose = checked
        self._refresh_display()

    def _apply_colormap(self, name: str):
        try:
            cmap = pg.colormap.get(name)
            if cmap is not None:
                self._img_view.setColorMap(cmap)
        except Exception:
            pass

    def _on_roi_toggled(self, checked: bool):
        self._roi_on = checked
        self._roi.setVisible(checked)
        if checked and self._arr is not None:
            disp = self._prepare(self._arr)
            h, w = disp.shape[:2]
            self._roi.setPos([w // 4, h // 4])
            self._roi.setSize([w // 2, h // 2])
            self._update_roi_stats()
        else:
            self._roi_lbl.setText("")

    def _update_roi_stats(self):
        if self._arr is None or not self._roi_on:
            return
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
                f"Size: {w_px}×{h_px} px"
            )
        except Exception:
            pass

    def _set_status(self, msg: str, color: str = "#888888"):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(f"color:{color};")

    # ── Lifecycle ────────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent):
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
