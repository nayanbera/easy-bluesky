"""devices_plans_tab.py — Devices & Plans browser tab."""

from qtpy.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QTreeWidget, QTreeWidgetItem,
    QPlainTextEdit, QPushButton, QDoubleSpinBox, QLineEdit, QComboBox, QMenu, QToolButton,
    QMessageBox, QDialog, QFormLayout, QDialogButtonBox, QCheckBox,
)
from .widgets import NoScrollDoubleSpinBox
import json
from pathlib import Path

import threading

from qtpy.QtCore import Qt, Signal, QObject, QThread, QTimer
from qtpy.QtGui import QBrush, QColor, QFont

from .config import ACCENT
from .plans_manager import (
    PlanCatalog, PLAN_COLORS, PLAN_TYPE_LABELS,
    plan_type_from_module,
)
from .plan_builder import PlanFileTreePanel

_METADATA_PATH = Path.home() / ".easy_bluesky" / "device_metadata.json"


class _ADConfigDialog(QDialog):
    """Two-field dialog: AD EPICS prefix + beamline host for PVA routing."""

    def __init__(self, device_name: str, default_prefix: str,
                 default_host: str, default_sample_view: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Configure AD Viewer — {device_name}")
        self.setMinimumWidth(400)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        note = QLabel(
            f"<b>{device_name}</b> — enter the EPICS prefix and beamline host.\n"
            "Settings are saved to <tt>~/.easy_bluesky/ad_viewer_settings.json</tt>."
        )
        note.setWordWrap(True)
        lay.addWidget(note)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        self._prefix_edit = QLineEdit(default_prefix)
        self._prefix_edit.setPlaceholderText("e.g. 15PS1:")
        self._prefix_edit.setToolTip(
            "EPICS base prefix for this detector (must end with ':')")
        form.addRow("AD prefix:", self._prefix_edit)

        self._host_edit = QLineEdit(default_host)
        self._host_edit.setPlaceholderText("e.g. 164.54.169.50")
        self._host_edit.setToolTip(
            "Detector host IP/hostname for PVAccess unicast routing")
        form.addRow("Detector host:", self._host_edit)

        lay.addLayout(form)

        self._sample_view_cb = QCheckBox("Sample View Mode")
        self._sample_view_cb.setChecked(default_sample_view)
        self._sample_view_cb.setToolTip(
            "Open the ASWAXS Sample View station instead of the standard AD image viewer.\n"
            "Requires: pip install git+https://github.com/JIAJTIAN/ASWAXS_Sample_View.git"
        )
        lay.addWidget(self._sample_view_cb)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Open Viewer")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    @property
    def prefix(self) -> str:
        p = self._prefix_edit.text().strip()
        return (p if p.endswith(':') else p + ':') if p else ''

    @property
    def host(self) -> str:
        return self._host_edit.text().strip()

    @property
    def sample_view_mode(self) -> bool:
        return self._sample_view_cb.isChecked()


class _XRFConfigDialog(QDialog):
    """Single-field dialog: spectrum array PV for the XRF Viewer."""

    def __init__(self, device_name: str, default_pv: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Configure XRF Viewer — {device_name}")
        self.setMinimumWidth(400)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        note = QLabel(
            f"<b>{device_name}</b> — enter the EPICS PV for the MCA spectrum array.\n"
            "Settings are saved to <tt>~/.easy_bluesky/xrf_viewer_settings.json</tt>."
        )
        note.setWordWrap(True)
        lay.addWidget(note)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        self._pv_edit = QLineEdit(default_pv)
        self._pv_edit.setPlaceholderText("e.g. IOC:MCA:.VAL  or  IOC:XSP3:MCA1:ArrayData")
        self._pv_edit.setToolTip("EPICS waveform PV holding the MCA spectrum array")
        form.addRow("Spectrum PV:", self._pv_edit)

        lay.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Open Viewer")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    @property
    def spectrum_pv(self) -> str:
        return self._pv_edit.text().strip()


def _device_color(module: str) -> tuple:
    m = (module or "").lower()
    if "sim" in m:
        return "#ff7f0e", "Simulated"
    if "areadetector" in m or "area_detector" in m:
        return "#9467bd", "AreaDetector"
    if "epics" in m:
        return "#2ca02c", "EPICS"
    if "flyer" in m:
        return "#17becf", "Flyer"
    if m in ("__main__", ""):
        return "#222222", "User-defined"
    return "#333333", "Other"


def _fmt_value(val) -> str:
    if val is None:
        return "—"
    # Unwrap numpy types — numpy ≥ 2.0 scalars are no longer Python float/int
    try:
        import numpy as _np
        if isinstance(val, _np.ndarray):
            if val.size == 1:
                val = val.flat[0].item()   # 1-element array → Python scalar
            else:
                return f"[{val.size} items]"
        elif isinstance(val, (_np.floating, _np.integer, _np.bool_)):
            val = val.item()
    except ImportError:
        pass
    if isinstance(val, float):
        return f"{val:.6g}"
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, int):
        return str(val)
    if isinstance(val, list):
        return f"[{len(val)} items]"
    return str(val)


# ── pyepics installer ───────────────────────────────────────────────────────────

class _EpicsInstaller(QThread):
    """Installs pyepics via pip in a background thread."""
    done = Signal(bool, str)   # success, message

    def run(self):
        import subprocess, sys, importlib
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "pyepics"],
                capture_output=True, text=True, timeout=120,
            )
            importlib.invalidate_caches()
            if r.returncode == 0:
                self.done.emit(True, "pyepics installed")
            else:
                self.done.emit(False, (r.stderr or r.stdout).strip()[-200:])
        except Exception as e:
            self.done.emit(False, str(e))


# ── pyepics stderr noise filter ─────────────────────────────────────────────────

class _CAStderrFilter:
    """Wraps sys.stderr to drop pyepics CA noise lines."""
    _SUPPRESS = ("cannot connect to", "ca.get(", "timed out after")

    def __init__(self, real):
        self._real = real

    def write(self, s: str):
        if not any(p in s for p in self._SUPPRESS):
            self._real.write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ── EPICS CA monitor ────────────────────────────────────────────────────────────

class _EPICSMonitor(QObject):
    """
    Wraps pyepics PV monitors and forwards value-change callbacks to Qt signals.
    Callbacks arrive on a CA background thread; emitting a Signal queues
    the update safely onto the main-thread event loop.
    """
    value_changed      = Signal(str, str, object, str)  # dev, sig, value, units
    connection_changed = Signal(str, str, bool)          # dev, sig, connected
    desc_changed       = Signal(str, str, str)           # dev, sig, desc
    # Internal: emitted from CA thread, received in main thread to do a safe get()
    _fetch_on_connect  = Signal(str)                     # pvname

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pvs: dict      = {}   # pvname → epics.PV  (strong refs)
        self._map: dict      = {}   # pvname → [(dev_name, sig_name), ...]  (list — multiple signals may share one PV)
        self._desc_pvs: dict = {}   # record.DESC → epics.PV  (one per record base)
        self._desc_map: dict = {}   # record.DESC → [(dev_name, sig_name), ...]
        self._alive: bool    = True  # guards callbacks after Qt C++ deletion
        # Queued connection ensures the slot always runs in the main Qt thread,
        # even when the signal is emitted from the CA background thread.
        self._fetch_on_connect.connect(
            self._do_fetch, Qt.ConnectionType.QueuedConnection
        )

    def setup(self, pv_map: dict):
        """Open CA monitors for every PV in pv_map = {dev: {sig: pvname}}.

        Processes device-level entries (dev_name != sig_name) before
        signal-level entries (dev_name == sig_name).  When the RE namespace
        exports a signal object alongside its parent device — e.g. both
        'mca1' and 'mca1_roi0_count' map to the same PV — the device-level
        entry wins.  Any subsequent entry for an already-mapped PV is skipped.
        """
        self.clear()
        self._alive = True   # clear() arms it False; re-arm for new subscriptions
        try:
            import epics
        except ImportError:
            return

        import sys
        if not isinstance(sys.stderr, _CAStderrFilter):
            sys.stderr = _CAStderrFilter(sys.stderr)

        for dev_name, sigs in pv_map.items():
            for sig_name, pvname in sigs.items():
                if not pvname:
                    continue
                # Multiple signals may share one PV (e.g. readback / user_readback).
                # Append to the list so every signal gets notified on each callback.
                self._map.setdefault(pvname, []).append((dev_name, sig_name))
                if pvname not in self._pvs:
                    pv = epics.PV(
                        pvname,
                        auto_monitor=True,
                        form='time',             # DBR_TIME: universally supported
                        callback=self._on_change,
                        connection_callback=self._on_connect,
                    )
                    self._pvs[pvname] = pv
                # Strip field suffix (e.g. "IOC:M1.RBV" → "IOC:M1") then add .DESC.
                # Appending .DESC directly would give "IOC:M1.RBV.DESC" (invalid).
                record_base = pvname.rsplit('.', 1)[0] if '.' in pvname else pvname
                desc_pvname = record_base + ".DESC"
                self._desc_map.setdefault(desc_pvname, []).append((dev_name, sig_name))
                if desc_pvname not in self._desc_pvs:
                    self._desc_pvs[desc_pvname] = epics.PV(
                        desc_pvname,
                        auto_monitor=True,
                        callback=self._on_desc_change,
                    )

    def clear(self):
        self._alive = False   # block in-flight CA callbacks from emitting
        for pv in list(self._pvs.values()) + list(self._desc_pvs.values()):
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()
        self._map.clear()
        self._desc_pvs.clear()
        self._desc_map.clear()

    def _on_change(self, pvname='', value=None, units='', **kw):
        if not self._alive or value is None:
            return
        for dev_name, sig_name in self._map.get(pvname, []):
            try:
                self.value_changed.emit(dev_name, sig_name, value, units or '')
            except RuntimeError:
                pass

    def _on_connect(self, pvname='', conn=True, **kw):
        if not self._alive:
            return
        for dev_name, sig_name in self._map.get(pvname, []):
            try:
                self.connection_changed.emit(dev_name, sig_name, bool(conn))
            except RuntimeError:
                pass
        if conn:
            try:
                self._fetch_on_connect.emit(pvname)
            except RuntimeError:
                pass

    def _do_fetch(self, pvname: str):
        # On connection, fetch the current value via caget (native DBR) and
        # attempt a separate DBR_CTRL get to retrieve engineering units.
        # DBR_CTRL is tried but ignored on failure (e.g. MCA .Rn fields).
        if not self._alive:
            return
        pv = self._pvs.get(pvname)
        if pv is None or not pv.connected:
            return
        try:
            import epics as _epics
            val = _epics.caget(pvname, timeout=0.5)
            if val is not None:
                units = ''
                try:
                    ctrl = pv.get(form='ctrl', use_monitor=False, timeout=0.3)
                    if ctrl is not None:
                        units = getattr(pv, 'units', '') or ''
                except Exception:
                    pass
                self._on_change(pvname=pvname, value=val, units=units)
        except Exception:
            pass

    def _on_desc_change(self, pvname='', value=None, **kw):
        if not self._alive:
            return
        infos = self._desc_map.get(pvname)
        if not infos or value is None:
            return
        if isinstance(value, bytes):
            desc = value.decode('latin-1', errors='replace')
        else:
            desc = str(value)
        desc = desc.strip()
        for dev_name, sig_name in infos:
            try:
                self.desc_changed.emit(dev_name, sig_name, desc)
            except RuntimeError:
                pass

    def put_value(self, pvname: str, value) -> str:
        """Write *value* to *pvname*.  Returns "" on success, error message on failure."""
        try:
            import epics
            pv = self._pvs.get(pvname)
            if pv is not None:
                # Reuse the already-connected PV object — guaranteed same CA channel
                # as our subscriptions, so if we can read the PV we can also write it.
                if not pv.connected:
                    return f"PV not connected"
                pv.put(value, wait=False)
            else:
                # Setpoint not in monitored set — fall back to a standalone caput.
                epics.caput(pvname, value, wait=False)
            return ""
        except Exception as e:
            return str(e)


# ── Tab widget ──────────────────────────────────────────────────────────────────

class DevicesPlansTab(QWidget):
    """Two-panel tab: live device tree (left) | plans + details (right)."""

    fetch_pvnames_requested   = Signal()
    reload_devices_requested  = Signal()   # full device+plan reload from RE env
    poll_sim_values_requested = Signal()
    set_sim_device_requested  = Signal(str, float)
    plan_file_open_requested  = Signal(str, str)   # (tier, name_or_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plans: dict = {}
        self._device_items: dict  = {}   # dev_name → QTreeWidgetItem
        self._signal_items: dict  = {}   # (dev_name, sig_name) → QTreeWidgetItem
        self._primary_signal: dict = {}  # dev_name → sig_name shown on device row
        self._readback_values: dict = {}  # dev_name → float | None (current readback)
        self._tweak_pvnames: dict = {}    # dev_name → user_setpoint pvname (EPICS)
        self._tweak_buttons: dict = {}    # dev_name → [QPushButton, ...] (for enable/disable)
        self._device_classes: dict = {}   # dev_name → classname (for sim detection)
        self._sim_mode: bool = False
        self._sim_device_names: set = set()   # devices polled via read_devices_status()
        self._pv_names_retry_count: int = 0
        self._sim_timer: QTimer | None = None
        self._tab_active: bool = False   # set by MainWindow via set_tab_active()
        # Persistent cache of units/desc from real EPICS so sim mode can show them
        self._metadata_cache: dict = {}   # dev_name → {"units": str, "desc": str}
        self._metadata_save_timer = QTimer(self)
        self._metadata_save_timer.setSingleShot(True)
        self._metadata_save_timer.setInterval(3000)
        self._metadata_save_timer.timeout.connect(self._save_metadata_cache)
        try:
            if _METADATA_PATH.exists():
                self._metadata_cache = json.loads(_METADATA_PATH.read_text())
        except Exception:
            pass
        self._epics_monitor = _EPICSMonitor(self)
        self._epics_monitor.value_changed.connect(self._on_pv_changed)
        self._epics_monitor.connection_changed.connect(self._on_pv_connected)
        self._epics_monitor.desc_changed.connect(self._on_desc_changed)
        self._pending_pv_map: dict = {}
        self._installer: _EpicsInstaller | None = None
        self._plan_catalog: PlanCatalog | None = None
        self._pv_map_cache:  dict = {}   # dev_name → {sig_name: pvname}
        self._ad_viewers:    dict = {}   # dev_name → ADViewerWindow
        self._sample_viewers: dict = {}  # dev_name → SampleStation window
        self._xrf_viewers:   dict = {}   # dev_name → XRFViewerWindow
        self._mca_viewers:   dict = {}   # dev_name → MCAViewerWindow
        self._conn_settings: dict = {}   # active connection profile
        self._tweak_step_values: dict = {}  # dev_name → step spinbox value (survives sort)
        self._sort_col:   int = -1          # -1 = unsorted
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
        # Coalesce CA value/desc callbacks — apply at most 10x/sec to avoid
        # flooding the tree widget with setText() calls during scans.
        self._pending_pv_updates: dict = {}    # (dev, sig) → (value, units)
        self._pending_desc_updates: dict = {}  # (dev, sig) → desc
        self._pv_flush_timer = QTimer(self)
        self._pv_flush_timer.setInterval(100)
        self._pv_flush_timer.timeout.connect(self._flush_pv_updates)
        self._pv_flush_timer.start()
        # Fingerprint to skip full rebuild when devices list is unchanged
        self._last_devices_fp: str = ""
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_devices())
        splitter.addWidget(self._build_plans())
        splitter.setSizes([600, 400])
        lay.addWidget(splitter)

    # ── Devices panel ───────────────────────────────────────────────────────────

    def _build_devices(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        hdr = QHBoxLayout()
        lbl = QLabel("AVAILABLE DEVICES")
        lbl.setObjectName("section_title")
        hdr.addWidget(lbl)
        hdr.addStretch()

        hdr.addWidget(QLabel("Update:"))
        self._flush_rate_spin = NoScrollDoubleSpinBox()
        self._flush_rate_spin.setRange(0.1, 10.0)
        self._flush_rate_spin.setSingleStep(0.1)
        self._flush_rate_spin.setDecimals(1)
        self._flush_rate_spin.setValue(0.1)
        self._flush_rate_spin.setSuffix(" s")
        self._flush_rate_spin.setFixedWidth(68)
        self._flush_rate_spin.setToolTip(
            "How often CA callback values are applied to the tree.\n"
            "Increase if the display is sluggish with many devices."
        )
        self._flush_rate_spin.valueChanged.connect(
            lambda v: self._pv_flush_timer.setInterval(int(v * 1000))
        )
        hdr.addWidget(self._flush_rate_spin)

        self._refresh_btn = QPushButton("⟳ Reconnect")
        self._refresh_btn.setFixedWidth(95)
        self._refresh_btn.setToolTip(
            "Re-fetch PV names from RE environment and reconnect CA monitors"
        )
        self._refresh_btn.clicked.connect(self._on_reconnect_clicked)
        hdr.addWidget(self._refresh_btn)
        vlay.addLayout(hdr)

        legend = QHBoxLayout()
        for color, label in [
            ("#ff7f0e", "Simulated"),
            ("#2ca02c", "EPICS"),
            ("#9467bd", "AreaDetector"),
            ("#17becf", "Flyer"),
            ("#d4d4d4", "Other"),
        ]:
            dot = QLabel(f"● {label}")
            dot.setStyleSheet(f"color: {color}; font-size: 11px;")
            legend.addWidget(dot)
        legend.addStretch()
        vlay.addLayout(legend)

        status_row = QHBoxLayout()
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        status_row.addWidget(self._status_lbl)
        status_row.addStretch()
        self._chk_poll_sim = QCheckBox("Poll Sim Devices")
        self._chk_poll_sim.setChecked(False)
        self._chk_poll_sim.setToolTip("Enable automatic 2-second polling of sim device values")
        self._chk_poll_sim.setVisible(False)
        self._chk_poll_sim.toggled.connect(self._on_poll_sim_toggled)
        status_row.addWidget(self._chk_poll_sim)
        vlay.addLayout(status_row)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search devices…")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_device_search)
        vlay.addWidget(self._search_box)

        self.devices_tree = QTreeWidget()
        self.devices_tree.setHeaderLabels(
            ["Device / Signal", "Class", "Value", "Units", "Description", "Tweak"]
        )
        self.devices_tree.setRootIsDecorated(True)
        self.devices_tree.setAlternatingRowColors(True)
        self.devices_tree.setSortingEnabled(False)  # manual sort to preserve col-5 widgets
        hdr = self.devices_tree.header()
        hdr.setSortIndicatorShown(True)
        hdr.setSectionsClickable(True)
        hdr.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        hdr.sectionClicked.connect(self._on_device_header_clicked)
        # resizeColumnToContents() ignores setItemWidget() widths, so column 5
        # (Tweak) would shrink to the "Tweak" header width (~50 px) and clip the
        # ◀/step/▶ widget.  Pre-size it; setup_epics_monitors enforces the minimum.
        self.devices_tree.setColumnWidth(5, 155)
        self.devices_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.devices_tree.customContextMenuRequested.connect(self._on_device_context_menu)
        vlay.addWidget(self.devices_tree, 1)
        return w

    # ── Plans panel ─────────────────────────────────────────────────────────────

    def _build_plans(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        hdr_row = QHBoxLayout()
        lbl = QLabel("AVAILABLE PLANS")
        lbl.setObjectName("section_title")
        self._plan_loading_lbl = QLabel()
        self._plan_loading_lbl.setStyleSheet(
            "font-size: 11px; color: #e8a44a; font-style: italic;")
        self._plan_loading_lbl.setVisible(False)
        self._plan_loading_timer = QTimer(self)
        self._plan_loading_timer.setInterval(400)
        self._plan_loading_timer.timeout.connect(self._tick_plan_loading)
        self._plan_loading_dots = 0
        hdr_row.addWidget(lbl)
        hdr_row.addStretch()
        hdr_row.addWidget(self._plan_loading_lbl)
        vlay.addLayout(hdr_row)

        # ── Legend ────────────────────────────────────────────────────────────
        legend = QLabel(
            f'<span style="color:{PLAN_COLORS["builtin"]}">■ Bluesky</span>&nbsp;&nbsp;'
            f'<span style="color:{PLAN_COLORS["profile"]}">■ Profile</span>&nbsp;&nbsp;'
            f'<span style="color:{PLAN_COLORS["session"]}">■ Session</span>'
        )
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setStyleSheet("font-size: 11px; padding: 2px 0;")
        vlay.addWidget(legend)

        # ── Search + type filter ──────────────────────────────────────────────
        filter_row = QHBoxLayout()
        self._plan_search = QLineEdit()
        self._plan_search.setPlaceholderText("Search plans…")
        self._plan_search.setClearButtonEnabled(True)
        self._plan_search.textChanged.connect(self._apply_plan_filter)

        self._plan_type_filter = QComboBox()
        self._plan_type_filter.addItems(["All Types", "Bluesky", "Profile", "Session"])
        self._plan_type_filter.setFixedWidth(100)
        self._plan_type_filter.currentTextChanged.connect(self._apply_plan_filter)

        filter_row.addWidget(self._plan_search, 1)
        filter_row.addWidget(self._plan_type_filter)
        vlay.addLayout(filter_row)

        # ── Plans tree ────────────────────────────────────────────────────────
        self.plans_tree = QTreeWidget()
        self.plans_tree.setColumnCount(3)
        self.plans_tree.setHeaderLabels(["Plan", "Type", "Description"])
        self.plans_tree.header().setStretchLastSection(True)
        self.plans_tree.header().resizeSection(0, 180)
        self.plans_tree.header().resizeSection(1, 72)
        self.plans_tree.setMaximumHeight(230)
        self.plans_tree.setRootIsDecorated(False)
        self.plans_tree.setSortingEnabled(False)
        self.plans_tree.currentItemChanged.connect(self._on_plan_selected)
        vlay.addWidget(self.plans_tree)

        lbl2 = QLabel("PARAMETERS")
        lbl2.setObjectName("section_title")
        vlay.addWidget(lbl2)

        self.plan_detail = QPlainTextEdit()
        self.plan_detail.setReadOnly(True)
        self.plan_detail.setPlaceholderText("Select a plan to view its parameters…")
        vlay.addWidget(self.plan_detail, 1)

        # ── Plan file tree (click → opens file in Code Editor) ────────────────
        self._plan_file_panel = PlanFileTreePanel(show_new_remote_btn=False)
        self._plan_file_panel.setMaximumHeight(200)
        self._plan_file_panel.file_open_requested.connect(
            self.plan_file_open_requested)
        vlay.addWidget(self._plan_file_panel)

        return w

    def set_profile(self, conn_settings: dict) -> None:
        """Called by MainWindow on connect to populate the plan file tree."""
        self._conn_settings = conn_settings or {}
        self._plan_file_panel.set_profile(conn_settings)

    # ── Public slots ────────────────────────────────────────────────────────────

    def on_disconnected(self):
        """Reset the device fingerprint so the next update_devices call does a
        full rebuild even if the device list is identical to the previous one.
        Without this, a reconnect after a dropped connection skips CA monitor
        setup because the fingerprint matches the stale cached value."""
        self._last_devices_fp = ""

    def update_devices(self, devices: dict):
        # Skip full rebuild if the device list is identical (same names + classes).
        # Avoids clearing CA monitors and re-fetching PV names on every poll cycle.
        fp = "|".join(
            f"{n}:{info.get('classname','')}"
            for n, info in sorted(devices.items())
        )
        if fp == self._last_devices_fp and devices:
            return
        self._last_devices_fp = fp
        self._pv_names_retry_count = 0

        if self._sim_timer is not None:
            self._sim_timer.stop()
            self._sim_timer = None
        self._sim_mode = False
        self._sim_device_names = set()

        self.devices_tree.clear()
        self._device_items.clear()
        self._signal_items.clear()
        self._primary_signal.clear()
        self._readback_values.clear()
        self._tweak_pvnames.clear()
        self._tweak_buttons.clear()
        self._device_classes.clear()
        self._epics_monitor.clear()

        if not devices:
            self._last_devices_fp = ""
            self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
            self._status_lbl.setText("● No devices — open the RE environment")
            self._refresh_btn.setEnabled(True)
            self._refresh_btn.setText("⟳ Reconnect")
            self._chk_poll_sim.setVisible(False)
            return

        groups: dict = {}
        for name, info in devices.items():
            module = info.get("module", "") or "Unknown"
            groups.setdefault(module, []).append((name, info))

        bold = QFont()
        bold.setBold(True)

        for module in sorted(groups.keys()):
            color, dev_type = _device_color(module)
            count = len(groups[module])
            group_item = QTreeWidgetItem([f"{module}  ({count})", "", "", ""])
            group_item.setForeground(0, QColor(color))
            group_item.setFont(0, bold)
            group_item.setToolTip(0, dev_type)

            for name, info in sorted(groups[module]):
                classname = info.get("classname", "")
                child = QTreeWidgetItem([name, classname, "", ""])
                child.setForeground(0, QColor(color))
                child.setForeground(1, QColor("#888"))
                child.setToolTip(0, f"Module: {module}")
                group_item.addChild(child)
                self._device_items[name] = child
                self._device_classes[name] = classname

            self.devices_tree.addTopLevelItem(group_item)

        self.devices_tree.expandAll()
        for i in range(5):
            self.devices_tree.resizeColumnToContents(i)
        self.devices_tree.setColumnWidth(5, max(self.devices_tree.columnWidth(5), 170))

        # Auto-start PV monitoring whenever a new device list arrives.
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        self._status_lbl.setText("● Fetching PV names…")
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Fetching…")
        self.fetch_pvnames_requested.emit()

    def _on_device_search(self, text: str):
        """Show only device rows whose name, class, or description match *text*."""
        q = text.strip().lower()
        root = self.devices_tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group = root.child(gi)
            any_visible = False
            for di in range(group.childCount()):
                dev = group.child(di)
                match = (
                    not q
                    or q in dev.text(0).lower()
                    or q in dev.text(1).lower()
                    or q in dev.text(4).lower()
                )
                dev.setHidden(not match)
                if match:
                    any_visible = True
            group.setHidden(not any_visible)

    def setup_epics_monitors(self, pv_map: dict):
        """Receive PV name map, create signal sub-rows and open CA monitors.

        Partitions devices into two groups:
        - EPICS devices: pv_map entry has ≥1 non-empty pvname → CA subscriptions
        - Polled devices: all pvnames empty (SynAxis, PseudoSingle, SynSignal…)
          → 2-second read_devices_status() polling

        Both groups can coexist (mixed beamline).
        """
        self._pv_map_cache = {dev: dict(sigs) for dev, sigs in pv_map.items()}
        try:
            import epics  # noqa: F401
        except ImportError:
            self._pending_pv_map = pv_map
            self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
            self._status_lbl.setText("pyepics not found — installing…")
            self._installer = _EpicsInstaller(self)
            self._installer.done.connect(self._on_install_done)
            self._installer.start()
            return

        self._epics_monitor.clear()
        self._signal_items.clear()
        self._primary_signal.clear()
        self._tweak_pvnames.clear()
        self._sim_device_names = set()
        self._sim_mode = False

        dim = QColor("#666666")

        # Partition: EPICS devices have ≥1 real (non-empty) pvname;
        # polled devices (SynAxis, PseudoSingle, SynSignal, …) have none.
        epics_pv_map = {dev: sigs for dev, sigs in pv_map.items()
                        if not dev.startswith('__') and any(v for v in sigs.values())}
        sim_dev_set = (set(pv_map) - set(epics_pv_map)
                       - {d for d in pv_map if d.startswith('__')})


        # ── Signal sub-rows + tweak widgets for EPICS devices ────────────
        for dev_name, sigs in epics_pv_map.items():
            item = self._device_items.get(dev_name)
            if item is None:
                continue

            while item.childCount() > 0:
                item.removeChild(item.child(0))

            primary = next(
                (s for s in ("user_readback", "readback", dev_name) if s in sigs),
                next(iter(sigs)),
            )
            self._primary_signal[dev_name] = primary

            for sig_name, pvname in sigs.items():
                sig_item = QTreeWidgetItem(
                    [f"  {sig_name}", "", "○ Connecting…", "", "", ""]
                )
                sig_item.setForeground(0, dim)
                sig_item.setForeground(2, QColor("#aaaaaa"))
                sig_item.setToolTip(0, pvname)
                item.addChild(sig_item)
                self._signal_items[(dev_name, sig_name)] = sig_item

            item.setText(2, "○ Connecting…")
            item.setForeground(2, QColor("#aaaaaa"))

            sp_pvname = sigs.get("user_setpoint") or sigs.get("setpoint") or ""
            if sp_pvname:
                self._tweak_pvnames[dev_name] = sp_pvname
                self.devices_tree.setItemWidget(
                    item, 5, self._make_tweak_widget(dev_name, sp_pvname)
                )

        total = sum(len(v) for v in epics_pv_map.values())

        # ── Tweak widgets for polled positioners (SynAxis, PseudoSingle) ─
        _SIM_MOTOR_CLASSES = {"SynAxis", "PseudoSingle"}
        for dev_name in sim_dev_set:
            item = self._device_items.get(dev_name)
            if item and self._device_classes.get(dev_name) in _SIM_MOTOR_CLASSES:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_tweak_widget(dev_name, None)
                )

        # ── Inline viewer buttons for AD and XRF/MCA devices ─────────────
        from .ad_viewer  import is_area_detector
        from .xrf_viewer import is_xrf_detector
        for dev_name, item in self._device_items.items():
            pv_map_dev = self._pv_map_cache.get(dev_name, {})
            classname  = self._device_classes.get(dev_name, "")
            is_ad  = is_area_detector(pv_map_dev, classname)
            is_xrf = is_xrf_detector(pv_map_dev, classname)
            if is_ad and is_xrf:
                # Both: two buttons side-by-side
                self.devices_tree.setItemWidget(
                    item, 5, self._make_ad_xrf_buttons(dev_name)
                )
            elif is_ad:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_ad_button(dev_name)
                )
            elif is_xrf:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_xrf_button(dev_name)
                )

        # ── Mode flags and status ────────────────────────────────────────
        if total == 0 and pv_map:
            # Pure sim: every device in pv_map has no EPICS PVs
            self._sim_mode = True
            self._sim_device_names = set(pv_map)
            self._status_lbl.setStyleSheet("font-size: 11px; color: #ff7f0e;")
            self._status_lbl.setText("● Sim — polling device values…")
        elif sim_dev_set:
            # Mixed: real EPICS devices + polled sim/pseudo devices
            self._epics_monitor.setup(epics_pv_map)
            self._sim_device_names = sim_dev_set
            self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
            self._status_lbl.setText(
                f"● Live — {total} PV(s) + {len(sim_dev_set)} polled"
            )
        else:
            # Pure EPICS: all devices have real PVs
            self._epics_monitor.setup(epics_pv_map)
            self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
            self._status_lbl.setText(f"● Live — monitoring {total} PV(s)")

        # ── Fallback: read any PVs still "Connecting…" after 4 s ────────
        # auto_monitor's initial callback can be missed for MCA field PVs
        # (e.g. .R0) when the MCA is idle.  _do_fetch (queued signal) handles
        # most cases; this timer catches anything that slipped through.
        if total > 0:
            self._fallback_attempt = 0
            QTimer.singleShot(4000, self._fallback_read_stuck_pvs)

        # ── Start polling timer for any polled devices ───────────────────
        if self._sim_device_names:
            self._sim_timer = QTimer(self)
            self._sim_timer.setInterval(2000)
            self._sim_timer.timeout.connect(self._on_sim_poll)
            self._chk_poll_sim.setVisible(True)
            if self._chk_poll_sim.isChecked():
                self._sim_timer.start()
                self._status_lbl.setText("● Sim — polling device values…")
            else:
                self._status_lbl.setText("● Sim — polling paused")
        else:
            self._chk_poll_sim.setVisible(False)

        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("⟳ Reconnect")
        for i in range(5):   # columns 0-4 only
            self.devices_tree.resizeColumnToContents(i)
        # Column 5 (Tweak): resizeColumnToContents ignores setItemWidget sizes,
        # so enforce a minimum wide enough for ◀ / step / ▶.
        self.devices_tree.setColumnWidth(5, max(self.devices_tree.columnWidth(5), 170))

    def on_pv_names_error(self, msg: str):
        _m = msg.lower()
        if "must be in idle" in _m or "executing_task" in _m or "executing task" in _m:
            # RE Manager busy (script_upload or other admin task in progress).
            # Retry a limited number of times so we don't keep RE Manager busy
            # with a perpetual stream of function_execute calls.
            self._pv_names_retry_count += 1
            if self._pv_names_retry_count <= 5:
                QTimer.singleShot(2000, self.fetch_pvnames_requested.emit)
            return
        self._status_lbl.setStyleSheet("font-size: 11px; color: #e05050;")
        self._status_lbl.setText(f"⚠ {msg[:120]}")
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("⟳ Reconnect")

    # ── Plan loading indicator ─────────────────────────────────────────────────

    def show_plan_loading(self, msg: str = "uploading") -> None:
        self._plan_loading_dots = 0
        self._plan_loading_lbl.setText(f"⟳ {msg}.")
        self._plan_loading_lbl.setVisible(True)
        self._plan_loading_timer.start()

    def hide_plan_loading(self) -> None:
        self._plan_loading_timer.stop()
        self._plan_loading_lbl.setVisible(False)

    def _tick_plan_loading(self) -> None:
        self._plan_loading_dots = (self._plan_loading_dots + 1) % 4
        text = self._plan_loading_lbl.text().split(".")[0]
        self._plan_loading_lbl.setText(text + "." * (self._plan_loading_dots + 1))

    # ── Plans update ───────────────────────────────────────────────────────────

    def update_plans(self, plans: dict):
        self.hide_plan_loading()
        self._plans = plans

        # Seed the catalog with module-field data from the RE Manager response
        if self._plan_catalog is not None:
            self._plan_catalog.classify_from_plans_dict(plans)

        cur = self.plans_tree.currentItem()
        current_name = cur.text(0) if cur else None

        self.plans_tree.clear()
        for name in sorted(plans.keys()):
            info = plans[name]

            # Determine type and color
            if self._plan_catalog is not None:
                ptype = self._plan_catalog.get_type(name)
            else:
                ptype = plan_type_from_module(info.get("module", "") or "")

            type_label = PLAN_TYPE_LABELS.get(ptype, ptype)
            color      = QBrush(QColor(PLAN_COLORS.get(ptype, "#cccccc")))
            desc       = (info.get("description") or "").split("\n")[0].strip()

            item = QTreeWidgetItem([name, type_label, desc])
            for col in range(3):
                item.setForeground(col, color)
            self.plans_tree.addTopLevelItem(item)

        # Restore previous selection
        if current_name:
            for i in range(self.plans_tree.topLevelItemCount()):
                if self.plans_tree.topLevelItem(i).text(0) == current_name:
                    self.plans_tree.setCurrentItem(self.plans_tree.topLevelItem(i))
                    break

        self._apply_plan_filter()

    _MAX_FALLBACK_ATTEMPTS = 3   # after 4 s + 3×6 s ≈ 22 s, give up

    def _fallback_read_stuck_pvs(self):
        """Called ~4 s after setup; retries up to _MAX_FALLBACK_ATTEMPTS times.

        After the final attempt any PV still 'Connecting…' is marked 'Not
        Available' in red so the user knows it is unreachable.  caget() calls
        run in a daemon thread so they never freeze the UI.
        """
        self._fallback_attempt = getattr(self, '_fallback_attempt', 0) + 1
        is_final = self._fallback_attempt >= self._MAX_FALLBACK_ATTEMPTS

        stuck = []
        for pvname in list(self._epics_monitor._pvs):
            pairs = self._epics_monitor._map.get(pvname)
            if not pairs:
                continue
            for dev_name, sig_name in pairs:
                sig_item = self._signal_items.get((dev_name, sig_name))
                if sig_item is None:
                    continue
                txt = sig_item.text(2)
                if "Connecting" in txt or txt == "○ —":
                    stuck.append(pvname)
                    break

        if not stuck:
            return

        monitor = self._epics_monitor

        def _bg():
            import epics as _ep
            try:
                # Bind this thread to the existing CA context so ca_pend_io
                # uses the same context as the main thread.  Without this the
                # call crashes with SIGSEGV if the context is torn down during
                # app shutdown while the thread is still inside ca_pend_io.
                _ep.ca.use_initial_context()
            except Exception:
                return
            for pvname in stuck:
                if not monitor._alive:
                    return
                try:
                    val = _ep.caget(pvname, timeout=0.5)
                    if val is not None and monitor._alive:
                        monitor._on_change(pvname=pvname, value=val, units='')
                except Exception:
                    pass

        threading.Thread(target=_bg, daemon=True).start()

        if is_final:
            # Schedule "Not Available" labelling after the bg thread has had time
            # to deliver any last-moment values (200 ms grace).
            QTimer.singleShot(200, self._mark_unavailable_pvs)
        else:
            QTimer.singleShot(6000, self._fallback_read_stuck_pvs)

    def _mark_unavailable_pvs(self):
        """Label any tree items still 'Connecting…' as 'Not Available' in red."""
        red = QColor("#e05050")
        for pvname in list(self._epics_monitor._pvs):
            pairs = self._epics_monitor._map.get(pvname)
            if not pairs:
                continue
            for dev_name, sig_name in pairs:
                sig_item = self._signal_items.get((dev_name, sig_name))
                if sig_item is None:
                    continue
                if "Connecting" in sig_item.text(2) or sig_item.text(2) == "○ —":
                    sig_item.setText(2, "Not Available")
                    sig_item.setForeground(2, red)
                    sig_item.setText(3, "")
                # Mirror to device row if this is the primary signal
                if self._primary_signal.get(dev_name) == sig_name:
                    dev_item = self._device_items.get(dev_name)
                    if dev_item and (
                        "Connecting" in dev_item.text(2) or dev_item.text(2) == "○ —"
                    ):
                        dev_item.setText(2, "Not Available")
                        dev_item.setForeground(2, red)

    # ── Internal ────────────────────────────────────────────────────────────────

    def _on_pv_connected(self, dev_name: str, sig_name: str, connected: bool):
        grey = QColor("#888888")
        red  = QColor("#e05050")

        sig_item = self._signal_items.get((dev_name, sig_name))
        if sig_item:
            if connected:
                # Clear the placeholder; actual value arrives via _on_pv_changed
                if sig_item.text(2) == "○ Connecting…":
                    sig_item.setText(2, "○ —")
                    sig_item.setForeground(2, grey)
            else:
                sig_item.setText(2, "○ Disconnected")
                sig_item.setForeground(2, red)
                sig_item.setText(3, "")

        if self._primary_signal.get(dev_name) == sig_name:
            dev_item = self._device_items.get(dev_name)
            if dev_item:
                if connected:
                    if dev_item.text(2) == "○ Connecting…":
                        dev_item.setText(2, "○ —")
                        dev_item.setForeground(2, grey)
                else:
                    dev_item.setText(2, "○ Disconnected")
                    dev_item.setForeground(2, red)
                    dev_item.setText(3, "")

    def _on_pv_changed(self, dev_name: str, sig_name: str, value, units: str):
        # Buffer — tree setText() calls are flushed at 10 Hz by _flush_pv_updates
        self._pending_pv_updates[(dev_name, sig_name)] = (value, units)
        # Track numeric readback immediately (used for tweak calculations).
        # Use try/float() rather than isinstance so numpy scalars work regardless
        # of numpy version (numpy ≥2.0 dropped float subclassing).
        if sig_name in ("user_readback", "readback") or (
            self._primary_signal.get(dev_name) == sig_name
        ):
            try:
                self._readback_values[dev_name] = float(value)
            except (TypeError, ValueError):
                pass
        # Cache units (no Qt tree ops)
        if units:
            self._metadata_cache.setdefault(dev_name, {})["units"] = units
            self._metadata_save_timer.start()

    def _on_desc_changed(self, dev_name: str, sig_name: str, desc: str):
        # Buffer — applied at 10 Hz by _flush_pv_updates
        self._pending_desc_updates[(dev_name, sig_name)] = desc
        if desc:
            self._metadata_cache.setdefault(dev_name, {})["desc"] = desc
            self._metadata_save_timer.start()

    def _flush_pv_updates(self):
        """Apply buffered CA value/desc updates to the tree (called at 10 Hz)."""
        if not self._pending_pv_updates and not self._pending_desc_updates:
            return

        green = QColor("#2ca02c")
        dim   = QColor("#666666")

        # Suspend repaints for the whole batch — each setText() on a visible
        # QTreeWidgetItem otherwise triggers a synchronous repaint, making the
        # flush O(N×repaint_cost) instead of O(1×repaint_cost).
        self.devices_tree.setUpdatesEnabled(False)
        try:
            pv_updates, self._pending_pv_updates = self._pending_pv_updates, {}
            for (dev_name, sig_name), (value, units) in pv_updates.items():
                sig_item = self._signal_items.get((dev_name, sig_name))
                if sig_item:
                    val_str = _fmt_value(value)
                    if sig_item.text(2) != val_str:
                        sig_item.setText(2, val_str)
                        sig_item.setForeground(2, dim)
                    if sig_item.text(3) != units:
                        sig_item.setText(3, units)
                if self._primary_signal.get(dev_name) == sig_name:
                    dev_item = self._device_items.get(dev_name)
                    if dev_item:
                        val_str = _fmt_value(value)
                        if dev_item.text(2) != val_str:
                            dev_item.setText(2, val_str)
                            dev_item.setForeground(2, green)
                        if dev_item.text(3) != units:
                            dev_item.setText(3, units)

            desc_updates, self._pending_desc_updates = self._pending_desc_updates, {}
            for (dev_name, sig_name), desc in desc_updates.items():
                sig_item = self._signal_items.get((dev_name, sig_name))
                if sig_item and sig_item.text(4) != desc:
                    sig_item.setText(4, desc)
                if self._primary_signal.get(dev_name) == sig_name:
                    dev_item = self._device_items.get(dev_name)
                    if dev_item and dev_item.text(4) != desc:
                        dev_item.setText(4, desc)
        finally:
            self.devices_tree.setUpdatesEnabled(True)

    def _save_metadata_cache(self):
        try:
            _METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            _METADATA_PATH.write_text(json.dumps(self._metadata_cache, indent=2))
        except Exception:
            pass

    def _make_ad_button(self, dev_name: str) -> QWidget:
        """Inline 'Open AD Viewer' button for area-detector devices."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 1, 2, 1)
        btn = QPushButton("📺 Open AD Viewer")
        btn.setFixedHeight(22)
        btn.setStyleSheet("padding: 1px 6px;")
        btn.setToolTip(f"Open area-detector live viewer for {dev_name}")
        btn.clicked.connect(
            lambda _checked, n=dev_name: self._open_ad_viewer(
                n, self._pv_map_cache.get(n, {}), force_dialog=False
            )
        )
        h.addWidget(btn)
        return w

    def _make_xrf_button(self, dev_name: str) -> QWidget:
        """Inline dropdown button offering both MCA/XRF viewer options."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 1, 2, 1)
        h.addWidget(self._make_xrf_tool_button(dev_name, full_label=True))
        return w

    def _make_xrf_tool_button(self, dev_name: str, full_label: bool = False) -> QToolButton:
        btn = QToolButton()
        btn.setText("📊 Open XRF Viewer" if full_label else "📊 XRF")
        btn.setFixedHeight(22)
        btn.setStyleSheet("padding: 1px 6px;")
        btn.setToolTip(f"Open spectrum viewer for {dev_name}")
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(btn)
        act_xrf = menu.addAction("Open XRF Viewer (PyMCA)")
        act_mca = menu.addAction("Open MCA Viewer (IOC ROIs)")
        btn.setMenu(menu)
        # Default click (arrow-less part) opens PyMCA viewer
        btn.clicked.connect(
            lambda _c, n=dev_name: self._open_xrf_viewer(
                n, self._pv_map_cache.get(n, {}), force_dialog=False
            )
        )
        act_xrf.triggered.connect(
            lambda _c, n=dev_name: self._open_xrf_viewer(
                n, self._pv_map_cache.get(n, {}), force_dialog=False
            )
        )
        act_mca.triggered.connect(
            lambda _c, n=dev_name: self._open_mca_viewer(
                n, self._pv_map_cache.get(n, {})
            )
        )
        return btn

    def _make_ad_xrf_buttons(self, dev_name: str) -> QWidget:
        """Inline AD + XRF buttons side-by-side for devices that are both."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 1, 2, 1)
        h.setSpacing(3)
        ad_btn = QPushButton("📺 AD")
        ad_btn.setFixedHeight(22)
        ad_btn.setStyleSheet("padding: 1px 6px;")
        ad_btn.setToolTip(f"Open area-detector live viewer for {dev_name}")
        ad_btn.clicked.connect(
            lambda _checked, n=dev_name: self._open_ad_viewer(
                n, self._pv_map_cache.get(n, {}), force_dialog=False
            )
        )
        h.addWidget(ad_btn)
        h.addWidget(self._make_xrf_tool_button(dev_name, full_label=False))
        return w

    def _make_tweak_widget(self, dev_name: str, setpoint_pvname: str | None) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(2)

        step = NoScrollDoubleSpinBox()
        step.setRange(0.0001, 100000)
        step.setValue(self._tweak_step_values.get(dev_name, 0.1))
        step.setDecimals(4)
        step.setFixedWidth(82)
        step.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        step.setToolTip("Tweak step size")
        step.wheelEvent = lambda e: e.ignore()

        btn_minus = QPushButton("◀")
        btn_plus  = QPushButton("▶")
        for btn in (btn_minus, btn_plus):
            btn.setFixedWidth(24)
            btn.setFixedHeight(22)

        def _move(sign: int):
            cur = self._readback_values.get(dev_name)
            if cur is None:
                # No CA callback yet — try to read the displayed value from the tree.
                item = self._device_items.get(dev_name)
                if item:
                    try:
                        cur = float(item.text(2))
                    except (ValueError, TypeError):
                        cur = 0.0
                else:
                    cur = 0.0
            new_val = cur + sign * step.value()
            if setpoint_pvname is None:
                # Sim device: disable buttons while the QThread is in flight.
                btn_minus.setEnabled(False)
                btn_plus.setEnabled(False)
                self._status_lbl.setText(
                    f"↦ Tweaking {dev_name} → {new_val:.6g}…"
                )
                self._status_lbl.setStyleSheet("font-size: 11px; color: #e8a44a;")
                self.set_sim_device_requested.emit(dev_name, new_val)
                self._readback_values[dev_name] = new_val  # optimistic update
                # Re-poll after the QThread completes (on_sim_device_set_done does it).
                # Also poll via the regular timer in case the signal doesn't arrive.
                QTimer.singleShot(800, self.poll_sim_values_requested.emit)
            else:
                err = self._epics_monitor.put_value(setpoint_pvname, new_val)
                if err:
                    self._status_lbl.setText(f"⚠ {setpoint_pvname}: {err}")
                    self._status_lbl.setStyleSheet("font-size: 11px; color: #e05050;")
                    QTimer.singleShot(4000, self._restore_status_label)
                else:
                    self._status_lbl.setText(
                        f"↦ {setpoint_pvname} → {new_val:.6g}  (from {cur:.6g})"
                    )
                    self._status_lbl.setStyleSheet("font-size: 11px; color: #e8a44a;")
                    QTimer.singleShot(4000, self._restore_status_label)

        btn_minus.clicked.connect(lambda: _move(-1))
        btn_plus.clicked.connect(lambda: _move(+1))
        step.valueChanged.connect(lambda v, n=dev_name: self._tweak_step_values.__setitem__(n, v))
        # Store button refs so on_sim_device_set_done can re-enable them.
        self._tweak_buttons.setdefault(dev_name, []).extend([btn_minus, btn_plus])

        h.addWidget(btn_minus)
        h.addWidget(step)
        h.addWidget(btn_plus)
        return w

    def set_tab_active(self, active: bool) -> None:
        """Called by MainWindow when this tab is shown or hidden."""
        self._tab_active = active

    def _on_sim_poll(self):
        """Timer callback — only emit when the tab is visible to avoid unnecessary function_execute traffic."""
        if self._tab_active:
            self.poll_sim_values_requested.emit()

    def pause_sim_poll(self):
        """Stop the sim poll timer so function_execute doesn't race with queue_start."""
        if self._sim_timer is not None:
            self._sim_timer.stop()

    def resume_sim_poll(self):
        """Restart the sim poll timer after queue_start has been sent (only if checkbox is on)."""
        if self._sim_timer is not None and self._chk_poll_sim.isChecked():
            self._sim_timer.start()

    def _on_poll_sim_toggled(self, checked: bool):
        if self._sim_timer is None:
            return
        if checked:
            self._sim_timer.start()
            self._status_lbl.setText("● Sim — polling device values…")
        else:
            self._sim_timer.stop()
            self._status_lbl.setText("● Sim — polling paused")

    def update_sim_values(self, readings: dict):
        """Update Value/Units/Description columns for polled (sim/pseudo) devices."""
        if not self._sim_device_names:
            return
        for dev_name, data in readings.items():
            if dev_name not in self._sim_device_names:
                continue  # in mixed mode, EPICS devices are updated by CA callbacks
            item = self._device_items.get(dev_name)
            if item is None:
                continue
            reading = data.get("reading", {})
            if not reading:
                continue
            key = next(iter(reading))
            val_data = reading[key]
            val = val_data.get("value")
            if val is None:
                continue
            item.setText(2, _fmt_value(val))
            item.setForeground(2, QColor("#dddddd"))
            try:
                self._readback_values[dev_name] = float(val)
            except (TypeError, ValueError):
                pass

            meta = self._metadata_cache.get(dev_name, {})
            if meta.get("units"):
                item.setText(3, meta["units"])
            if meta.get("desc"):
                item.setText(4, meta["desc"])

    def on_sim_device_set_done(self, dev_name: str, success: bool, msg: str):
        """Called when _SimDeviceSetter finishes. Re-enable tweak buttons and refresh."""
        for btn in self._tweak_buttons.get(dev_name, []):
            try:
                btn.setEnabled(True)
            except RuntimeError:
                pass  # widget already deleted
        if success:
            self._restore_status_label()
            # Poll immediately for the updated value.
            self.poll_sim_values_requested.emit()
        else:
            self._status_lbl.setStyleSheet("font-size: 11px; color: #e05050;")
            self._status_lbl.setText(f"⚠ Tweak {dev_name} failed: {msg[:100]}")
            QTimer.singleShot(4000, self._restore_status_label)

    # ── Device tree sorting ────────────────────────────────────────────────────

    def _on_device_header_clicked(self, col: int):
        if col not in (0, 1):
            return
        if self._sort_col == col:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_col   = col
            self._sort_order = Qt.SortOrder.AscendingOrder
        self.devices_tree.header().setSortIndicator(col, self._sort_order)
        self._sort_devices_tree(col, self._sort_order)

    def _sort_devices_tree(self, col: int, order: Qt.SortOrder):
        reverse = (order == Qt.SortOrder.DescendingOrder)
        root    = self.devices_tree.invisibleRootItem()

        # Lift all top-level group items
        groups = [root.takeChild(0) for _ in range(root.childCount())]
        groups.sort(key=lambda it: it.text(col).lower(), reverse=reverse)

        for grp in groups:
            # Sort device children within each group
            children = [grp.takeChild(0) for _ in range(grp.childCount())]
            children.sort(key=lambda it: it.text(col).lower(), reverse=reverse)
            for ch in children:
                grp.addChild(ch)
            root.addChild(grp)

        self.devices_tree.expandAll()
        # Qt stores item-widgets by model index, not by item pointer, so they
        # become detached after a takeChild/addChild reorder.  Re-apply them all.
        self._reapply_col5_widgets()

    def _reapply_col5_widgets(self):
        from .ad_viewer  import is_area_detector
        from .xrf_viewer import is_xrf_detector
        _SIM_MOTOR_CLASSES = {"SynAxis", "PseudoSingle"}
        self._tweak_buttons.clear()
        for dev_name, item in self._device_items.items():
            pv_map_dev = self._pv_map_cache.get(dev_name, {})
            classname  = self._device_classes.get(dev_name, "")
            is_ad  = is_area_detector(pv_map_dev, classname)
            is_xrf = is_xrf_detector(pv_map_dev, classname)
            if is_ad and is_xrf:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_ad_xrf_buttons(dev_name))
            elif is_ad:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_ad_button(dev_name))
            elif is_xrf:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_xrf_button(dev_name))
            elif dev_name in self._tweak_pvnames:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_tweak_widget(dev_name, self._tweak_pvnames[dev_name]))
            elif self._sim_mode and classname in _SIM_MOTOR_CLASSES:
                self.devices_tree.setItemWidget(
                    item, 5, self._make_tweak_widget(dev_name, None))

    def _restore_status_label(self):
        """Restore the status label to its normal connected/sim state."""
        if self._sim_mode:
            self._status_lbl.setStyleSheet("font-size: 11px; color: #ff7f0e;")
            self._status_lbl.setText("● Sim — polling device values…")
        elif self._sim_device_names:
            n_epics = sum(
                1 for d in self._device_items if d not in self._sim_device_names
            )
            self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
            self._status_lbl.setText(
                f"● Live — {n_epics} PV(s) + {len(self._sim_device_names)} polled"
            )
        else:
            self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
            self._status_lbl.setText(
                f"● Live — monitoring PVs"
            )

    def _on_install_done(self, success: bool, msg: str):
        if success:
            self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
            self._status_lbl.setText("✓ pyepics installed — connecting monitors…")
            self.setup_epics_monitors(self._pending_pv_map)
        else:
            self._status_lbl.setStyleSheet("font-size: 11px; color: #e05050;")
            self._status_lbl.setText(f"⚠ Failed to install pyepics: {msg[:120]}")

    def _on_reconnect_clicked(self):
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Loading…")
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        self._status_lbl.setText("● Reloading devices from RE environment…")
        self._last_devices_fp = ""   # force full rebuild on next update_devices
        self.reload_devices_requested.emit()

    def _on_plan_selected(self, current, _previous):
        if not current:
            self.plan_detail.clear()
            return
        name   = current.text(0)
        info   = self._plans.get(name, {})
        params = info.get("parameters", [])
        lines  = [f"Plan: {name}", ""]

        # Source info when catalog is available
        if self._plan_catalog is not None:
            src = self._plan_catalog.get_source(name)
            if src:
                lines.append(f"Source: {src}")
                lines.append("")

        if params:
            lines.append("Parameters:")
            for p in params:
                pname      = p.get("name", "")
                annotation = p.get("annotation", {})
                default    = p.get("default", "<required>")
                ptype = annotation.get("type", "") if isinstance(annotation, dict) else str(annotation)
                lines.append(f"  {pname}: {ptype}  (default: {default})")
        else:
            lines.append("No parameters.")
        self.plan_detail.setPlainText("\n".join(lines))

    def set_plan_catalog(self, catalog: PlanCatalog) -> None:
        """Set the PlanCatalog used for type classification and source lookup."""
        self._plan_catalog = catalog

    # ── Device context menu (AD Viewer + XRF Viewer) ────────────────────────────

    def _on_device_context_menu(self, pos):
        from qtpy.QtGui import QClipboard
        from qtpy.QtWidgets import QApplication

        item = self.devices_tree.itemAt(pos)
        if item is None:
            return

        menu = QMenu(self)
        acts: dict = {}

        # ── Signal sub-row: offer copy PV name ──────────────────────────────
        parent = item.parent()
        if parent is not None and parent in self._device_items.values():
            pvname = item.toolTip(0)
            sig_label = item.text(0).strip()
            if pvname:
                acts['copy_pv'] = menu.addAction(f"Copy PV name:  {pvname}")
            action = menu.exec(self.devices_tree.viewport().mapToGlobal(pos))
            if action is not None and action is acts.get('copy_pv'):
                QApplication.clipboard().setText(pvname)
            return

        # ── Device row ───────────────────────────────────────────────────────
        dev_name = item.text(0).strip()
        if dev_name not in self._device_items:
            return  # group header — nothing to show

        pv_map_dev = self._pv_map_cache.get(dev_name, {})
        classname  = self._device_classes.get(dev_name, "")

        # Copy all PV names for this device
        pvnames = {sig: pv for sig, pv in pv_map_dev.items() if pv}
        if pvnames:
            if len(pvnames) == 1:
                only_pv = next(iter(pvnames.values()))
                acts['copy_pv'] = menu.addAction(f"Copy PV name:  {only_pv}")
            else:
                acts['copy_pv'] = menu.addAction("Copy all PV names")
            menu.addSeparator()

        from .ad_viewer  import is_area_detector
        from .xrf_viewer import is_xrf_detector
        is_ad  = is_area_detector(pv_map_dev, classname)
        is_xrf = is_xrf_detector(pv_map_dev, classname)
        if is_ad:
            acts['ad_open'] = menu.addAction("Open AD Viewer")
            acts['ad_cfg']  = menu.addAction("Configure AD Viewer…")
        if is_xrf:
            if is_ad:
                menu.addSeparator()
            acts['xrf_open'] = menu.addAction("Open XRF Viewer (PyMCA)")
            acts['xrf_cfg']  = menu.addAction("Configure XRF Viewer…")
            acts['mca_open'] = menu.addAction("Open MCA Viewer (IOC ROIs)")

        if not acts:
            return

        action = menu.exec(self.devices_tree.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action is acts.get('copy_pv'):
            text = "\n".join(pvnames.values()) if len(pvnames) > 1 else next(iter(pvnames.values()))
            QApplication.clipboard().setText(text)
        elif action is acts.get('ad_open'):
            self._open_ad_viewer(dev_name, pv_map_dev, force_dialog=False)
        elif action is acts.get('ad_cfg'):
            self._open_ad_viewer(dev_name, pv_map_dev, force_dialog=True)
        elif action is acts.get('xrf_open'):
            self._open_xrf_viewer(dev_name, pv_map_dev, force_dialog=False)
        elif action is acts.get('xrf_cfg'):
            self._open_xrf_viewer(dev_name, pv_map_dev, force_dialog=True)
        elif action is acts.get('mca_open'):
            self._open_mca_viewer(dev_name, pv_map_dev)

    def _open_ad_viewer(self, dev_name: str, pv_map_dev: dict,
                        force_dialog: bool = False):
        from .ad_viewer import (ADViewerWindow, extract_ad_prefix,
                                extract_ad_pva_pv,
                                _HAS_P4P, _P4P_ERROR,
                                load_ad_settings, save_ad_settings)

        if not _HAS_P4P:
            detail = f"\n\nError: {_P4P_ERROR}" if _P4P_ERROR else ""
            QMessageBox.warning(
                self, "p4p not available",
                "The p4p package is required for PVA image streaming.\n\n"
                "Install it with:\n    pip install p4p"
                + detail,
            )
            return

        # Load saved per-device settings
        ad_settings = load_ad_settings()
        saved       = ad_settings.get(dev_name, {})

        # Auto-detect from PV map: full PVA PV first, then base prefix
        auto_pva_pv = extract_ad_pva_pv(pv_map_dev)
        auto_prefix = extract_ad_prefix(pv_map_dev)
        prefix      = auto_prefix or saved.get('prefix', '')

        # Host: saved override first, then active profile host
        profile_host = self._conn_settings.get('host', '')
        pva_host     = saved.get('pva_host', '') or profile_host

        sample_view_mode = saved.get('sample_view_mode', False)

        # Show config dialog when forced, prefix unknown, or host not yet saved
        if force_dialog or not prefix or not pva_host:
            dlg = _ADConfigDialog(
                dev_name,
                prefix or f"{dev_name}:",
                pva_host,
                default_sample_view=sample_view_mode,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            prefix           = dlg.prefix
            pva_host         = dlg.host
            sample_view_mode = dlg.sample_view_mode

        # Persist settings without overwriting other saved keys (colormap etc.)
        ad_settings.setdefault(dev_name, {}).update(
            {'prefix': prefix, 'pva_host': pva_host, 'sample_view_mode': sample_view_mode}
        )
        save_ad_settings(ad_settings)

        if sample_view_mode:
            self._open_sample_viewer(dev_name)
            return

        # Bring existing window to front rather than open a second one
        existing = self._ad_viewers.get(dev_name)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                pass

        viewer = ADViewerWindow(dev_name, prefix, pv_map_dev,
                                pva_host=pva_host,
                                pva_pv=auto_pva_pv or "",
                                parent=None)
        self._ad_viewers[dev_name] = viewer
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.destroyed.connect(lambda _, n=dev_name: self._ad_viewers.pop(n, None))
        viewer.show()

    def _open_sample_viewer(self, dev_name: str) -> None:
        """Launch the ASWAXS Sample View station (aswaxs-sample-view package)."""
        existing = self._sample_viewers.get(dev_name)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                pass

        try:
            from sample_station import SampleStation  # noqa: PLC0415
        except ImportError:
            QMessageBox.warning(
                self,
                "aswaxs-sample-view not installed",
                "The Sample View Mode requires the aswaxs-sample-view package.\n\n"
                "Install it with:\n"
                "    pip install git+https://github.com/JIAJTIAN/ASWAXS_Sample_View.git",
            )
            return

        viewer = SampleStation()
        viewer.setWindowTitle(f"Sample View — {dev_name}" if dev_name else "Sample View")
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._sample_viewers[dev_name] = viewer
        viewer.destroyed.connect(lambda _, n=dev_name: self._sample_viewers.pop(n, None))
        viewer.show()

    # ── XRF Viewer ───────────────────────────────────────────────────────────────

    def _open_xrf_viewer(self, dev_name: str, pv_map_dev: dict,
                         force_dialog: bool = False):
        from .xrf_viewer import (XRFViewerWindow, extract_xrf_spectrum_pv,
                                  load_xrf_settings, save_xrf_settings)

        saved       = load_xrf_settings().get(dev_name, {})
        auto_pv     = extract_xrf_spectrum_pv(pv_map_dev)
        spectrum_pv = auto_pv or saved.get('spectrum_pv', '')

        if force_dialog or not spectrum_pv:
            dlg = _XRFConfigDialog(
                dev_name,
                spectrum_pv or f"{dev_name}:mca1.VAL",
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            spectrum_pv = dlg.spectrum_pv

        load_xrf_settings()  # re-read fresh
        xrf_settings = load_xrf_settings()
        xrf_settings.setdefault(dev_name, {}).update({'spectrum_pv': spectrum_pv})
        save_xrf_settings(xrf_settings)

        existing = self._xrf_viewers.get(dev_name)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                pass

        viewer = XRFViewerWindow(dev_name, spectrum_pv, pv_map_dev, parent=None)
        self._xrf_viewers[dev_name] = viewer
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.destroyed.connect(lambda _, n=dev_name: self._xrf_viewers.pop(n, None))
        viewer.show()

    def _open_mca_viewer(self, dev_name: str, pv_map_dev: dict):
        from .mca_viewer import MCAViewerWindow, extract_mca_prefix

        existing = self._mca_viewers.get(dev_name)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                pass

        prefix = extract_mca_prefix(pv_map_dev) or ""
        viewer = MCAViewerWindow(dev_name, prefix, pv_map_dev, parent=None)
        self._mca_viewers[dev_name] = viewer
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.destroyed.connect(lambda _, n=dev_name: self._mca_viewers.pop(n, None))
        viewer.show()

    def close_all_viewers(self):
        """Close all open AD Viewer, XRF Viewer and Sample View windows."""
        for viewers in (self._ad_viewers, self._xrf_viewers,
                        self._mca_viewers, self._sample_viewers):
            for win in list(viewers.values()):
                try:
                    win.close()
                except RuntimeError:
                    pass
            viewers.clear()

    # ── Public launchers for Tools menu ─────────────────────────────────────────

    def open_ad_viewer_from_menu(self):
        """Show a device picker (or config dialog) and open an AD Viewer."""
        from .ad_viewer import is_area_detector
        ad_devices = [
            dev for dev, pvm in self._pv_map_cache.items()
            if is_area_detector(pvm, self._device_classes.get(dev, ''))
        ]
        if not ad_devices:
            # No devices loaded — open config dialog with blank fields
            self._open_ad_viewer("", {}, force_dialog=True)
            return
        if len(ad_devices) == 1:
            self._open_ad_viewer(ad_devices[0],
                                  self._pv_map_cache[ad_devices[0]],
                                  force_dialog=False)
            return
        from qtpy.QtWidgets import QInputDialog
        name, ok = QInputDialog.getItem(
            self, "Open AD Viewer", "Select area detector:", ad_devices, 0, False)
        if ok and name:
            self._open_ad_viewer(name, self._pv_map_cache[name], force_dialog=False)

    def open_xrf_viewer_from_menu(self):
        """Show a device picker (or config dialog) and open an XRF Viewer."""
        from .xrf_viewer import is_xrf_detector
        xrf_devices = [
            dev for dev, pvm in self._pv_map_cache.items()
            if is_xrf_detector(pvm, self._device_classes.get(dev, ''))
        ]
        if not xrf_devices:
            self._open_xrf_viewer("", {}, force_dialog=True)
            return
        if len(xrf_devices) == 1:
            self._open_xrf_viewer(xrf_devices[0],
                                   self._pv_map_cache[xrf_devices[0]],
                                   force_dialog=False)
            return
        from qtpy.QtWidgets import QInputDialog
        name, ok = QInputDialog.getItem(
            self, "Open XRF Viewer", "Select XRF detector:", xrf_devices, 0, False)
        if ok and name:
            self._open_xrf_viewer(name, self._pv_map_cache[name], force_dialog=False)

    # ── Plan filter ──────────────────────────────────────────────────────────────

    def _apply_plan_filter(self) -> None:
        """Show/hide plan rows based on text search and type-filter combo."""
        text        = self._plan_search.text().lower()
        type_filter = self._plan_type_filter.currentText()   # "All Types" | "Bluesky" | "Profile" | "Session"

        for i in range(self.plans_tree.topLevelItemCount()):
            item      = self.plans_tree.topLevelItem(i)
            name      = item.text(0).lower()
            type_lbl  = item.text(1)
            desc      = item.text(2).lower()

            text_match = not text or (text in name or text in desc)
            type_match = type_filter == "All Types" or type_lbl == type_filter

            item.setHidden(not (text_match and type_match))
