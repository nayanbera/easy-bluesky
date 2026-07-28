"""devices_plans_tab.py — Devices & Plans browser tab."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QCheckBox, QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont

from .config import ACCENT


def _device_color(module: str) -> tuple:
    """Return (fg_color, type_label) based on module path."""
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


class DevicesPlansTab(QWidget):
    """Two-panel tab: color-coded device tree with live readings (left) | plans + details (right)."""

    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plans: dict = {}
        self._readings: dict = {}
        self._device_items: dict = {}   # name → QTreeWidgetItem for fast update
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self.refresh_requested)
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_devices())
        splitter.addWidget(self._build_plans())
        splitter.setSizes([600, 400])
        lay.addWidget(splitter)

    # ── Devices panel ──────────────────────────────────────────────────────────

    def _build_devices(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        # Header row: title + refresh controls
        hdr = QHBoxLayout()
        lbl = QLabel("AVAILABLE DEVICES")
        lbl.setObjectName("section_title")
        hdr.addWidget(lbl)
        hdr.addStretch()

        self._auto_cb = QCheckBox("Auto")
        self._auto_cb.setToolTip("Auto-refresh device readings")
        self._auto_cb.toggled.connect(self._on_auto_toggled)
        hdr.addWidget(self._auto_cb)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(2, 60)
        self._interval_spin.setValue(5)
        self._interval_spin.setSuffix(" s")
        self._interval_spin.setFixedWidth(60)
        self._interval_spin.setToolTip("Auto-refresh interval")
        self._interval_spin.valueChanged.connect(self._on_interval_changed)
        hdr.addWidget(self._interval_spin)

        self._refresh_btn = QPushButton("⟳ Refresh")
        self._refresh_btn.setFixedWidth(80)
        self._refresh_btn.setToolTip("Read current values from all devices")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        hdr.addWidget(self._refresh_btn)

        vlay.addLayout(hdr)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        vlay.addWidget(self._status_lbl)

        # Legend row
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

        self.devices_tree = QTreeWidget()
        self.devices_tree.setHeaderLabels(["Device / Signal", "Kind", "Value", "Units"])
        self.devices_tree.setRootIsDecorated(True)
        self.devices_tree.setAlternatingRowColors(True)
        self.devices_tree.setSortingEnabled(False)
        vlay.addWidget(self.devices_tree, 1)
        return w

    # ── Plans panel ────────────────────────────────────────────────────────────

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

    # ── Public update slots ────────────────────────────────────────────────────

    def update_devices(self, devices: dict):
        self.devices_tree.clear()
        self._device_items.clear()

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

        # Re-apply any readings already in hand
        if self._readings:
            self._apply_readings(self._readings)

    def update_readings(self, readings: dict):
        self._readings = readings
        self._apply_readings(readings)
        n = len(readings)
        self._status_lbl.setStyleSheet("font-size: 11px; color: #2ca02c;")
        self._status_lbl.setText(f"✓ {n} device(s) read")
        self._refresh_done()

    def on_read_error(self, msg: str):
        self._status_lbl.setStyleSheet("font-size: 11px; color: #e05050;")
        self._status_lbl.setText(f"⚠ {msg[:120]}")
        self._refresh_done()

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

    # ── Internal ───────────────────────────────────────────────────────────────

    def _apply_readings(self, readings: dict):
        grey  = QColor("#888888")
        red   = QColor("#e05050")
        green = QColor("#2ca02c")
        dim   = QColor("#666666")

        for dev_name, info in readings.items():
            item = self._device_items.get(dev_name)
            if item is None:
                continue

            # Remove old signal children
            while item.childCount() > 0:
                item.removeChild(item.child(0))

            connected = info.get('connected', False)
            error     = info.get('error')
            reading   = info.get('reading', {})

            if error:
                item.setText(2, f"⚠ {error[:50]}")
                item.setForeground(2, red)
                item.setText(3, "")
            elif not connected:
                item.setText(2, "○ Disconnected")
                item.setForeground(2, grey)
                item.setText(3, "")
            else:
                # Pick the primary value to show on the device row
                primary_key = None
                for candidate in (
                    dev_name,
                    f"{dev_name}_user_readback",
                    f"{dev_name}_readback",
                ):
                    if candidate in reading:
                        primary_key = candidate
                        break
                if primary_key is None and reading:
                    primary_key = next(iter(reading))

                if primary_key:
                    val   = _fmt_value(reading[primary_key]['value'])
                    units = reading[primary_key]['units']
                    item.setText(2, val)
                    item.setText(3, units)
                    item.setForeground(2, green)
                else:
                    item.setText(2, "—")
                    item.setText(3, "")

                # Signal sub-rows
                for sig_name, sig_data in reading.items():
                    short = sig_name[len(dev_name) + 1:] if sig_name.startswith(dev_name + '_') else sig_name
                    sig_item = QTreeWidgetItem([
                        f"  {short}",
                        "",
                        _fmt_value(sig_data['value']),
                        str(sig_data['units']),
                    ])
                    sig_item.setForeground(0, dim)
                    sig_item.setForeground(2, dim)
                    item.addChild(sig_item)

        for i in range(4):
            self.devices_tree.resizeColumnToContents(i)

    def _on_refresh_clicked(self):
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Reading…")
        self._status_lbl.setStyleSheet("font-size: 11px; color: #888;")
        self._status_lbl.setText("Reading device values…")
        self.refresh_requested.emit()
        QTimer.singleShot(20_000, self._refresh_done)  # fallback re-enable

    def _refresh_done(self):
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("⟳ Refresh")

    def _on_auto_toggled(self, checked: bool):
        if checked:
            self._auto_timer.start(self._interval_spin.value() * 1000)
            self._on_refresh_clicked()
        else:
            self._auto_timer.stop()

    def _on_interval_changed(self, value: int):
        if self._auto_timer.isActive():
            self._auto_timer.setInterval(value * 1000)

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
