"""widgets.py — Reusable Qt widgets: ParamForm and PlanDialog."""

import json
from PyQt6.QtWidgets import (
    QWidget, QDialog, QDialogButtonBox, QFormLayout, QScrollArea,
    QListWidget, QListWidgetItem, QComboBox, QLineEdit, QDoubleSpinBox,
    QSpinBox, QCheckBox, QLabel, QVBoxLayout, QHBoxLayout, QGroupBox,
    QAbstractItemView, QMessageBox,
)
from PyQt6.QtCore import Qt

class ParamForm(QWidget):
    def __init__(self, params, devices, parent=None):
        super().__init__(parent)
        self.params  = params
        self.devices = list(devices.keys()) if devices else []
        self.widgets = {}
        self._build()

    def _build(self):
        layout = QFormLayout(self)
        layout.setSpacing(10)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        for p in self.params:
            name     = p["name"]
            ann      = p.get("annotation", {})
            typ      = ann.get("type", "") if isinstance(ann, dict) else str(ann)
            default  = p.get("default", None)
            desc     = p.get("description", "")
            optional = default is not None

            # Skip internal params
            if name in ("per_step", "md", "cycler"):
                continue

            label = QLabel(f"{'[opt] ' if optional else ''}{name}")
            label.setToolTip(desc)
            if optional:
                label.setStyleSheet("color: #888;")

            widget = self._make_widget(name, typ, default, p)
            if widget:
                self.widgets[name] = widget
                layout.addRow(label, widget)

    def _make_widget(self, name, typ, default, param):
        convert = param.get("convert_device_names", False)

        # Detector list (multi-select)
        if "__READABLE__" in typ or (convert and "Sequence" in typ):
            w = QListWidget()
            w.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            w.setMaximumHeight(120)
            for d in self.devices:
                item = QListWidgetItem(d)
                w.addItem(item)
            return w

        # Single device (motor etc.)
        if "__MOVABLE__" in typ or ("__SETTABLE__" in typ) or (convert and "Sequence" not in typ):
            w = QComboBox()
            w.addItems(["-- select --"] + self.devices)
            return w

        # Float
        if typ in ("float", "int") or "float" in typ or "int" in typ:
            if "int" in typ and "float" not in typ:
                w = QSpinBox()
                w.setRange(-999999, 999999)
                if default not in (None, "None"):
                    try:
                        w.setValue(int(default))
                    except Exception:
                        pass
            else:
                w = QDoubleSpinBox()
                w.setRange(-999999.0, 999999.0)
                w.setDecimals(4)
                w.setSingleStep(0.1)
                if default not in (None, "None"):
                    try:
                        w.setValue(float(default))
                    except Exception:
                        pass
            return w

        # Bool
        if typ == "bool":
            w = QCheckBox()
            if default not in (None, "None"):
                w.setChecked(bool(default))
            return w

        # Enum / literal
        if "Literal" in typ:
            import re
            opts = re.findall(r"'([^']+)'", typ)
            w = QComboBox()
            w.addItems(opts)
            return w

        # Generic / args
        w = QLineEdit()
        w.setPlaceholderText(typ or "value")
        if default not in (None, "None"):
            w.setText(str(default))
        return w

    def get_values(self):
        """Return (args, kwargs) for the plan."""
        args   = []
        kwargs = {}
        for p in self.params:
            name    = p["name"]
            if name in ("per_step", "md", "cycler"):
                continue
            if name not in self.widgets:
                continue
            w   = self.widgets[name]
            val = self._read_widget(w, p)
            if val is None:
                continue
            kind = p.get("kind", {}).get("name", "POSITIONAL_OR_KEYWORD")
            if kind == "KEYWORD_ONLY":
                kwargs[name] = val
            else:
                args.append(val)
        return args, kwargs

    def _read_widget(self, w, param):
        convert = param.get("convert_device_names", False)
        ann     = param.get("annotation", {})
        typ     = ann.get("type", "") if isinstance(ann, dict) else str(ann)

        if isinstance(w, QListWidget):
            selected = [item.text() for item in w.selectedItems()]
            return selected if selected else None

        if isinstance(w, QComboBox):
            v = w.currentText()
            if v == "-- select --":
                return None
            return v

        if isinstance(w, QDoubleSpinBox):
            return w.value()

        if isinstance(w, QSpinBox):
            return w.value()

        if isinstance(w, QCheckBox):
            return w.isChecked()

        if isinstance(w, QLineEdit):
            v = w.text().strip()
            if not v:
                return None
            try:
                return json.loads(v)
            except Exception:
                return v

        return None

# ══════════════════════════════════════════════════════════════════════════════
#  ADD/EDIT PLAN DIALOG
# ══════════════════════════════════════════════════════════════════════════════
class PlanDialog(QDialog):
    def __init__(self, plans, devices, item=None, parent=None):
        super().__init__(parent)
        self.plans   = plans
        self.devices = devices
        self.item    = item
        self.result_item = None
        self.setWindowTitle("Edit Plan" if item else "Add Plan")
        self.setMinimumSize(500, 500)
        self._build()
        if item:
            self._populate(item)

    def _build(self):
        layout = QVBoxLayout(self)

        # Plan selector
        top = QHBoxLayout()
        top.addWidget(QLabel("Plan:"))
        self.plan_combo = QComboBox()
        self.plan_combo.addItems(sorted(self.plans.keys()))
        self.plan_combo.currentTextChanged.connect(self._on_plan_changed)
        top.addWidget(self.plan_combo, 1)
        layout.addLayout(top)

        # Description
        self.desc_label = QLabel("")
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("color: #888; font-size: 12px; padding: 4px;")
        layout.addWidget(self.desc_label)

        # Scrollable param form
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.form_widget = QWidget()
        self.scroll.setWidget(self.form_widget)
        layout.addWidget(self.scroll, 1)

        # Metadata
        meta_grp = QGroupBox("Metadata (optional)")
        meta_lay = QFormLayout(meta_grp)
        self.meta_edit = QLineEdit()
        self.meta_edit.setPlaceholderText('{"sample": "Si", "comment": "test"}')
        meta_lay.addRow("md (JSON):", self.meta_edit)
        layout.addWidget(meta_grp)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._on_plan_changed(self.plan_combo.currentText())

    def _on_plan_changed(self, name):
        if name not in self.plans:
            return
        info = self.plans[name]
        self.desc_label.setText(info.get("description", ""))
        params = info.get("parameters", [])
        self.param_form = ParamForm(params, self.devices)
        self.form_widget.deleteLater()
        self.form_widget = self.param_form
        self.scroll.setWidget(self.form_widget)

    def _populate(self, item):
        name = item.get("name", "")
        idx  = self.plan_combo.findText(name)
        if idx >= 0:
            self.plan_combo.setCurrentIndex(idx)
        md = item.get("kwargs", {}).get("md", {})
        if md:
            self.meta_edit.setText(json.dumps(md))

    def _on_accept(self):
        plan_name = self.plan_combo.currentText()
        if plan_name not in self.plans:
            QMessageBox.warning(self, "Error", "Select a valid plan")
            return

        args, kwargs = self.param_form.get_values()

        # Parse metadata
        md_text = self.meta_edit.text().strip()
        if md_text:
            try:
                kwargs["md"] = json.loads(md_text)
            except Exception:
                QMessageBox.warning(self, "Invalid JSON", "Metadata must be valid JSON")
                return

        self.result_item = {
            "name":      plan_name,
            "args":      args,
            "kwargs":    kwargs,
            "item_type": "plan",
        }
        if self.item and "item_uid" in self.item:
            self.result_item["item_uid"] = self.item["item_uid"]

        self.accept()
