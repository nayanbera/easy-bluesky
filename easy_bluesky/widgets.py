"""widgets.py — Reusable Qt widgets: ParamForm and PlanDialog."""

import csv
import json
from PyQt6.QtWidgets import (
    QWidget, QDialog, QDialogButtonBox, QFormLayout, QScrollArea,
    QListWidget, QListWidgetItem, QComboBox, QLineEdit, QDoubleSpinBox,
    QSpinBox, QCheckBox, QLabel, QVBoxLayout, QHBoxLayout, QGroupBox,
    QAbstractItemView, QMessageBox, QPushButton, QFileDialog,
)
from PyQt6.QtCore import Qt


class MultiSelectWidget(QWidget):
    """
    Multi-select QListWidget with a live summary label below showing
    which items are currently selected.  Drop-in replacement for
    the raw QListWidget used for detectors and similar device lists.
    """

    def __init__(self, items: list, max_height: int = 120, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._list = QListWidget()
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection)
        self._list.setMaximumHeight(max_height)
        for item in items:
            self._list.addItem(QListWidgetItem(item))
        self._list.itemSelectionChanged.connect(self._update_summary)
        lay.addWidget(self._list)

        self._summary = QLabel("None selected")
        self._summary.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)

    def _update_summary(self):
        selected = [self._list.item(i).text()
                    for i in range(self._list.count())
                    if self._list.item(i).isSelected()]
        if selected:
            self._summary.setText("✓  " + ",   ".join(selected))
            self._summary.setStyleSheet(
                "color: #2ca02c; font-size: 11px; font-weight: bold; padding: 1px 2px;")
        else:
            self._summary.setText("None selected")
            self._summary.setStyleSheet(
                "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")

    # ── proxy methods so callers that used QListWidget directly still work ──────

    def count(self) -> int:
        return self._list.count()

    def item(self, i: int):
        return self._list.item(i)

    def selectedItems(self) -> list:
        return self._list.selectedItems()

    def itemSelectionChanged(self):
        return self._list.itemSelectionChanged


class PositionListWidget(QWidget):
    """
    Widget for entering a List[float] parameter (e.g. list_scan positions).
    Accepts comma-separated values directly, or generates a linspace.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("e.g.  1.0, 2.5, 3.0, 5.5  (comma-separated)")
        lay.addWidget(self._edit)

        gen_row = QHBoxLayout()
        gen_row.setSpacing(4)
        gen_row.addWidget(QLabel("linspace:"))
        self._ls_start = QDoubleSpinBox()
        self._ls_start.setRange(-1e9, 1e9)
        self._ls_start.setDecimals(4)
        self._ls_stop = QDoubleSpinBox()
        self._ls_stop.setRange(-1e9, 1e9)
        self._ls_stop.setDecimals(4)
        self._ls_stop.setValue(1.0)
        self._ls_num = QSpinBox()
        self._ls_num.setRange(2, 100000)
        self._ls_num.setValue(11)
        btn_gen = QPushButton("Fill")
        btn_gen.setFixedWidth(44)
        btn_gen.setToolTip("Generate evenly spaced positions")
        btn_gen.clicked.connect(self._generate)
        gen_row.addWidget(self._ls_start, 2)
        gen_row.addWidget(QLabel("→"))
        gen_row.addWidget(self._ls_stop, 2)
        gen_row.addWidget(QLabel("×"))
        gen_row.addWidget(self._ls_num, 1)
        gen_row.addWidget(btn_gen)
        lay.addLayout(gen_row)

    def _generate(self):
        start = self._ls_start.value()
        stop  = self._ls_stop.value()
        n     = self._ls_num.value()
        if n < 2:
            return
        step = (stop - start) / (n - 1)
        pts  = [start + i * step for i in range(n)]
        self._edit.setText(", ".join(f"{p:.6g}" for p in pts))

    def get_value(self):
        text = self._edit.text().strip()
        if not text:
            return None
        try:
            return [float(x.strip()) for x in text.split(",") if x.strip()]
        except ValueError:
            return None

    def populate(self, val):
        if isinstance(val, (list, tuple)):
            self._edit.setText(", ".join(str(v) for v in val))
        elif val is not None:
            self._edit.setText(str(val))

class ScanArgsWidget(QWidget):
    """
    Motor selector for scan plans.
    Top: multi-select list of available motors.
    Bottom: start/stop fields appear for each selected motor.
    Outputs flat list [motor1, start1, stop1, motor2, start2, stop2, ...].
    """

    def __init__(self, devices, parent=None):
        super().__init__(parent)
        self.devices = list(devices) if devices else []
        self._spinboxes = {}   # motor_name -> (start_spin, stop_spin)
        self._row_widgets = {} # motor_name -> row QWidget
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lay.addWidget(QLabel("Select motors:"))

        self._motor_list = QListWidget()
        self._motor_list.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection)
        self._motor_list.setMaximumHeight(100)
        for d in self.devices:
            self._motor_list.addItem(QListWidgetItem(d))
        self._motor_list.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._motor_list)

        self._motor_summary = QLabel("None selected")
        self._motor_summary.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")
        self._motor_summary.setWordWrap(True)
        lay.addWidget(self._motor_summary)

        # Container for start/stop rows
        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 2, 0, 0)
        self._rows_layout.setSpacing(2)
        lay.addWidget(self._rows_container)

        # Pre-build one hidden row per device so spinboxes are stable objects
        for d in self.devices:
            start_spin = QDoubleSpinBox()
            start_spin.setRange(-1e9, 1e9)
            start_spin.setDecimals(4)
            start_spin.setSingleStep(0.1)

            stop_spin = QDoubleSpinBox()
            stop_spin.setRange(-1e9, 1e9)
            stop_spin.setDecimals(4)
            stop_spin.setSingleStep(0.1)
            stop_spin.setValue(1.0)

            self._spinboxes[d] = (start_spin, stop_spin)

            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(4)
            lbl = QLabel(f"{d}:")
            lbl.setMinimumWidth(80)
            row_lay.addWidget(lbl)
            row_lay.addWidget(QLabel("start"))
            row_lay.addWidget(start_spin, 2)
            row_lay.addWidget(QLabel("stop"))
            row_lay.addWidget(stop_spin, 2)

            self._row_widgets[d] = row
            self._rows_layout.addWidget(row)
            row.hide()

    def _on_selection_changed(self):
        selected = {self._motor_list.item(i).text()
                    for i in range(self._motor_list.count())
                    if self._motor_list.item(i).isSelected()}
        for motor, row in self._row_widgets.items():
            row.setVisible(motor in selected)
        if selected:
            self._motor_summary.setText("✓  " + ",   ".join(sorted(selected)))
            self._motor_summary.setStyleSheet(
                "color: #2ca02c; font-size: 11px; font-weight: bold; padding: 1px 2px;")
        else:
            self._motor_summary.setText("None selected")
            self._motor_summary.setStyleSheet(
                "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")

    def populate(self, flat_args):
        """Pre-fill from [motor1, start1, stop1, motor2, ...]."""
        triplets = [flat_args[i:i + 3] for i in range(0, len(flat_args) - 2, 3)]
        for triplet in triplets:
            motor, start, stop = str(triplet[0]), triplet[1], triplet[2]
            for i in range(self._motor_list.count()):
                if self._motor_list.item(i).text() == motor:
                    self._motor_list.item(i).setSelected(True)
            if motor in self._spinboxes:
                s, e = self._spinboxes[motor]
                try:
                    s.setValue(float(start))
                    e.setValue(float(stop))
                except (TypeError, ValueError):
                    pass

    def get_value(self):
        """Return [motor1, start1, stop1, ...] in device-list order, or None."""
        result = []
        for d in self.devices:
            if self._row_widgets.get(d, None) and self._row_widgets[d].isVisible():
                s, e = self._spinboxes[d]
                result.extend([d, s.value(), e.value()])
        return result if result else None


class ListScanArgsWidget(QWidget):
    """
    Widget for list_scan-style plans: alternating (motor, [positions]) pairs.

    Each selected motor gets a line-edit for comma-separated positions.
    A CSV import button lets users load multi-column position tables where
    each column header matches a motor name.

    Output: [motor1, [p1, p2, ...], motor2, [p1, p2, ...], ...]
    """

    def __init__(self, devices, parent=None):
        super().__init__(parent)
        self.devices      = list(devices) if devices else []
        self._edits       = {}        # motor_name → QLineEdit
        self._row_widgets = {}        # motor_name → row QWidget
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lay.addWidget(QLabel("Select motors:"))

        self._motor_list = QListWidget()
        self._motor_list.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection)
        self._motor_list.setMaximumHeight(90)
        for d in self.devices:
            self._motor_list.addItem(QListWidgetItem(d))
        self._motor_list.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._motor_list)

        self._motor_summary = QLabel("None selected")
        self._motor_summary.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")
        self._motor_summary.setWordWrap(True)
        lay.addWidget(self._motor_summary)

        btn_csv = QPushButton("Load from CSV…")
        btn_csv.setToolTip(
            "CSV with a header row of motor names.\n"
            "Each column = positions for that motor.")
        btn_csv.clicked.connect(self._load_csv)
        lay.addWidget(btn_csv)

        self._rows_container = QWidget()
        self._rows_layout    = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 2, 0, 0)
        self._rows_layout.setSpacing(2)
        lay.addWidget(self._rows_container)

        for d in self.devices:
            edit = QLineEdit()
            edit.setPlaceholderText("1.0, 2.5, 3.0  (comma-separated)")
            self._edits[d] = edit

            row = QWidget()
            rl  = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            lbl = QLabel(f"{d}:")
            lbl.setMinimumWidth(80)
            rl.addWidget(lbl)
            rl.addWidget(edit, 1)

            self._row_widgets[d] = row
            self._rows_layout.addWidget(row)
            row.hide()

    def _on_selection_changed(self):
        selected = {self._motor_list.item(i).text()
                    for i in range(self._motor_list.count())
                    if self._motor_list.item(i).isSelected()}
        for motor, row in self._row_widgets.items():
            row.setVisible(motor in selected)
        if selected:
            self._motor_summary.setText("✓  " + ",   ".join(sorted(selected)))
            self._motor_summary.setStyleSheet(
                "color: #2ca02c; font-size: 11px; font-weight: bold; padding: 1px 2px;")
        else:
            self._motor_summary.setText("None selected")
            self._motor_summary.setStyleSheet(
                "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")

    def _load_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Positions from CSV", "",
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                cols: dict = {}
                for row in reader:
                    for key, val in row.items():
                        cols.setdefault(key.strip(), []).append(val.strip())

            matched = False
            for col_name, values in cols.items():
                for d in self.devices:
                    if d == col_name or col_name in d or d in col_name:
                        for i in range(self._motor_list.count()):
                            if self._motor_list.item(i).text() == d:
                                self._motor_list.item(i).setSelected(True)
                        self._edits[d].setText(", ".join(v for v in values if v))
                        matched = True
                        break

            if not matched:
                QMessageBox.warning(
                    self, "CSV Import",
                    "No column headers matched any known motor name.\n"
                    f"CSV columns: {list(cols.keys())}\n"
                    f"Available motors: {self.devices}")
        except Exception as e:
            QMessageBox.critical(self, "CSV Error", str(e))

    def populate(self, flat_args):
        """Pre-fill from [motor1, [p1, p2, ...], motor2, [...], ...]."""
        i = 0
        while i + 1 < len(flat_args):
            motor     = str(flat_args[i])
            positions = flat_args[i + 1]
            i += 2
            for j in range(self._motor_list.count()):
                if self._motor_list.item(j).text() == motor:
                    self._motor_list.item(j).setSelected(True)
            if motor in self._edits:
                if isinstance(positions, (list, tuple)):
                    self._edits[motor].setText(
                        ", ".join(str(p) for p in positions))
                else:
                    self._edits[motor].setText(str(positions))

    def get_value(self):
        """Return [motor1, [p1, p2, ...], ...] in selection order, or None."""
        result = []
        for d in self.devices:
            row = self._row_widgets.get(d)
            if not (row and row.isVisible()):
                continue
            text = self._edits[d].text().strip()
            if not text:
                continue
            try:
                positions = [float(x.strip()) for x in text.split(",") if x.strip()]
                if positions:
                    result.extend([d, positions])
            except ValueError:
                pass
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
        w = MultiSelectWidget(self.devices)
        if default:
            items = default if isinstance(default, list) else [default]
            for i in range(w.count()):
                if w.item(i).text() in items:
                    w.item(i).setSelected(True)
            w._update_summary()
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

        # ── VAR_POSITIONAL motor args ─────────────────────────────────────────────
        # list_scan: annotation is tuple[Movable, list[...]] → contains "list["
        # scan:      annotation is Movable | Any             → no "list["
        if kind == "VAR_POSITIONAL" and ("__MOVABLE__" in typ or n in ("args",)):
            if "list[" in typ.lower():
                return ListScanArgsWidget(self.devices)
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

        # ── List[float] / List[int] → position list widget ──────────────────────
        # Must come BEFORE the float/int check because "float" in "List[float]".
        if is_list_ann and not is_readable_ann and not is_movable_ann:
            if "float" in typ or "int" in typ:
                return PositionListWidget()

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
                if isinstance(w, (ScanArgsWidget, ListScanArgsWidget)) and remaining:
                    w.populate(remaining)
                continue  # arg_iter exhausted; KEYWORD_ONLY params may still follow
            elif kind == "KEYWORD_ONLY":
                val = kwargs.get(name)
            else:
                val = next(arg_iter, kwargs.get(name))
            if val is None:
                continue
            self._set_widget(w, val)

    def _set_widget(self, w, val):
        if isinstance(w, PositionListWidget):
            w.populate(val)
            return
        if isinstance(w, MultiSelectWidget):
            items = val if isinstance(val, list) else [val]
            for i in range(w.count()):
                w.item(i).setSelected(w.item(i).text() in items)
            w._update_summary()
            return
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
                val = w.get_value() if isinstance(w, (ScanArgsWidget, ListScanArgsWidget)) else self._read_widget(w, p)
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

        if isinstance(w, PositionListWidget):
            return w.get_value()

        if isinstance(w, (MultiSelectWidget, QListWidget)):
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
