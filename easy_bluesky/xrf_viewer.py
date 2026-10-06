"""xrf_viewer.py — Live XRF spectrum viewer using PyMCA's McaAdvancedFit widget."""

import json
from pathlib import Path

import numpy as np
from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtGui import QCloseEvent
from qtpy.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

# ── Per-device persistent settings ───────────────────────────────────────────

_XRF_SETTINGS_PATH = Path.home() / ".easy_bluesky" / "xrf_viewer_settings.json"

# Mn K-line energies (eV) produced by Fe-55 electron-capture decay
_FE55_KA_EV = 5895.0   # Mn Kα (weighted centroid of Kα1 5898.8 + Kα2 5887.6)
_FE55_KB_EV = 6490.4   # Mn Kβ1


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

# ── scipy availability (for Gaussian fitting) ─────────────────────────────────

try:
    from scipy.optimize import curve_fit as _curve_fit
    from scipy.signal import find_peaks as _find_peaks
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

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

    _spectrum_received = Signal(object)   # np.ndarray — cross-thread delivery

    def __init__(
        self,
        device_name: str,
        spectrum_pv: str = "",
        pv_map: dict | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._device_name      = device_name
        self._spectrum_pv_name = spectrum_pv.strip()
        self._pv_map           = pv_map or {}
        self._spectrum_pv      = None   # epics.PV handle
        self._live             = True
        self._last_counts: np.ndarray | None = None
        self._connected        = False

        # Energy calibration: E(ch) = _cal_offset + _cal_gain * ch  (eV)
        # 0 gain = uncalibrated (display raw channels)
        self._cal_gain: float   = 0.0
        self._cal_offset: float = 0.0

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
                "PyMCA ≥ 5.9 is required for Qt6 compatibility."
            )
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setWordWrap(True)
            root.addWidget(msg, stretch=1)

        self._status_lbl = QLabel("○ Not connected")
        self.statusBar().addWidget(self._status_lbl, 1)

    def _build_ctrl(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(230)
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

        # ── Fe-55 energy calibration ──────────────────────────────────
        lay.addWidget(self._build_calibration_panel())

        lay.addStretch()
        return panel

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
        btn_fit.setToolTip(
            "Fits Gaussians to Mn Kα and Kβ peaks in the current spectrum.\n"
            "Requires scipy."
        )
        btn_fit.clicked.connect(self._on_autofit_fe55)
        gcl.addWidget(btn_fit)

        # Result display
        self._cal_result_lbl = QLabel("Gain:   —\nOffset: —")
        self._cal_result_lbl.setStyleSheet("color:#aaa; font-size:11px;")
        gcl.addWidget(self._cal_result_lbl)

        row = QHBoxLayout()
        self._btn_apply_cal = QPushButton("Apply")
        self._btn_apply_cal.setEnabled(False)
        self._btn_apply_cal.clicked.connect(self._on_apply_calibration)
        btn_clear_cal = QPushButton("Clear")
        btn_clear_cal.clicked.connect(self._on_clear_calibration)
        row.addWidget(self._btn_apply_cal)
        row.addWidget(btn_clear_cal)
        gcl.addLayout(row)

        return gc

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
        cal_tag = "  cal" if self._cal_gain > 0 else ""
        self._set_status(
            f"● {self._spectrum_pv_name}  |  {n} ch  |  {total:,} cts{cal_tag}",
            "#2ca02c",
        )
        if not _HAS_PYMCA:
            return
        try:
            channels = np.arange(n, dtype=np.float64)
            if self._cal_gain > 0:
                # Convert to keV for PyMCA (PyMCA uses keV internally)
                x = (self._cal_offset + self._cal_gain * channels) / 1000.0
            else:
                x = channels
            self._mca.setData(x, counts.astype(np.float64), legend=self._device_name)
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

    # ── Fe-55 energy calibration ──────────────────────────────────────────────

    def _fit_gaussian_centroid(self, counts: np.ndarray, center: int,
                               window: int = 25) -> float:
        """Fit a Gaussian + background around `center` and return the centroid."""
        n = len(counts)
        lo = max(0, center - window)
        hi = min(n, center + window + 1)
        x = np.arange(lo, hi, dtype=np.float64)
        y = counts[lo:hi].astype(np.float64)
        if y.max() == 0 or not _HAS_SCIPY:
            return float(center)
        try:
            def gauss(x, amp, mu, sigma, bg):
                return amp * np.exp(-0.5 * ((x - mu) / max(sigma, 0.5)) ** 2) + bg

            p0 = [float(y.max() - y.min()), float(center), 5.0, float(y.min())]
            bounds = ([0, lo, 0.5, 0], [np.inf, hi, window, np.inf])
            popt, _ = _curve_fit(gauss, x, y, p0=p0, bounds=bounds, maxfev=3000)
            return float(popt[1])
        except Exception:
            # Fall back to weighted centroid
            if y.sum() > 0:
                return float((x * y).sum() / y.sum())
            return float(center)

    def _on_autofit_fe55(self):
        if self._last_counts is None:
            self._set_status("⚠ No spectrum yet — connect and acquire first", "#e05050")
            return
        counts = self._last_counts.astype(np.float64)
        n = len(counts)
        if n < 10:
            return

        if not _HAS_SCIPY:
            self._set_status("⚠ scipy not installed — enter channel positions manually", "#e08050")
            return

        # Find dominant peak → Mn Kα
        peaks, props = _find_peaks(counts, prominence=counts.max() * 0.05, width=2)
        if len(peaks) == 0:
            self._set_status("⚠ No peaks found in spectrum", "#e05050")
            return

        ka_peak = int(peaks[np.argmax(counts[peaks])])
        ka_ch = self._fit_gaussian_centroid(counts, ka_peak)

        # Mn Kβ expected at ka_ch * (Kβ_eV / Kα_eV), ±10 % search window
        ratio   = _FE55_KB_EV / _FE55_KA_EV          # ≈ 1.1010
        kb_est  = int(round(ka_ch * ratio))
        kb_win  = max(15, int(ka_ch * 0.05))
        lo_kb   = max(0, kb_est - kb_win)
        hi_kb   = min(n, kb_est + kb_win + 1)

        # Look for a secondary peak in the Kβ window
        sub = counts[lo_kb:hi_kb]
        if sub.max() > 0:
            kb_local = int(np.argmax(sub))
            kb_peak  = lo_kb + kb_local
            kb_ch    = self._fit_gaussian_centroid(counts, kb_peak, window=15)
        else:
            # No clear Kβ: use ratio estimate (single-point fallback)
            kb_ch = ka_ch * ratio

        self._spin_ka.blockSignals(True)
        self._spin_kb.blockSignals(True)
        self._spin_ka.setValue(round(ka_ch, 1))
        self._spin_kb.setValue(round(kb_ch, 1))
        self._spin_ka.blockSignals(False)
        self._spin_kb.blockSignals(False)

        self._update_calibration_result()

    def _on_cal_channels_changed(self):
        self._update_calibration_result()

    def _update_calibration_result(self):
        ka_ch = self._spin_ka.value()
        kb_ch = self._spin_kb.value()
        if ka_ch <= 0 or kb_ch <= 0 or abs(kb_ch - ka_ch) < 1:
            self._cal_result_lbl.setText("Gain:   —\nOffset: —")
            self._btn_apply_cal.setEnabled(False)
            return

        gain   = (_FE55_KB_EV - _FE55_KA_EV) / (kb_ch - ka_ch)
        offset = _FE55_KA_EV - gain * ka_ch
        self._cal_result_lbl.setText(
            f"Gain:   {gain:.4f} eV/ch\nOffset: {offset:.2f} eV"
        )
        self._btn_apply_cal.setEnabled(True)

    def _on_apply_calibration(self):
        ka_ch = self._spin_ka.value()
        kb_ch = self._spin_kb.value()
        if ka_ch <= 0 or kb_ch <= 0 or abs(kb_ch - ka_ch) < 1:
            return

        gain   = (_FE55_KB_EV - _FE55_KA_EV) / (kb_ch - ka_ch)
        offset = _FE55_KA_EV - gain * ka_ch

        self._cal_gain   = gain
        self._cal_offset = offset

        # Re-display current spectrum with new calibration
        if self._last_counts is not None:
            self._on_new_spectrum(self._last_counts)

        self._set_status(
            f"Calibrated: gain={gain:.4f} eV/ch  offset={offset:.2f} eV", "#2ca02c"
        )
        self._save_settings()

    def _on_clear_calibration(self):
        self._cal_gain   = 0.0
        self._cal_offset = 0.0
        self._spin_ka.setValue(0.0)
        self._spin_kb.setValue(0.0)
        self._cal_result_lbl.setText("Gain:   —\nOffset: —")
        self._btn_apply_cal.setEnabled(False)
        if self._last_counts is not None:
            self._on_new_spectrum(self._last_counts)
        self._save_settings()

    # ── Persistent settings ───────────────────────────────────────────────────

    def _restore_settings(self):
        saved = load_xrf_settings().get(self._device_name, {})
        pv = saved.get('spectrum_pv', '')
        if pv and not self._spectrum_pv_name:
            self._spectrum_pv_name = pv
            self._pv_edit.setText(pv)
        if 'preset_time' in saved:
            self._spin_preset.setValue(float(saved['preset_time']))
        ka_ch = float(saved.get('ka_channel', 0.0))
        kb_ch = float(saved.get('kb_channel', 0.0))
        if ka_ch > 0:
            self._spin_ka.setValue(ka_ch)
        if kb_ch > 0:
            self._spin_kb.setValue(kb_ch)
        # Restore calibration if both channels were saved
        if ka_ch > 0 and kb_ch > 0 and abs(kb_ch - ka_ch) >= 1:
            self._cal_gain   = float(saved.get('cal_gain', 0.0))
            self._cal_offset = float(saved.get('cal_offset', 0.0))
            self._update_calibration_result()

    def _save_settings(self):
        settings = load_xrf_settings()
        settings.setdefault(self._device_name, {}).update({
            'spectrum_pv':  self._spectrum_pv_name,
            'preset_time':  self._spin_preset.value(),
            'ka_channel':   self._spin_ka.value(),
            'kb_channel':   self._spin_kb.value(),
            'cal_gain':     self._cal_gain,
            'cal_offset':   self._cal_offset,
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
