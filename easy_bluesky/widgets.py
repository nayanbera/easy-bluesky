"""widgets.py — Reusable Qt widgets: ParamForm and PlanDialog."""

import json
from PyQt6.QtWidgets import (
    QWidget, QDialog, QDialogButtonBox, QFormLayout, QScrollArea,
    QListWidget, QListWidgetItem, QComboBox, QLineEdit, QDoubleSpinBox,
    QSpinBox, QCheckBox, QLabel, QVBoxLayout, QHBoxLayout, QGroupBox,
    QAbstractItemView, QMessageBox, QPushButton,
)
from PyQt6.QtCore import Qt

class ScanArgsWidget(QWidget):
    """Editor for VAR_POSITIONAL scan args: repeating (motor, start, stop) groups."""

    def __init__(self, devices, parent=None):
        super().__init__(parent)
        self.devices = list(devices) if devices else []
        self._rows = []  # list of (motor_combo, start_spin, stop_spin, row_widget)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        outer.addWidget(self._rows_widget)

        btn = QPushButton("+ Add Motor Axis")
        btn.clicked.connect(self._add_row)
        outer.addWidget(btn)

        self._add_row()

    def _add_row(self, motor="", start=0.0, stop=1.0):
        row_w = QWidget()
        row_lay = QHBoxLayout(row_w)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(6)

        motor_combo = QComboBox()
        motor_combo.addItems(["-- motor --"] + self.devices)
        if motor:
            idx = motor_combo.findText(str(motor))
            if idx >= 0:
                motor_combo.setCurrentIndex(idx)

        start_spin = QDoubleSpinBox()
        start_spin.setRange(-1e9, 1e9)
        start_spin.setDecimals(4)
        start_spin.setSingleStep(0.1)
        start_spin.setPrefix("start ")
        start_spin.setValue(start)

        stop_spin = QDoubleSpinBox()
        stop_spin.setRange(-1e9, 1e9)
        stop_spin.setDecimals(4)
        stop_spin.setSingleStep(0.1)
        stop_spin.setPrefix("stop ")
        stop_spin.setValue(stop)

        rm_btn = QPushButton("✕")
        rm_btn.setMaximumWidth(28)
        rm_btn.setToolTip("Remove this axis")

        row_lay.addWidget(motor_combo, 3)
        row_lay.addWidget(start_spin, 2)
        row_lay.addWidget(stop_spin, 2)
        row_lay.addWidget(rm_btn)

        entry = (motor_combo, start_spin, stop_spin, row_w)
        self._rows.append(entry)
        self._rows_layout.addWidget(row_w)

        rm_btn.clicked.connect(lambda: self._remove_row(entry))

    def _remove_row(self, entry):
        if len(self._rows) <= 1:
            return
        mc, ss, es, rw = entry
        self._rows.remove(entry)
        self._rows_layout.removeWidget(rw)
        rw.deleteLater()

    def populate(self, flat_args):
        """Fill from a flat list [motor1, start1, stop1, motor2, ...]."""
        for _, _, _, rw in list(self._rows):
            self._rows_layout.removeWidget(rw)
            rw.deleteLater()
        self._rows.clear()

        triplets = [flat_args[i:i+3] for i in range(0, len(flat_args) - 2, 3)]
        for triplet in triplets:
            motor, start, stop = triplet[0], triplet[1], triplet[2]
            try:
                self._add_row(motor=str(motor), start=float(start), stop=float(stop))
            except (TypeError, ValueError):
                self._add_row()

        if not self._rows:
            self._add_row()

    def get_value(self):
        """Return flat list [motor1, start1, stop1, ...] or None if empty."""
        result = []
        for mc, ss, es, _ in self._rows:
            motor = mc.currentText()
            if motor and motor != "-- motor --":
                result.extend([motor, ss.value(), es.value()])
        return result if result else None


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

    # ── Widget factory helpers ─────────────────────────────────────────────────

    def _make_device_list(self, default=None):
        """Multi-select list for detectors / readable devices."""
        w = QListWidget()
        w.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        w.setMaximumHeight(120)
        for d in self.devices:
            w.addItem(QListWidgetItem(d))
        if default:
            items = default if isinstance(default, list) else [default]
            for i in range(w.count()):
                w.item(i).setSelected(w.item(i).text() in items)
        return w

    def _make_device_combo(self, default=None):
        """Single-select combo for motors / movable devices."""
        w = QComboBox()
        w.addItems(["-- select --"] + self.devices)
        if default:
            idx = w.findText(str(default))
            if idx >= 0:
                w.setCurrentIndex(idx)
        return w

    def _make_widget(self, name, typ, default, param):
        convert = param.get("convert_device_names", False)
        kind    = param.get("kind", {}).get("name", "POSITIONAL_OR_KEYWORD")
        n       = name.lower()

        # ── VAR_POSITIONAL motor args → (motor, start, stop, ...) groups ────────
        if kind == "VAR_POSITIONAL" and ("__MOVABLE__" in typ or n in ("args",)):
            return ScanArgsWidget(self.devices)

        # ── Classify the annotation ──────────────────────────────────────────────
        _list_types     = ("List[", "Sequence[", "list[")
        _readable_types = ("__READABLE__", "Readable", "readable", "Detector")
        _movable_types  = ("__MOVABLE__", "__SETTABLE__", "Movable", "movable",
                           "Motor", "Device", "ophyd")
        is_list_ann     = any(t in typ for t in _list_types)
        is_readable_ann = any(t in typ for t in _readable_types)
        is_movable_ann  = any(t in typ for t in _movable_types)

        # ── List[Movable] / List[Motor] → ScanArgsWidget (motor, start, stop rows)
        _mot_names = ("motors", "movables", "axes", "positioners", "actuators")
        if (is_list_ann and is_movable_ann and not is_readable_ann) or \
           (n in _mot_names and not typ):
            return ScanArgsWidget(self.devices)

        # ── List[Readable] / List[Detector] → multi-select device list ──────────
        is_det_name = n in ("detectors", "dets", "det", "readables", "readable",
                            "detectors_list")
        if is_readable_ann or (convert and is_list_ann) or (is_det_name and not typ):
            return self._make_device_list(default)

        # ── Float / int — check BEFORE device fallbacks to avoid mis-routing ────
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

        # ── Numeric heuristics by name when no annotation ────────────────────────
        if not typ:
            if n in ("num", "num_points", "npts", "steps", "n_points", "num_steps",
                     "num_images", "nframes", "n"):
                w = QSpinBox()
                w.setRange(1, 999999)
                if default not in (None, "None"):
                    try:
                        w.setValue(int(default))
                    except Exception:
                        pass
                return w
            if n in ("start", "stop", "step", "delay", "exposure", "exposure_time",
                     "sleep_time", "wait_time", "count_time", "dwell", "speed",
                     "velocity", "position", "pos", "value", "val"):
                w = QDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(4)
                w.setSingleStep(0.1)
                if default not in (None, "None"):
                    try:
                        w.setValue(float(default))
                    except Exception:
                        pass
                return w

        # ── Single device (motor / movable) ──────────────────────────────────────
        is_mot_name = n in ("motor", "movable", "device", "flyer",
                            "axis", "positioner", "actuator")
        if (is_movable_ann and not is_list_ann) or \
           (convert and not is_list_ann) or \
           (is_mot_name and not typ):
            return self._make_device_combo(default)

        # ── Bool ─────────────────────────────────────────────────────────────────
        if typ == "bool":
            w = QCheckBox()
            if default not in (None, "None"):
                w.setChecked(bool(default))
            return w

        # ── Enum / literal ───────────────────────────────────────────────────────
        if "Literal" in typ:
            import re
            opts = re.findall(r"'([^']+)'", typ)
            w = QComboBox()
            w.addItems(opts)
            return w

        # ── Generic fallback ─────────────────────────────────────────────────────
        w = QLineEdit()
        w.setPlaceholderText(typ or "value")
        if default not in (None, "None"):
            w.setText(str(default))
        return w

    def set_values(self, args, kwargs):
        """Pre-fill form widgets from existing args/kwargs."""
        arg_iter = iter(args)
        for p in self.params:
            name = p["name"]
            if name in ("per_step", "md", "cycler") or name not in self.widgets:
                continue
            kind = p.get("kind", {}).get("name", "POSITIONAL_OR_KEYWORD")
            w = self.widgets[name]
            if kind == "VAR_POSITIONAL":
                remaining = list(arg_iter)  # consume all remaining positional args
                if isinstance(w, ScanArgsWidget) and remaining:
                    w.populate(remaining)
                break  # nothing positional after VAR_POSITIONAL
            elif kind == "KEYWORD_ONLY":
                val = kwargs.get(name)
            else:
                val = next(arg_iter, kwargs.get(name))
            if val is None:
                continue
            self._set_widget(w, val)

    def _set_widget(self, w, val):
        if isinstance(w, QListWidget):
            items = val if isinstance(val, list) else [val]
            for i in range(w.count()):
                w.item(i).setSelected(w.item(i).text() in items)
        elif isinstance(w, QComboBox):
            idx = w.findText(str(val))
            if idx >= 0:
                w.setCurrentIndex(idx)
        elif isinstance(w, QDoubleSpinBox):
            try:
                w.setValue(float(val))
            except (TypeError, ValueError):
                pass
        elif isinstance(w, QSpinBox):
            try:
                w.setValue(int(val))
            except (TypeError, ValueError):
                pass
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(val))
        elif isinstance(w, QLineEdit):
            w.setText(json.dumps(val) if not isinstance(val, str) else val)

    def get_values(self):
        """Return (args, kwargs) for the plan."""
        args   = []
        kwargs = {}
        for p in self.params:
            name = p["name"]
            if name in ("per_step", "md", "cycler"):
                continue
            if name not in self.widgets:
                continue
            w    = self.widgets[name]
            kind = p.get("kind", {}).get("name", "POSITIONAL_OR_KEYWORD")
            if kind == "VAR_POSITIONAL":
                val = w.get_value() if isinstance(w, ScanArgsWidget) else self._read_widget(w, p)
                if val:
                    args.extend(val)
            elif kind == "KEYWORD_ONLY":
                val = self._read_widget(w, p)
                if val is not None:
                    kwargs[name] = val
            else:
                val = self._read_widget(w, p)
                if val is not None:
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
        args   = item.get("args", [])
        kwargs = {k: v for k, v in item.get("kwargs", {}).items() if k != "md"}
        self.param_form.set_values(args, kwargs)
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
