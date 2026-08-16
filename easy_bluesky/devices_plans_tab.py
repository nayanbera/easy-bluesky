"""devices_plans_tab.py — Devices & Plans browser tab."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QTreeWidget, QTreeWidgetItem,
    QPlainTextEdit, QPushButton, QDoubleSpinBox, QLineEdit, QComboBox, QMenu,
    QMessageBox, QDialog, QFormLayout, QDialogButtonBox,
)
from .widgets import NoScrollDoubleSpinBox
import json
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont

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
                 default_host: str, parent=None):
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
    if isinstance(val, float):
        return f"{val:.6g}"
    if isinstance(val, list):
        return f"[{len(val)} items]"
    return str(val)


# ── pyepics installer ───────────────────────────────────────────────────────────

class _EpicsInstaller(QThread):
    """Installs pyepics via pip in a background thread."""
    done = pyqtSignal(bool, str)   # success, message

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


# ── EPICS CA monitor ────────────────────────────────────────────────────────────

class _EPICSMonitor(QObject):
    """
    Wraps pyepics PV monitors and forwards value-change callbacks to Qt signals.
    Callbacks arrive on a CA background thread; emitting a pyqtSignal queues
    the update safely onto the main-thread event loop.
    """
    value_changed      = pyqtSignal(str, str, object, str)  # dev, sig, value, units
    connection_changed = pyqtSignal(str, str, bool)          # dev, sig, connected
    desc_changed       = pyqtSignal(str, str, str)           # dev, sig, desc

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pvs: dict      = {}   # pvname → epics.PV  (strong refs)
        self._map: dict      = {}   # pvname → (dev_name, sig_name)
        self._desc_pvs: dict = {}   # record.DESC → epics.PV  (one per record base)
        self._desc_map: dict = {}   # record.DESC → [(dev_name, sig_name), ...]
        self._alive: bool    = True  # guards callbacks after Qt C++ deletion

    def setup(self, pv_map: dict):
        """Open CA monitors for every PV in pv_map = {dev: {sig: pvname}}."""
        self.clear()
        self._alive = True   # clear() arms it False; re-arm for new subscriptions
        try:
            import epics
        except ImportError:
            return
        for dev_name, sigs in pv_map.items():
            for sig_name, pvname in sigs.items():
                if not pvname:
                    continue
                self._map[pvname] = (dev_name, sig_name)
                pv = epics.PV(
                    pvname,
                    auto_monitor=True,
                    form='ctrl',             # DBR_CTRL callbacks include units
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
        if not self._alive:
            return
        info = self._map.get(pvname)
        if info:
            try:
                self.value_changed.emit(info[0], info[1], value, units or '')
            except RuntimeError:
                pass

    def _on_connect(self, pvname='', conn=True, **kw):
        if not self._alive:
            return
        info = self._map.get(pvname)
        if info:
            try:
                self.connection_changed.emit(info[0], info[1], bool(conn))
            except RuntimeError:
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

    fetch_pvnames_requested   = pyqtSignal()
    reload_devices_requested  = pyqtSignal()   # full device+plan reload from RE env
    poll_sim_values_requested = pyqtSignal()
    set_sim_device_requested  = pyqtSignal(str, float)
    plan_file_open_requested  = pyqtSignal(str, str)   # (tier, name_or_path)

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
        self._sim_timer: QTimer | None = None
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
        self._conn_settings: dict = {}   # active connection profile
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

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        vlay.addWidget(self._status_lbl)

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
        self.devices_tree.setSortingEnabled(False)
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
        self.devices_tree.setColumnWidth(5, max(self.devices_tree.columnWidth(5), 155))

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
                        if any(v for v in sigs.values())}
        sim_dev_set = set(pv_map) - set(epics_pv_map)

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

        # ── Start polling timer for any polled devices ───────────────────
        if self._sim_device_names:
            self._sim_timer = QTimer(self)
            self._sim_timer.setInterval(2000)
            self._sim_timer.timeout.connect(self._on_sim_poll)
            self._sim_timer.start()

        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("⟳ Reconnect")
        for i in range(5):   # columns 0-4 only
            self.devices_tree.resizeColumnToContents(i)
        # Column 5 (Tweak): resizeColumnToContents ignores setItemWidget sizes,
        # so enforce a minimum wide enough for ◀ / step / ▶.
        self.devices_tree.setColumnWidth(5, max(self.devices_tree.columnWidth(5), 155))

    def on_pv_names_error(self, msg: str):
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

    # ── Internal ────────────────────────────────────────────────────────────────

    def _on_pv_connected(self, dev_name: str, sig_name: str, connected: bool):
        grey = QColor("#888888")
        red  = QColor("#e05050")

        sig_item = self._signal_items.get((dev_name, sig_name))
        if sig_item:
            if not connected:
                sig_item.setText(2, "○ Disconnected")
                sig_item.setForeground(2, red)
                sig_item.setText(3, "")

        if self._primary_signal.get(dev_name) == sig_name:
            dev_item = self._device_items.get(dev_name)
            if dev_item and not connected:
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

        pv_updates, self._pending_pv_updates = self._pending_pv_updates, {}
        for (dev_name, sig_name), (value, units) in pv_updates.items():
            sig_item = self._signal_items.get((dev_name, sig_name))
            if sig_item:
                sig_item.setText(2, _fmt_value(value))
                sig_item.setText(3, units)
                sig_item.setForeground(2, dim)
            if self._primary_signal.get(dev_name) == sig_name:
                dev_item = self._device_items.get(dev_name)
                if dev_item:
                    dev_item.setText(2, _fmt_value(value))
                    dev_item.setText(3, units)
                    dev_item.setForeground(2, green)

        desc_updates, self._pending_desc_updates = self._pending_desc_updates, {}
        for (dev_name, sig_name), desc in desc_updates.items():
            sig_item = self._signal_items.get((dev_name, sig_name))
            if sig_item:
                sig_item.setText(4, desc)
            if self._primary_signal.get(dev_name) == sig_name:
                dev_item = self._device_items.get(dev_name)
                if dev_item:
                    dev_item.setText(4, desc)

    def _save_metadata_cache(self):
        try:
            _METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            _METADATA_PATH.write_text(json.dumps(self._metadata_cache, indent=2))
        except Exception:
            pass

    def _make_tweak_widget(self, dev_name: str, setpoint_pvname: str | None) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(2)

        step = NoScrollDoubleSpinBox()
        step.setRange(0.0001, 100000)
        step.setValue(0.1)
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
        # Store button refs so on_sim_device_set_done can re-enable them.
        self._tweak_buttons.setdefault(dev_name, []).extend([btn_minus, btn_plus])

        h.addWidget(btn_minus)
        h.addWidget(step)
        h.addWidget(btn_plus)
        return w

    def _on_sim_poll(self):
        """Timer callback — requests a fresh device value poll from the worker."""
        self.poll_sim_values_requested.emit()

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

    # ── AD Viewer context menu ───────────────────────────────────────────────────

    def _on_device_context_menu(self, pos):
        item = self.devices_tree.itemAt(pos)
        if item is None:
            return
        dev_name = item.text(0).strip()
        if dev_name not in self._device_items:
            return  # group header or signal sub-row — skip

        pv_map_dev = self._pv_map_cache.get(dev_name, {})
        classname  = self._device_classes.get(dev_name, "")

        from .ad_viewer import is_area_detector
        if not is_area_detector(pv_map_dev, classname):
            return

        menu = QMenu(self)
        act_open   = menu.addAction("📺  Open AD Viewer")
        act_config = menu.addAction("⚙  Configure AD Viewer…")
        action = menu.exec(self.devices_tree.viewport().mapToGlobal(pos))
        if action == act_open:
            self._open_ad_viewer(dev_name, pv_map_dev, force_dialog=False)
        elif action == act_config:
            self._open_ad_viewer(dev_name, pv_map_dev, force_dialog=True)

    def _open_ad_viewer(self, dev_name: str, pv_map_dev: dict,
                        force_dialog: bool = False):
        from .ad_viewer import ADViewerWindow, extract_ad_prefix, _HAS_P4P, load_ad_settings, save_ad_settings

        if not _HAS_P4P:
            QMessageBox.warning(
                self, "p4p not installed",
                "The p4p package is required for PVA image streaming.\n\n"
                "Install it with:\n    pip install p4p",
            )
            return

        # Load saved per-device settings
        ad_settings = load_ad_settings()
        saved       = ad_settings.get(dev_name, {})

        # Auto-detect prefix; fall back to saved value
        auto_prefix = extract_ad_prefix(pv_map_dev)
        prefix      = auto_prefix or saved.get('prefix', '')

        # Host: saved override first, then active profile host
        profile_host = self._conn_settings.get('host', '')
        pva_host     = saved.get('pva_host', '') or profile_host

        # Show config dialog when forced, prefix unknown, or host not yet saved
        if force_dialog or not prefix or not pva_host:
            dlg = _ADConfigDialog(
                dev_name,
                prefix or f"{dev_name}:",
                pva_host,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            prefix   = dlg.prefix
            pva_host = dlg.host

        # Persist prefix/host without overwriting other saved keys (colormap etc.)
        ad_settings.setdefault(dev_name, {}).update({'prefix': prefix, 'pva_host': pva_host})
        save_ad_settings(ad_settings)

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
                                pva_host=pva_host, parent=None)
        self._ad_viewers[dev_name] = viewer
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.destroyed.connect(lambda _, n=dev_name: self._ad_viewers.pop(n, None))
        viewer.show()

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
