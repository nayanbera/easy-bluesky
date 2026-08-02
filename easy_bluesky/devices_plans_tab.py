"""devices_plans_tab.py — Devices & Plans browser tab."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QDoubleSpinBox, QLineEdit,
)
from .widgets import NoScrollDoubleSpinBox
import json
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread, QTimer
from PyQt6.QtGui import QColor, QFont

from .config import ACCENT

_METADATA_PATH = Path.home() / ".easy_bluesky" / "device_metadata.json"


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

    def put_value(self, pvname: str, value):
        try:
            import epics
            epics.caput(pvname, value, wait=False)
        except Exception:
            pass


# ── Tab widget ──────────────────────────────────────────────────────────────────

class DevicesPlansTab(QWidget):
    """Two-panel tab: live device tree (left) | plans + details (right)."""

    fetch_pvnames_requested  = pyqtSignal()
    poll_sim_values_requested = pyqtSignal()   # triggers worker.read_devices_status()
    set_sim_device_requested = pyqtSignal(str, float)  # dev_name, new_value

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plans: dict = {}
        self._device_items: dict  = {}   # dev_name → QTreeWidgetItem
        self._signal_items: dict  = {}   # (dev_name, sig_name) → QTreeWidgetItem
        self._primary_signal: dict = {}  # dev_name → sig_name shown on device row
        self._readback_values: dict = {}  # dev_name → float (current readback)
        self._tweak_pvnames: dict = {}    # dev_name → user_setpoint pvname (EPICS)
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
        vlay.addWidget(self.devices_tree, 1)
        return w

    # ── Plans panel ─────────────────────────────────────────────────────────────

    def _build_plans(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        lbl = QLabel("AVAILABLE PLANS")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

        self.plans_list = QListWidget()
        self.plans_list.setMaximumHeight(200)
        self.plans_list.currentItemChanged.connect(self._on_plan_selected)
        vlay.addWidget(self.plans_list)

        lbl2 = QLabel("PARAMETERS")
        lbl2.setObjectName("section_title")
        vlay.addWidget(lbl2)

        self.plan_detail = QPlainTextEdit()
        self.plan_detail.setReadOnly(True)
        self.plan_detail.setPlaceholderText("Select a plan to view its parameters…")
        vlay.addWidget(self.plan_detail, 1)
        return w

    # ── Public slots ────────────────────────────────────────────────────────────

    def update_devices(self, devices: dict):
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
        self._device_classes.clear()
        self._epics_monitor.clear()

        if not devices:
            self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
            self._status_lbl.setText("● No devices — open the RE environment")
            self._refresh_btn.setEnabled(False)
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
        for i in range(6):
            self.devices_tree.resizeColumnToContents(i)

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
        for i in range(6):
            self.devices_tree.resizeColumnToContents(i)

    def on_pv_names_error(self, msg: str):
        self._status_lbl.setStyleSheet("font-size: 11px; color: #e05050;")
        self._status_lbl.setText(f"⚠ {msg[:120]}")
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("⟳ Reconnect")

    def update_plans(self, plans: dict):
        self._plans = plans
        current = self.plans_list.currentItem()
        current_name = current.text() if current else None

        self.plans_list.clear()
        for name in sorted(plans.keys()):
            self.plans_list.addItem(name)

        if current_name:
            for i in range(self.plans_list.count()):
                if self.plans_list.item(i).text() == current_name:
                    self.plans_list.setCurrentRow(i)
                    break

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
        green = QColor("#2ca02c")
        dim   = QColor("#666666")

        # Update signal sub-row
        sig_item = self._signal_items.get((dev_name, sig_name))
        if sig_item:
            sig_item.setText(2, _fmt_value(value))
            sig_item.setText(3, units)
            sig_item.setForeground(2, dim)

        # Update device row if this is the primary signal
        if self._primary_signal.get(dev_name) == sig_name:
            dev_item = self._device_items.get(dev_name)
            if dev_item:
                dev_item.setText(2, _fmt_value(value))
                dev_item.setText(3, units)
                dev_item.setForeground(2, green)

        # Track numeric readback for tweak calculations
        if sig_name in ("user_readback", "readback") or (
            self._primary_signal.get(dev_name) == sig_name
        ):
            if isinstance(value, (int, float)):
                self._readback_values[dev_name] = float(value)

        # Cache units for sim mode reuse
        if units:
            self._metadata_cache.setdefault(dev_name, {})["units"] = units
            self._metadata_save_timer.start()

    def _on_desc_changed(self, dev_name: str, sig_name: str, desc: str):
        sig_item = self._signal_items.get((dev_name, sig_name))
        if sig_item:
            sig_item.setText(4, desc)
        if self._primary_signal.get(dev_name) == sig_name:
            dev_item = self._device_items.get(dev_name)
            if dev_item:
                dev_item.setText(4, desc)

        # Cache description for sim mode reuse
        if desc:
            self._metadata_cache.setdefault(dev_name, {})["desc"] = desc
            self._metadata_save_timer.start()

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
            cur = self._readback_values.get(dev_name, 0.0)
            new_val = cur + sign * step.value()
            if setpoint_pvname is None:
                self.set_sim_device_requested.emit(dev_name, new_val)
                self._readback_values[dev_name] = new_val  # optimistic update
                # Re-poll soon after set completes so linked signals (e.g.
                # SynSignal with func reading this motor) update promptly.
                QTimer.singleShot(400, self.poll_sim_values_requested.emit)
            else:
                self._epics_monitor.put_value(setpoint_pvname, new_val)

        btn_minus.clicked.connect(lambda: _move(-1))
        btn_plus.clicked.connect(lambda: _move(+1))

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
            if isinstance(val, (int, float)):
                self._readback_values[dev_name] = float(val)

            meta = self._metadata_cache.get(dev_name, {})
            if meta.get("units"):
                item.setText(3, meta["units"])
            if meta.get("desc"):
                item.setText(4, meta["desc"])

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
        self._refresh_btn.setText("Fetching…")
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        self._status_lbl.setText("Fetching PV names from RE environment…")
        self.fetch_pvnames_requested.emit()

    def _on_plan_selected(self, current: QListWidgetItem, _previous):
        if not current:
            self.plan_detail.clear()
            return
        name   = current.text()
        info   = self._plans.get(name, {})
        params = info.get("parameters", [])
        lines  = [f"Plan: {name}", ""]
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
