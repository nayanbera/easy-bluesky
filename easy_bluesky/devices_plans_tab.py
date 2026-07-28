"""devices_plans_tab.py — Devices & Plans browser tab."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton,
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QColor, QFont

from .config import ACCENT


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pvs: dict = {}   # pvname → epics.PV  (strong refs)
        self._map: dict = {}   # pvname → (dev_name, sig_name)

    def setup(self, pv_map: dict):
        """Open CA monitors for every PV in pv_map = {dev: {sig: pvname}}."""
        self.clear()
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
                    callback=self._on_change,
                    connection_callback=self._on_connect,
                )
                self._pvs[pvname] = pv

    def clear(self):
        for pv in self._pvs.values():
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()
        self._map.clear()

    def _on_change(self, pvname='', value=None, units='', **kw):
        info = self._map.get(pvname)
        if info:
            if not units:
                pv = self._pvs.get(pvname)
                try:
                    units = pv.units or ''
                except Exception:
                    units = ''
            self.value_changed.emit(info[0], info[1], value, units)

    def _on_connect(self, pvname='', conn=True, **kw):
        info = self._map.get(pvname)
        if info:
            self.connection_changed.emit(info[0], info[1], bool(conn))


# ── Tab widget ──────────────────────────────────────────────────────────────────

class DevicesPlansTab(QWidget):
    """Two-panel tab: live device tree (left) | plans + details (right)."""

    fetch_pvnames_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plans: dict = {}
        self._device_items: dict  = {}   # dev_name → QTreeWidgetItem
        self._signal_items: dict  = {}   # (dev_name, sig_name) → QTreeWidgetItem
        self._primary_signal: dict = {}  # dev_name → sig_name shown on device row
        self._epics_monitor = _EPICSMonitor(self)
        self._epics_monitor.value_changed.connect(self._on_pv_changed)
        self._epics_monitor.connection_changed.connect(self._on_pv_connected)
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

        self.devices_tree = QTreeWidget()
        self.devices_tree.setHeaderLabels(["Device / Signal", "Kind", "Value", "Units"])
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
        self.devices_tree.clear()
        self._device_items.clear()
        self._signal_items.clear()
        self._primary_signal.clear()

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
                kind = info.get("kind", "")
                child = QTreeWidgetItem([name, kind, "", ""])
                child.setForeground(0, QColor(color))
                child.setForeground(1, QColor("#888"))
                child.setToolTip(0, f"Module: {module}")
                group_item.addChild(child)
                self._device_items[name] = child

            self.devices_tree.addTopLevelItem(group_item)

        self.devices_tree.expandAll()
        for i in range(4):
            self.devices_tree.resizeColumnToContents(i)

    def setup_epics_monitors(self, pv_map: dict):
        """Receive PV name map, create signal sub-rows and open CA monitors."""
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

        dim = QColor("#666666")

        for dev_name, sigs in pv_map.items():
            item = self._device_items.get(dev_name)
            if item is None or not sigs:
                continue

            # Remove old signal children
            while item.childCount() > 0:
                item.removeChild(item.child(0))

            # Choose which signal shows on the device row
            primary = next(
                (s for s in ("user_readback", "readback", dev_name) if s in sigs),
                next(iter(sigs)),
            )
            self._primary_signal[dev_name] = primary

            for sig_name, pvname in sigs.items():
                sig_item = QTreeWidgetItem([f"  {sig_name}", "", "○ Connecting…", ""])
                sig_item.setForeground(0, dim)
                sig_item.setForeground(2, QColor("#aaaaaa"))
                sig_item.setToolTip(0, pvname)
                item.addChild(sig_item)
                self._signal_items[(dev_name, sig_name)] = sig_item

            # Initialise device row too
            if item:
                item.setText(2, "○ Connecting…")
                item.setForeground(2, QColor("#aaaaaa"))

        self._epics_monitor.setup(pv_map)

        total = sum(len(v) for v in pv_map.values())
        self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
        self._status_lbl.setText(f"● Live — monitoring {total} PV(s)")
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("⟳ Reconnect")

        for i in range(4):
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
