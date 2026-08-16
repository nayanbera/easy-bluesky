"""xrf_viewer.py — Live XRF spectrum viewer using PyMCA's McaAdvancedFit widget."""

import json
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

# ── Per-device persistent settings ───────────────────────────────────────────

_XRF_SETTINGS_PATH = Path.home() / ".easy_bluesky" / "xrf_viewer_settings.json"


def load_xrf_settings() -> dict:
    try:
        if _XRF_SETTINGS_PATH.exists():
            return json.loads(_XRF_SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def save_xrf_settings(settings: dict):
    try:
        _XRF_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _XRF_SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass


# ── PyMCA availability ────────────────────────────────────────────────────────

try:
    from PyMca5.PyMcaGui.physics.xrf.McaAdvancedFit import McaAdvancedFit as _McaAdvancedFit
    _HAS_PYMCA = True
except ImportError:
    _HAS_PYMCA = False

# ── XRF detector classification ───────────────────────────────────────────────

_XRF_CLASSES = frozenset({
    'EpicsMCA', 'EpicsMCARecord', 'EpicsArrayMap',
    'Xspress3', 'Xspress3Channel', 'Xspress3ROI',
    'Saturn', 'SaturnDXP', 'Mercury', 'MercuryDXP',
    'XMAP', 'DXP', 'XmapDXP',
})

_XRF_SIG_KEYWORDS  = ('spectrum', 'mca', 'dxp', 'xspress', 'xmap', 'xrf')
_XRF_PV_KEYWORDS   = ('spectrum', ':mca', 'dxp', 'xspress', 'xmap', ':xrf')


def is_xrf_detector(pv_map: dict, classname: str = "") -> bool:
    """Return True if this device looks like an XRF / MCA detector."""
    if classname in _XRF_CLASSES:
        return True
    for sig, pv in pv_map.items():
        sl = sig.lower()
        pl = (pv or '').lower()
        if any(k in sl for k in _XRF_SIG_KEYWORDS):
            return True
        if any(k in pl for k in _XRF_PV_KEYWORDS):
            return True
    return False


def extract_xrf_spectrum_pv(pv_map: dict, classname: str = "") -> str | None:
    """Best-guess at the spectrum array PV from a device's signal map.

    Priority: signal named exactly 'spectrum' or 'mca' → signals containing
    'arraydata' / 'spectrum' in PV address → first signal with 'mca' anywhere.
    """
    # Exact signal name matches first
    for target in ('spectrum', 'mca', 'mca1', 'mca_spectrum'):
        for sig, pv in pv_map.items():
            if sig.lower() == target and pv:
                return pv

    # PV address patterns (Xspress3, DXP, ...)
    for sig, pv in pv_map.items():
        if not pv:
            continue
        pl = pv.lower()
        if 'arraydata' in pl or 'array_data' in pl or 'spectrum' in pl:
            return pv

    # Loose match on signal name
    for sig, pv in pv_map.items():
        if pv and 'mca' in sig.lower():
            return pv

    return None


# ── XRF Viewer Window ─────────────────────────────────────────────────────────

class XRFViewerWindow(QMainWindow):
    """Floating live XRF spectrum viewer.  Embeds PyMCA's McaAdvancedFit widget."""

    _spectrum_received = pyqtSignal(object)   # np.ndarray — cross-thread delivery

    def __init__(
        self,
        device_name: str,
        spectrum_pv: str = "",
        pv_map: dict | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._device_name    = device_name
        self._spectrum_pv_name = spectrum_pv.strip()
        self._pv_map         = pv_map or {}
        self._spectrum_pv    = None   # epics.PV handle
        self._live           = True
        self._last_counts: np.ndarray | None = None
        self._connected      = False

        self._spectrum_received.connect(self._on_new_spectrum)

        self._build_ui()
        self._restore_settings()

        if self._spectrum_pv_name:
            QTimer.singleShot(200, lambda: self._connect_spectrum_pv(self._spectrum_pv_name))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle(f"XRF Viewer — {self._device_name}")
        self.resize(1200, 700)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        root.addWidget(self._build_ctrl())

        if _HAS_PYMCA:
            self._mca = _McaAdvancedFit(parent=central)
            root.addWidget(self._mca, stretch=1)
        else:
            msg = QLabel(
                "<b>PyMCA is not installed.</b><br><br>"
                "Install it with:<br>"
                "<tt>pip install pymca</tt><br><br>"
                "PyMCA ≥ 5.9 is required for PyQt6 compatibility."
            )
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setWordWrap(True)
            root.addWidget(msg, stretch=1)

        self._status_lbl = QLabel("○ Not connected")
        self.statusBar().addWidget(self._status_lbl, 1)

    def _build_ctrl(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(220)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(2, 4, 4, 4)
        lay.setSpacing(8)

        # ── PV connection ─────────────────────────────────────────────
        gp = QGroupBox("Spectrum PV")
        gpl = QVBoxLayout(gp)
        gpl.setSpacing(4)

        self._pv_edit = QLineEdit(self._spectrum_pv_name)
        self._pv_edit.setPlaceholderText("e.g. IOC:MCA:.VAL")
        self._pv_edit.returnPressed.connect(self._on_pv_connect)
        gpl.addWidget(self._pv_edit)

        btn_conn = QPushButton("Connect")
        btn_conn.clicked.connect(self._on_pv_connect)
        gpl.addWidget(btn_conn)
        lay.addWidget(gp)

        # ── Acquire ───────────────────────────────────────────────────
        ga = QGroupBox("Acquire")
        gal = QVBoxLayout(ga)
        gal.setSpacing(5)

        row = QHBoxLayout()
        self._btn_erase = _btn("⏮  Erase+Start", "#1a3a1a", "#6ddc6d")
        self._btn_start = _btn("▶  Start",        "#1a2a3a", "#6db0dc")
        self._btn_stop  = _btn("■  Stop",          "#3a1a1a", "#dc6d6d")
        self._btn_erase.clicked.connect(self._on_erase_start)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        row.addWidget(self._btn_erase)
        row.addWidget(self._btn_stop)
        gal.addLayout(row)
        gal.addWidget(self._btn_start)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Preset time:"))
        self._spin_preset = QDoubleSpinBox()
        self._spin_preset.setRange(0.0, 86400.0)
        self._spin_preset.setDecimals(2)
        self._spin_preset.setSuffix(" s")
        self._spin_preset.setValue(10.0)
        self._spin_preset.wheelEvent = lambda e: e.ignore()
        self._spin_preset.editingFinished.connect(self._on_preset_changed)
        r2.addWidget(self._spin_preset)
        gal.addLayout(r2)
        lay.addWidget(ga)

        # ── Live update ───────────────────────────────────────────────
        gd = QGroupBox("Display")
        gdl = QVBoxLayout(gd)
        self._chk_live = QCheckBox("Live update")
        self._chk_live.setChecked(True)
        self._chk_live.toggled.connect(self._on_live_toggled)
        gdl.addWidget(self._chk_live)

        btn_refresh = QPushButton("Read Now")
        btn_refresh.clicked.connect(self._on_read_now)
        gdl.addWidget(btn_refresh)
        lay.addWidget(gd)

        lay.addStretch()
        return panel

    # ── PV connection ─────────────────────────────────────────────────────────

    def _on_pv_connect(self):
        pv = self._pv_edit.text().strip()
        if pv:
            self._connect_spectrum_pv(pv)

    def _connect_spectrum_pv(self, pvname: str):
        try:
            import epics
        except ImportError:
            self._set_status("⚠ pyepics not installed — pip install pyepics", "#e05050")
            return

        # Disconnect existing
        if self._spectrum_pv is not None:
            try:
                self._spectrum_pv.clear_callbacks()
                self._spectrum_pv.disconnect()
            except Exception:
                pass
            self._spectrum_pv = None

        self._spectrum_pv_name = pvname
        self._pv_edit.setText(pvname)
        self._set_status(f"● Connecting to {pvname}…", "#888888")

        self._spectrum_pv = epics.PV(
            pvname,
            auto_monitor=True,
            callback=self._on_spectrum_callback,
            connection_callback=self._on_pv_connection_cb,
        )

    def _on_pv_connection_cb(self, pvname, conn, **kw):
        self._connected = conn
        if not conn:
            self._set_status(f"○ Disconnected from {pvname}", "#888888")

    def _on_spectrum_callback(self, pvname, value, **kw):
        if value is not None and self._live:
            self._spectrum_received.emit(np.asarray(value).copy())

    # ── Spectrum display ──────────────────────────────────────────────────────

    def _on_new_spectrum(self, counts: np.ndarray):
        if counts.ndim != 1 or counts.size == 0:
            return
        self._last_counts = counts
        n = len(counts)
        total = int(counts.sum())
        self._set_status(
            f"● {self._spectrum_pv_name}  |  {n} ch  |  {total:,} cts", "#2ca02c")
        if not _HAS_PYMCA:
            return
        try:
            channels = np.arange(n, dtype=np.float64)
            self._mca.setData(channels, counts.astype(np.float64),
                              legend=self._device_name)
        except Exception as exc:
            self._set_status(f"⚠ PyMCA display error: {exc}", "#e05050")

    def _on_live_toggled(self, checked: bool):
        self._live = checked

    def _on_read_now(self):
        if self._spectrum_pv is None:
            return
        try:
            val = self._spectrum_pv.get(timeout=2.0)
            if val is not None:
                self._spectrum_received.emit(np.asarray(val).copy())
        except Exception:
            pass

    # ── EPICS MCA acquire controls ────────────────────────────────────────────

    def _mca_base(self) -> str:
        """Strip field suffix from spectrum PV to get the MCA record base."""
        pv = self._spectrum_pv_name
        for sep in ('.VAL', '.', ':'):
            idx = pv.rfind(sep)
            if idx > 0:
                return pv[:idx]
        return pv

    def _ca_put(self, field: str, value):
        try:
            import epics
            epics.caput(f"{self._mca_base()}{field}", value, wait=False)
        except Exception:
            pass

    def _on_erase_start(self):  self._ca_put('.ERST', 1)
    def _on_start(self):        self._ca_put('.STRT', 1)
    def _on_stop(self):         self._ca_put('.STOP', 1)
    def _on_preset_changed(self): self._ca_put('.PRTM', self._spin_preset.value())

    # ── Persistent settings ───────────────────────────────────────────────────

    def _restore_settings(self):
        saved = load_xrf_settings().get(self._device_name, {})
        pv = saved.get('spectrum_pv', '')
        if pv and not self._spectrum_pv_name:
            self._spectrum_pv_name = pv
            self._pv_edit.setText(pv)
        if 'preset_time' in saved:
            self._spin_preset.setValue(float(saved['preset_time']))

    def _save_settings(self):
        settings = load_xrf_settings()
        settings.setdefault(self._device_name, {}).update({
            'spectrum_pv': self._spectrum_pv_name,
            'preset_time': self._spin_preset.value(),
        })
        save_xrf_settings(settings)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = "#888888"):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(f"color:{color};")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent):
        self._save_settings()
        if self._spectrum_pv is not None:
            try:
                self._spectrum_pv.clear_callbacks()
                self._spectrum_pv.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


# ── Small helpers ─────────────────────────────────────────────────────────────

def _btn(text: str, bg: str, fg: str) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(f"background:{bg}; color:{fg}; font-weight:bold;")
    return b
