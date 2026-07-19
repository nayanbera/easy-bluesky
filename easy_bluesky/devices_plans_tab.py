"""devices_plans_tab.py — Devices & Plans browser tab."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPlainTextEdit,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

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
    return "#d4d4d4", "Other"


class DevicesPlansTab(QWidget):
    """Two-panel tab: color-coded device tree (left) | plans + details (right)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plans: dict = {}
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_devices())
        splitter.addWidget(self._build_plans())
        splitter.setSizes([500, 500])
        lay.addWidget(splitter)

    # ── Devices panel ──────────────────────────────────────────────────────────

    def _build_devices(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        lbl = QLabel("AVAILABLE DEVICES")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

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
        self.devices_tree.setHeaderLabels(["Name", "Kind", "Module"])
        self.devices_tree.setRootIsDecorated(False)
        self.devices_tree.setAlternatingRowColors(True)
        self.devices_tree.setSortingEnabled(True)
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
        self.devices_tree.setSortingEnabled(False)
        self.devices_tree.clear()
        for name, info in sorted(devices.items()):
            module   = info.get("module", "")
            kind     = info.get("kind", "")
            color, dev_type = _device_color(module)
            item = QTreeWidgetItem([name, kind, module])
            item.setForeground(0, QColor(color))
            item.setForeground(1, QColor(color))
            item.setForeground(2, QColor("#555"))
            item.setToolTip(0, f"Type: {dev_type}\nModule: {module}")
            self.devices_tree.addTopLevelItem(item)
        self.devices_tree.resizeColumnToContents(0)
        self.devices_tree.resizeColumnToContents(1)
        self.devices_tree.setSortingEnabled(True)
        self.devices_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)

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
