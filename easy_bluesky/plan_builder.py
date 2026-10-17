"""plan_builder.py — Plan Composer: visual flowchart builder + code editor."""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from qtpy.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QPlainTextEdit, QComboBox, QLineEdit, QMessageBox,
    QFormLayout, QDoubleSpinBox, QSpinBox, QFrame, QScrollArea, QTabWidget,
    QFileDialog, QCheckBox, QInputDialog, QMenu, QDialog, QTextBrowser,
    QSizePolicy, QStackedWidget, QToolButton,
)
from qtpy.QtCore import Qt, QTimer, Signal, QMimeData, QThread, QPoint, QByteArray
from qtpy.QtGui import QFont, QColor, QDrag, QCursor

from .highlighter import PythonHighlighter
from .code_editor import CodeEditor
from .widgets import NoScrollSpinBox, NoScrollDoubleSpinBox, ParamForm


# ── FlowNode data model ────────────────────────────────────────────────────────

KIND_MOVE          = "move"
KIND_RMOVE         = "rmove"
KIND_MOVE_MULTI    = "move_multi"
KIND_SLEEP         = "sleep"
KIND_SET_ATTR      = "set_attr"
KIND_OPEN_SHUTTER  = "open_shutter"
KIND_CLOSE_SHUTTER = "close_shutter"
KIND_TRIGGER_READ  = "trigger_read"
KIND_STAGE         = "stage"
KIND_UNSTAGE       = "unstage"
KIND_PLAN          = "plan"
KIND_CUSTOM_PY     = "custom_python"
KIND_REPEAT        = "repeat"
KIND_FOR_EACH      = "for_each"
KIND_WHILE         = "while_loop"
KIND_IF_ELSE       = "if_else"
KIND_STATS         = "stats"

CONTAINER_KINDS = {KIND_REPEAT, KIND_FOR_EACH, KIND_WHILE, KIND_IF_ELSE, KIND_STATS}

_MIME = "application/x-ebflow-node"

# Module-level drag state (in-process D&D only)
_drag_state: dict = {}


@dataclass
class FlowNode:
    kind: str
    params: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    children_else: list = field(default_factory=list)
    per_step: list = field(default_factory=list)
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "params": dict(self.params),
            "node_id": self.node_id,
            "children": [c.to_dict() for c in self.children],
            "children_else": [c.to_dict() for c in self.children_else],
            "per_step": [c.to_dict() for c in self.per_step],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FlowNode":
        n = cls(
            kind=d["kind"],
            params=dict(d.get("params", {})),
            node_id=d.get("node_id", uuid.uuid4().hex[:8]),
        )
        n.children = [cls.from_dict(c) for c in d.get("children", [])]
        n.children_else = [cls.from_dict(c) for c in d.get("children_else", [])]
        n.per_step = [cls.from_dict(c) for c in d.get("per_step", [])]
        return n


# ── Code generator ─────────────────────────────────────────────────────────────

def _gen_list(nodes: list, depth: int) -> str:
    if not nodes:
        return "    " * depth + "pass"
    return "\n".join(_gen_node(n, depth) for n in nodes)


def _split_csv(text: str) -> list:
    """Split 'a, b, c' into ['a', 'b', 'c'], dropping blanks."""
    return [t.strip() for t in str(text).split(",") if t.strip()]


def _ident(text: str, default: str) -> str:
    """Return *text* as a valid Python identifier (or *default*)."""
    t = re.sub(r"\W", "_", str(text or "").strip())
    if not t or t[0].isdigit():
        return default
    return t


def _uses_kind(nodes: list, kind: str) -> bool:
    return any(n.kind == kind
               or _uses_kind(n.children, kind)
               or _uses_kind(n.children_else, kind)
               or _uses_kind(n.per_step, kind)
               for n in nodes)


def _gen_node(node: "FlowNode", depth: int = 1) -> str:
    pad = "    " * depth
    k = node.kind
    p = node.params

    if k == KIND_MOVE:
        dev = p.get("device", "motor") or "motor"
        pos = p.get("position", 0.0)
        return f"{pad}yield from bps.mv({dev}, {pos})"

    if k == KIND_MOVE_MULTI:
        motors = _split_csv(p.get("motors", ""))
        positions = _split_csv(p.get("positions", ""))
        if not motors:
            return f"{pad}pass"
        positions += ["0.0"] * (len(motors) - len(positions))
        args = ", ".join(f"{m}, {v}" for m, v in zip(motors, positions))
        return f"{pad}yield from bps.mv({args})"

    if k == KIND_RMOVE:
        dev = p.get("device", "motor") or "motor"
        delta = p.get("delta", 0.0)
        return f"{pad}yield from bps.mvr({dev}, {delta})"

    if k == KIND_SLEEP:
        secs = p.get("seconds", 1.0)
        return f"{pad}yield from bps.sleep({secs})"

    if k == KIND_SET_ATTR:
        dev = p.get("device", "device") or "device"
        attr = p.get("attribute", "attr") or "attr"
        val = p.get("value", "0") or "0"
        return f"{pad}yield from bps.mv({dev}.{attr}, {val})"

    if k == KIND_OPEN_SHUTTER:
        dev = p.get("device", "shutter") or "shutter"
        return f"{pad}yield from bps.mv({dev}, 'open')"

    if k == KIND_CLOSE_SHUTTER:
        dev = p.get("device", "shutter") or "shutter"
        return f"{pad}yield from bps.mv({dev}, 'closed')"

    if k == KIND_TRIGGER_READ:
        dets = p.get("detectors", "detectors") or "detectors"
        return f"{pad}yield from bps.trigger_and_read({dets})"

    if k == KIND_STAGE:
        dev = p.get("device", "device") or "device"
        return f"{pad}yield from bps.stage({dev})"

    if k == KIND_UNSTAGE:
        dev = p.get("device", "device") or "device"
        return f"{pad}yield from bps.unstage({dev})"

    if k == KIND_CUSTOM_PY:
        code = p.get("code", "pass") or "pass"
        lines = [pad + ln if ln.strip() else "" for ln in code.splitlines()]
        return "\n".join(lines) if lines else f"{pad}pass"

    if k == KIND_PLAN:
        plan_name = p.get("plan_name", "unknown_plan") or "unknown_plan"
        if node.per_step:
            fn = f"_per_step_{node.node_id}"
            ps_lines = [
                f"{pad}def {fn}(detectors, step, pos_cache):",
                f"{pad}    yield from bps.move_per_step(step, pos_cache)",
            ]
            for ps_node in node.per_step:
                ps_lines.append(_gen_node(ps_node, depth + 1))
            call = f"{pad}yield from {plan_name}(per_step={fn})"
            return "\n".join(ps_lines) + "\n" + call
        return f"{pad}yield from {plan_name}()"

    if k == KIND_REPEAT:
        n = p.get("n", 1)
        body = _gen_list(node.children, depth + 1)
        return f"{pad}for _i in range({n}):\n{body}"

    if k == KIND_FOR_EACH:
        varname = p.get("varname", "_val") or "_val"
        iter_type = p.get("iter_type", "list")
        motor = p.get("motor", "")
        if iter_type == "range":
            start = p.get("start", 0)
            stop = p.get("stop", 1)
            num = p.get("num", 10)
            iterable = f"np.linspace({start}, {stop}, {num})"
        elif iter_type == "csv":
            fpath = p.get("csv_file", "data.csv")
            cols = _split_csv(p.get("csv_col", "0")) or ["0"]
            try:
                idxs = [int(c) for c in cols]
            except ValueError:
                idxs = None
            if idxs is not None and len(idxs) == 1:
                iterable = f"np.loadtxt(r'{fpath}', delimiter=',', usecols={idxs})"
            elif idxs is not None:
                iterable = f"np.loadtxt(r'{fpath}', delimiter=',', usecols={idxs}, ndmin=2)"
            else:
                # named columns (header row) -> rows of (col1, col2, ...)
                iterable = (f"zip(*[np.genfromtxt(r'{fpath}', delimiter=',', names=True)[c] "
                            f"for c in {cols}])")
        else:
            vals = p.get("values", "")
            iterable = f"[{vals}]"
        lines = [f"{pad}for {varname} in {iterable}:"]
        if motor:
            motors = _split_csv(motor)
            names = _split_csv(varname)
            if len(motors) > 1 and len(names) == len(motors):
                args = ", ".join(f"{m}, {v}" for m, v in zip(motors, names))
                lines.append(f"{pad}    yield from bps.mv({args})")
            else:
                lines.append(f"{pad}    yield from bps.mv({motors[0]}, {varname})")
        lines.append(_gen_list(node.children, depth + 1))
        return "\n".join(lines)

    if k == KIND_STATS:
        x = p.get("x", "") or "x"
        y = p.get("y", "") or "y"
        pre = _ident(p.get("prefix", "stat"), "stat")
        ps = f"_ps_{node.node_id}"
        fn = f"_stats_{node.node_id}"
        body = _gen_list(node.children, depth + 1) if node.children else \
            f"{pad}    yield from bps.null()"
        lines = [
            f"{pad}{ps} = PeakStats({x!r}, {y!r})",
            f"{pad}@bpp.subs_decorator({ps})",
            f"{pad}def {fn}():",
            body,
            f"{pad}yield from {fn}()",
            f"{pad}{pre}_cen = {ps}.cen",
            f"{pad}{pre}_com = {ps}.com",
            f"{pad}{pre}_max, {pre}_max_y = {ps}.max if {ps}.max else (None, None)",
            f"{pad}{pre}_min, {pre}_min_y = {ps}.min if {ps}.min else (None, None)",
            f"{pad}{pre}_fwhm = getattr({ps}, 'fwhm', None)",
            f"{pad}print(f'[stats] {y}: cen={{{pre}_cen}} com={{{pre}_com}} "
            f"max={{{pre}_max}} min={{{pre}_min}} fwhm={{{pre}_fwhm}}')",
        ]
        move_to = p.get("move_to", "none")
        motor = (p.get("motor", "") or "").strip()
        if move_to in ("cen", "com", "max", "min") and motor:
            lines += [
                f"{pad}if {pre}_{move_to} is not None:",
                f"{pad}    yield from bps.mv({motor}, {pre}_{move_to})",
                f"{pad}else:",
                f"{pad}    print('[stats] {move_to} not found - {motor} not moved')",
            ]
        return "\n".join(lines)

    if k == KIND_WHILE:
        cond = p.get("condition", "True") or "True"
        body = _gen_list(node.children, depth + 1)
        return f"{pad}while {cond}:\n{body}"

    if k == KIND_IF_ELSE:
        cond = p.get("condition", "True") or "True"
        true_body = _gen_list(node.children, depth + 1)
        result = f"{pad}if {cond}:\n{true_body}"
        if node.children_else:
            else_body = _gen_list(node.children_else, depth + 1)
            result += f"\n{pad}else:\n{else_body}"
        return result

    return f"{pad}pass  # unknown: {k}"


def generate_code(nodes: list, plan_name: str = "my_plan",
                  params: list = None) -> str:
    """Generate a complete Python plan function from a list of FlowNodes.

    params: list of dicts with keys 'name', 'default', 'annotation'
    """
    body = _gen_list(nodes, depth=1) if nodes else "    pass"
    return assemble_plan_code(body, plan_name, params, _uses_kind(nodes, KIND_STATS))


def assemble_plan_code(body: str, plan_name: str = "my_plan",
                       params: list = None, stats_imports: bool = False) -> str:
    """Wrap an already-indented plan *body* in imports, signature and docstring."""
    if not plan_name or not plan_name.replace("_", "").isalnum():
        plan_name = "my_plan"

    if params:
        parts = []
        for p in params:
            n = p.get("name", "").strip()
            if not n:
                continue
            ann = p.get("annotation", "").strip()
            dflt = p.get("default", "").strip()
            tok = f"{n}: {ann}" if ann else n
            if dflt:
                tok += f" = {dflt}"
            parts.append(tok)
        parts.append("md=None")
        # Wrap long signatures: one param per line if > 80 chars total
        indent = " " * (len(f"def {plan_name}("))
        one_line = ", ".join(parts)
        if len(one_line) + len(f"def {plan_name}(") > 88:
            sig = f",\n{indent}".join(parts)
        else:
            sig = one_line
    else:
        sig = "md=None"

    extra = (
        "import bluesky.preprocessors as bpp\n"
        "from bluesky.callbacks.fitting import PeakStats\n"
        if stats_imports else ""
    )
    return (
        "import numpy as np\n"
        "import bluesky.plan_stubs as bps\n"
        "import bluesky.plans as bp\n"
        f"{extra}\n\n"
        f"def {plan_name}({sig}):\n"
        '    """\n'
        "    Plan composed with EasyBluesky Visual Composer.\n"
        '    """\n'
        f"{body}\n"
    )


# ── _ConditionWidget ───────────────────────────────────────────────────────────

class _ConditionWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        # Simple mode
        self._simple_w = QWidget()
        s_lay = QHBoxLayout(self._simple_w)
        s_lay.setContentsMargins(0, 0, 0, 0)
        s_lay.setSpacing(2)
        self._lhs = QLineEdit()
        self._lhs.setPlaceholderText("signal")
        self._lhs.setFixedWidth(110)
        self._lhs.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")
        self._op = QComboBox()
        self._op.addItems([">", ">=", "<", "<=", "==", "!="])
        self._op.setFixedWidth(52)
        self._op.setStyleSheet("background: rgba(0,0,0,0.4); color: white;")
        self._rhs = QLineEdit()
        self._rhs.setPlaceholderText("value")
        self._rhs.setFixedWidth(70)
        self._rhs.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")
        s_lay.addWidget(self._lhs)
        s_lay.addWidget(self._op)
        s_lay.addWidget(self._rhs)

        # Advanced mode
        self._adv_w = QLineEdit()
        self._adv_w.setPlaceholderText("Python expression…")
        self._adv_w.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")

        self._stack = QStackedWidget()
        self._stack.addWidget(self._simple_w)
        self._stack.addWidget(self._adv_w)

        self._adv_btn = QPushButton("Adv →")
        self._adv_btn.setFixedWidth(55)
        self._adv_btn.setCheckable(True)
        self._adv_btn.setStyleSheet("color: white; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.3);")
        self._adv_btn.toggled.connect(self._toggle_mode)

        lay.addWidget(self._stack)
        lay.addWidget(self._adv_btn)

        self._lhs.textChanged.connect(self.changed)
        self._op.currentIndexChanged.connect(self.changed)
        self._rhs.textChanged.connect(self.changed)
        self._adv_w.textChanged.connect(self.changed)

    def _toggle_mode(self, adv: bool):
        if adv:
            self._adv_w.setText(self.get_condition())
            self._stack.setCurrentIndex(1)
            self._adv_btn.setText("← Sim")
        else:
            self._stack.setCurrentIndex(0)
            self._adv_btn.setText("Adv →")
        self.changed.emit()

    def get_condition(self) -> str:
        if self._adv_btn.isChecked():
            return self._adv_w.text() or "True"
        lhs = self._lhs.text() or "True"
        op = self._op.currentText()
        rhs = self._rhs.text() or "0"
        if lhs == "True":
            return "True"
        return f"{lhs} {op} {rhs}"

    def set_condition(self, cond: str):
        for op in [">=", "<=", "!=", "==", ">", "<"]:
            if op in cond:
                parts = cond.split(op, 1)
                if len(parts) == 2:
                    self._lhs.setText(parts[0].strip())
                    self._op.setCurrentText(op)
                    self._rhs.setText(parts[1].strip())
                    self._stack.setCurrentIndex(0)
                    self._adv_btn.setChecked(False)
                    return
        self._adv_w.setText(cond)
        self._adv_btn.setChecked(True)


# ── _IterableWidget ────────────────────────────────────────────────────────────

class _IterableWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("<font color='white'>var:</font>"))
        self._varname = QLineEdit("_val")
        self._varname.setFixedWidth(80)
        self._varname.setToolTip("Loop variable. Use '_sx, _sy' to unpack paired values (tuples / multi-column CSV).")
        self._varname.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")
        top.addWidget(self._varname)
        top.addWidget(QLabel("<font color='white'>in:</font>"))
        self._type = QComboBox()
        self._type.addItems(["List", "Range", "CSV"])
        self._type.setFixedWidth(60)
        self._type.setStyleSheet("background: rgba(0,0,0,0.4); color: white;")
        top.addWidget(self._type)
        top.addStretch()
        lay.addLayout(top)

        self._stack = QStackedWidget()

        list_w = QWidget()
        ll = QHBoxLayout(list_w)
        ll.setContentsMargins(0, 0, 0, 0)
        self._list_vals = QLineEdit()
        self._list_vals.setPlaceholderText("0.5, 1.0, 2.5   or pairs: (0, 0), (5, -2.5)")
        self._list_vals.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")
        ll.addWidget(self._list_vals)

        range_w = QWidget()
        rl = QHBoxLayout(range_w)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        self._r_start = QDoubleSpinBox()
        self._r_start.setRange(-1e9, 1e9)
        self._r_start.setFixedWidth(65)
        self._r_start.setStyleSheet("background: rgba(0,0,0,0.4); color: white;")
        self._r_stop = QDoubleSpinBox()
        self._r_stop.setRange(-1e9, 1e9)
        self._r_stop.setValue(1)
        self._r_stop.setFixedWidth(65)
        self._r_stop.setStyleSheet("background: rgba(0,0,0,0.4); color: white;")
        self._r_num = QSpinBox()
        self._r_num.setRange(2, 100000)
        self._r_num.setValue(10)
        self._r_num.setFixedWidth(55)
        self._r_num.setStyleSheet("background: rgba(0,0,0,0.4); color: white;")
        for lbl, w in [("start:", self._r_start), ("stop:", self._r_stop), ("N:", self._r_num)]:
            rl.addWidget(QLabel(f"<font color='white'>{lbl}</font>"))
            rl.addWidget(w)

        csv_w = QWidget()
        cl = QHBoxLayout(csv_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        self._csv_file = QLineEdit()
        self._csv_file.setPlaceholderText("data.csv")
        self._csv_file.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")
        csv_browse = QPushButton("…")
        csv_browse.setFixedWidth(24)
        csv_browse.clicked.connect(self._browse_csv)
        self._csv_col = QLineEdit("0")
        self._csv_col.setFixedWidth(60)
        self._csv_col.setToolTip("Column index or header name. Use '0, 1' for several columns\n(pairs with var '_sx, _sy').")
        self._csv_col.setStyleSheet("background: rgba(0,0,0,0.4); color: white; border: 1px solid rgba(255,255,255,0.3);")
        cl.addWidget(self._csv_file)
        cl.addWidget(csv_browse)
        cl.addWidget(QLabel("<font color='white'>col:</font>"))
        cl.addWidget(self._csv_col)

        self._stack.addWidget(list_w)
        self._stack.addWidget(range_w)
        self._stack.addWidget(csv_w)
        lay.addWidget(self._stack)

        motor_row = QHBoxLayout()
        motor_row.setContentsMargins(0, 0, 0, 0)
        motor_lbl = QLabel("<font color='white'>→ motor (opt):</font>")
        motor_lbl.setToolTip("Optional: move this motor to the loop variable before each iteration")
        motor_row.addWidget(motor_lbl)
        self._motor = QComboBox()
        self._motor.setEditable(True)
        self._motor.setFixedWidth(160)
        self._motor.addItem("")
        self._motor.setToolTip("Motor to move to the iterable value before each loop body.\nLeave blank to iterate without moving a motor.\nSeveral motors (comma-separated, e.g. 'sx, sy') are moved together;\nthe loop variable must then list the same number of names ('_sx, _sy').")
        self._motor.setStyleSheet("background: rgba(0,0,0,0.4); color: white;")
        motor_row.addWidget(self._motor)
        motor_row.addStretch()
        lay.addLayout(motor_row)

        self._type.currentIndexChanged.connect(self._stack.setCurrentIndex)
        self._type.currentIndexChanged.connect(self.changed)
        self._varname.textChanged.connect(self.changed)
        self._list_vals.textChanged.connect(self.changed)
        self._r_start.valueChanged.connect(self.changed)
        self._r_stop.valueChanged.connect(self.changed)
        self._r_num.valueChanged.connect(self.changed)
        self._csv_file.textChanged.connect(self.changed)
        self._csv_col.textChanged.connect(self.changed)
        self._motor.currentTextChanged.connect(self.changed)

    def _browse_csv(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select CSV", "", "CSV (*.csv *.txt *.dat)")
        if f:
            self._csv_file.setText(f)

    def set_motors(self, motor_names: list):
        cur = self._motor.currentText()
        self._motor.clear()
        self._motor.addItem("")
        for m in motor_names:
            self._motor.addItem(m)
        idx = self._motor.findText(cur)
        if idx >= 0:
            self._motor.setCurrentIndex(idx)
        else:
            self._motor.setCurrentText(cur)

    def get_params(self) -> dict:
        t = ["list", "range", "csv"][self._type.currentIndex()]
        p = {"varname": self._varname.text() or "_val", "iter_type": t,
             "motor": self._motor.currentText()}
        if t == "list":
            p["values"] = self._list_vals.text()
        elif t == "range":
            p["start"] = self._r_start.value()
            p["stop"] = self._r_stop.value()
            p["num"] = self._r_num.value()
        else:
            p["csv_file"] = self._csv_file.text()
            p["csv_col"] = self._csv_col.text()
        return p

    def set_params(self, p: dict):
        t = p.get("iter_type", "list")
        idx = ["list", "range", "csv"].index(t) if t in ["list", "range", "csv"] else 0
        self._type.setCurrentIndex(idx)
        self._varname.setText(p.get("varname", "_val"))
        self._list_vals.setText(p.get("values", ""))
        self._r_start.setValue(float(p.get("start", 0)))
        self._r_stop.setValue(float(p.get("stop", 1)))
        self._r_num.setValue(int(p.get("num", 10)))
        self._csv_file.setText(p.get("csv_file", ""))
        self._csv_col.setText(p.get("csv_col", "0"))
        motor = p.get("motor", "")
        midx = self._motor.findText(motor)
        if midx >= 0:
            self._motor.setCurrentIndex(midx)
        else:
            self._motor.setCurrentText(motor)


# ── BlockWidget ────────────────────────────────────────────────────────────────

_KIND_COLORS = {
    KIND_MOVE: "#2d5f7a", KIND_RMOVE: "#2d5f7a", KIND_MOVE_MULTI: "#2d5f7a",
    KIND_SLEEP: "#4a4a6a",
    KIND_SET_ATTR: "#5a3a6a", KIND_STAGE: "#5a3a6a", KIND_UNSTAGE: "#5a3a6a",
    KIND_OPEN_SHUTTER: "#4a6a3a", KIND_CLOSE_SHUTTER: "#4a6a3a",
    KIND_TRIGGER_READ: "#3a6a5a",
    KIND_PLAN: "#3a5a2a",
    KIND_CUSTOM_PY: "#6a5a3a",
    KIND_REPEAT: "#7a3a2a",
    KIND_FOR_EACH: "#7a4a2a",
    KIND_WHILE: "#6a2a5a",
    KIND_IF_ELSE: "#4a2a7a",
    KIND_STATS: "#2a6a6a",
}

_KIND_ICONS = {
    KIND_MOVE: "→", KIND_RMOVE: "↔", KIND_MOVE_MULTI: "⇉", KIND_SLEEP: "⏱",
    KIND_SET_ATTR: "⚙", KIND_STAGE: "▲", KIND_UNSTAGE: "▼",
    KIND_OPEN_SHUTTER: "⊙", KIND_CLOSE_SHUTTER: "⊗",
    KIND_TRIGGER_READ: "◎", KIND_PLAN: "▶", KIND_CUSTOM_PY: "⌨",
    KIND_REPEAT: "↻", KIND_FOR_EACH: "∀", KIND_WHILE: "∞", KIND_IF_ELSE: "?", KIND_STATS: "Σ",
}

_KIND_LABELS = {
    KIND_MOVE: "Move", KIND_RMOVE: "Relative Move", KIND_MOVE_MULTI: "Move Multiple",
    KIND_SLEEP: "Sleep",
    KIND_SET_ATTR: "Set Attr", KIND_STAGE: "Stage", KIND_UNSTAGE: "Unstage",
    KIND_OPEN_SHUTTER: "Open Shutter", KIND_CLOSE_SHUTTER: "Close Shutter",
    KIND_TRIGGER_READ: "Trigger & Read",
    KIND_CUSTOM_PY: "Custom Python",
    KIND_REPEAT: "Repeat", KIND_FOR_EACH: "For Each",
    KIND_WHILE: "While", KIND_IF_ELSE: "If / Else", KIND_STATS: "Statistics",
}

_FIELD_STYLE = "background: rgba(0,0,0,0.35); color: white; border: 1px solid rgba(255,255,255,0.25);"


class BlockWidget(QWidget):
    node_changed = Signal()
    delete_requested = Signal(object)
    drag_started = Signal(object)

    def __init__(self, node: "FlowNode", devices: dict = None, parent=None):
        super().__init__(parent)
        self.node = node
        self._devices = devices or {}
        self._expanded = True
        self._drag_pos = None
        self._build()

    def _build(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 2, 0, 2)
        root_lay.setSpacing(0)

        color = _KIND_COLORS.get(self.node.kind, "#444")
        header = QWidget()
        header.setStyleSheet(
            f"background: {color}; border-radius: 4px;"
        )
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(4, 3, 4, 3)
        h_lay.setSpacing(4)

        # Drag handle
        handle = QLabel("⠿")
        handle.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        handle.setFixedWidth(14)
        handle.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 13px;")
        handle.mousePressEvent = self._handle_press
        handle.mouseMoveEvent = self._handle_move
        h_lay.addWidget(handle)

        icon = _KIND_ICONS.get(self.node.kind, "●")
        kind_lbl = self.node.params.get("plan_name", "") if self.node.kind == KIND_PLAN else _KIND_LABELS.get(self.node.kind, self.node.kind)
        lbl = QLabel(f"<b>{icon}  {kind_lbl}</b>")
        lbl.setStyleSheet("color: white; font-size: 12px;")
        lbl.setMinimumWidth(10)
        h_lay.addWidget(lbl)
        h_lay.addSpacing(6)

        # Inline params
        self._build_inline(h_lay)

        h_lay.addStretch()

        # Expand button for containers and plan blocks
        is_container = self.node.kind in CONTAINER_KINDS or self.node.kind == KIND_PLAN
        if is_container:
            self._exp_btn = QToolButton()
            self._exp_btn.setText("▾")
            self._exp_btn.setFixedSize(18, 18)
            self._exp_btn.setStyleSheet("color: white; border: none; background: transparent;")
            self._exp_btn.clicked.connect(self._toggle_expand)
            h_lay.addWidget(self._exp_btn)

        del_btn = QToolButton()
        del_btn.setText("×")
        del_btn.setFixedSize(18, 18)
        del_btn.setStyleSheet("color: rgba(255,255,255,0.7); border: none; background: transparent; font-size: 14px;")
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self))
        h_lay.addWidget(del_btn)

        root_lay.addWidget(header)

        # Body for containers and plan blocks
        if is_container:
            self._body = QWidget()
            b_lay = QVBoxLayout(self._body)
            b_lay.setContentsMargins(16, 2, 0, 2)
            b_lay.setSpacing(2)

            if self.node.kind == KIND_IF_ELSE:
                true_lbl = QLabel("▶ True branch")
                true_lbl.setStyleSheet("color: #88ff88; font-size: 10px; font-weight: bold;")
                b_lay.addWidget(true_lbl)

            self._children_area = _ChildrenArea(self.node.children, self._devices)
            self._children_area.changed.connect(self.node_changed)
            self._children_area.drag_started.connect(self.drag_started)
            b_lay.addWidget(self._children_area)

            if self.node.kind == KIND_IF_ELSE:
                else_lbl = QLabel("▶ False branch")
                else_lbl.setStyleSheet("color: #ff8888; font-size: 10px; font-weight: bold;")
                b_lay.addWidget(else_lbl)
                self._else_area = _ChildrenArea(self.node.children_else, self._devices)
                self._else_area.changed.connect(self.node_changed)
                self._else_area.drag_started.connect(self.drag_started)
                b_lay.addWidget(self._else_area)

            if self.node.kind == KIND_PLAN:
                self._ps_toggle = QPushButton("+ Add Per-step Actions")
                self._ps_toggle.setCheckable(True)
                self._ps_toggle.setChecked(bool(self.node.per_step))
                self._ps_toggle.setStyleSheet(
                    "color: #aaaaff; border: 1px solid #aaaaff; background: transparent; font-size: 10px; padding: 1px 4px;")
                self._ps_toggle.toggled.connect(self._on_ps_toggled)
                b_lay.addWidget(self._ps_toggle)

                self._ps_area = _ChildrenArea(self.node.per_step, self._devices)
                self._ps_area.changed.connect(self.node_changed)
                self._ps_area.drag_started.connect(self.drag_started)
                self._ps_area.setVisible(bool(self.node.per_step))
                b_lay.addWidget(self._ps_area)

            root_lay.addWidget(self._body)

    def _on_ps_toggled(self, checked: bool):
        if hasattr(self, "_ps_area"):
            self._ps_area.setVisible(checked)
            if not checked:
                self.node.per_step.clear()
                self.node_changed.emit()

    def _build_inline(self, lay: QHBoxLayout):
        k = self.node.kind
        p = self.node.params

        motors = sorted(n for n, d in self._devices.items()
                        if isinstance(d, dict) and d.get("is_movable"))
        any_devs = sorted(self._devices.keys())

        def combo(opts, default="", w=120):
            c = QComboBox()
            c.setEditable(True)
            c.setFixedWidth(w)
            c.setStyleSheet(_FIELD_STYLE)
            c.addItems(opts)
            idx = c.findText(default)
            if idx >= 0:
                c.setCurrentIndex(idx)
            else:
                c.setCurrentText(default)
            return c

        def spin(val=0.0, mn=-1e9, mx=1e9, w=75):
            s = QDoubleSpinBox()
            s.setRange(mn, mx); s.setValue(val); s.setFixedWidth(w)
            s.setDecimals(4)
            s.setStyleSheet(_FIELD_STYLE)
            return s

        def ispin(val=1, mn=1, mx=100000, w=60):
            s = QSpinBox()
            s.setRange(mn, mx); s.setValue(val); s.setFixedWidth(w)
            s.setStyleSheet(_FIELD_STYLE)
            return s

        def edit(text="", placeholder="", w=100):
            e = QLineEdit(text)
            e.setPlaceholderText(placeholder)
            e.setFixedWidth(w)
            e.setStyleSheet(_FIELD_STYLE)
            return e

        def wlbl(t):
            l = QLabel(f"<font color='white'>{t}</font>")
            return l

        if k == KIND_MOVE:
            c = combo(motors, p.get("device", ""), 130)
            c.currentTextChanged.connect(lambda v: p.update({"device": v}) or self.node_changed.emit())
            lay.addWidget(c)
            lay.addWidget(wlbl("→"))
            s = spin(p.get("position", 0.0))
            s.valueChanged.connect(lambda v: p.update({"position": v}) or self.node_changed.emit())
            lay.addWidget(s)

        elif k == KIND_MOVE_MULTI:
            e1 = edit(p.get("motors", ""), "sx, sy", 110)
            e1.setToolTip("Comma-separated motors, e.g. sx, sy")
            e1.textChanged.connect(lambda v: p.update({"motors": v}) or self.node_changed.emit())
            lay.addWidget(e1)
            lay.addWidget(wlbl("→"))
            e2 = edit(p.get("positions", ""), "0.0, 5.0", 110)
            e2.setToolTip("Comma-separated positions, one per motor (same order).\n"
                          "Expressions and variables (e.g. _sx) are allowed.\n"
                          "All motors are moved simultaneously: bps.mv(m1, p1, m2, p2)")
            e2.textChanged.connect(lambda v: p.update({"positions": v}) or self.node_changed.emit())
            lay.addWidget(e2)

        elif k == KIND_RMOVE:
            c = combo(motors, p.get("device", ""), 130)
            c.currentTextChanged.connect(lambda v: p.update({"device": v}) or self.node_changed.emit())
            lay.addWidget(c)
            lay.addWidget(wlbl("±"))
            s = spin(p.get("delta", 0.0))
            s.valueChanged.connect(lambda v: p.update({"delta": v}) or self.node_changed.emit())
            lay.addWidget(s)

        elif k == KIND_SLEEP:
            s = spin(p.get("seconds", 1.0), 0, 86400, 75)
            s.valueChanged.connect(lambda v: p.update({"seconds": v}) or self.node_changed.emit())
            lay.addWidget(s)
            lay.addWidget(wlbl("s"))

        elif k == KIND_SET_ATTR:
            c = combo(any_devs, p.get("device", ""), 120)
            c.currentTextChanged.connect(lambda v: p.update({"device": v}) or self.node_changed.emit())
            lay.addWidget(c)
            lay.addWidget(wlbl("."))
            e1 = edit(p.get("attribute", ""), "attr", 90)
            e1.textChanged.connect(lambda v: p.update({"attribute": v}) or self.node_changed.emit())
            lay.addWidget(e1)
            lay.addWidget(wlbl("="))
            e2 = edit(p.get("value", ""), "value", 70)
            e2.textChanged.connect(lambda v: p.update({"value": v}) or self.node_changed.emit())
            lay.addWidget(e2)

        elif k in (KIND_OPEN_SHUTTER, KIND_CLOSE_SHUTTER, KIND_STAGE, KIND_UNSTAGE):
            c = combo(any_devs, p.get("device", ""), 130)
            c.currentTextChanged.connect(lambda v: p.update({"device": v}) or self.node_changed.emit())
            lay.addWidget(c)

        elif k == KIND_TRIGGER_READ:
            e = edit(p.get("detectors", ""), "detectors", 140)
            e.textChanged.connect(lambda v: p.update({"detectors": v}) or self.node_changed.emit())
            lay.addWidget(e)

        elif k == KIND_PLAN:
            pass  # plan name shown in header label already

        elif k == KIND_CUSTOM_PY:
            # Show first non-blank line of code as a hint
            code = p.get("code", "")
            first_line = next((ln.strip() for ln in code.splitlines() if ln.strip()), "")
            if len(first_line) > 40:
                first_line = first_line[:38] + "…"
            if first_line:
                hint = QLabel(f"<font color='rgba(255,255,255,0.6)' style='font-size:10px'>{first_line}</font>")
                hint.setMaximumWidth(220)
                lay.addWidget(hint)
            btn = QPushButton("Edit Code…")
            btn.setStyleSheet("color: white; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.3); padding: 2px 6px;")
            btn.clicked.connect(self._edit_custom_py)
            lay.addWidget(btn)

        elif k == KIND_REPEAT:
            lay.addWidget(wlbl("N:"))
            s = ispin(p.get("n", 3))
            s.valueChanged.connect(lambda v: p.update({"n": v}) or self.node_changed.emit())
            lay.addWidget(s)
            lay.addWidget(wlbl("times"))

        elif k == KIND_FOR_EACH:
            self._iter_w = _IterableWidget()
            self._iter_w.set_params(p)
            self._iter_w.set_motors(motors)
            def _on_iter():
                p.clear(); p.update(self._iter_w.get_params())
                self.node_changed.emit()
            self._iter_w.changed.connect(_on_iter)
            lay.addWidget(self._iter_w)

        elif k == KIND_STATS:
            lay.addWidget(wlbl("x:"))
            ex = edit(p.get("x", ""), "fipi2_setpoint", 110)
            ex.setToolTip("Event-data field of the scanned axis (as in the viewer's X list)")
            ex.textChanged.connect(lambda v: p.update({"x": v}) or self.node_changed.emit())
            lay.addWidget(ex)
            lay.addWidget(wlbl("y:"))
            ey = edit(p.get("y", ""), "sbpmTot", 90)
            ey.setToolTip("Detector / signal field to analyse")
            ey.textChanged.connect(lambda v: p.update({"y": v}) or self.node_changed.emit())
            lay.addWidget(ey)
            lay.addWidget(wlbl("vars:"))
            ep = edit(p.get("prefix", "stat"), "stat", 60)
            ep.setToolTip("Variable prefix. Creates <prefix>_cen, _com, _max, _max_y,\n"
                          "_min, _min_y, _fwhm (None when not computable) for later blocks.")
            ep.textChanged.connect(lambda v: p.update({"prefix": v}) or self.node_changed.emit())
            lay.addWidget(ep)
            lay.addWidget(wlbl("move to:"))
            cm = QComboBox()
            cm.addItems(["none", "cen", "com", "max", "min"])
            cm.setCurrentText(p.get("move_to", "none"))
            cm.setFixedWidth(65)
            cm.setStyleSheet(_FIELD_STYLE)
            cm.setToolTip("After the enclosed blocks finish, move the motor to this statistic.")
            cm.currentTextChanged.connect(lambda v: p.update({"move_to": v}) or self.node_changed.emit())
            lay.addWidget(cm)
            cmot = combo(motors, p.get("motor", ""), 100)
            cmot.setToolTip("Motor to move (only used when 'move to' is not none)")
            cmot.currentTextChanged.connect(lambda v: p.update({"motor": v}) or self.node_changed.emit())
            lay.addWidget(cmot)

        elif k in (KIND_WHILE, KIND_IF_ELSE):
            self._cond_w = _ConditionWidget()
            self._cond_w.set_condition(p.get("condition", "True"))
            def _on_cond():
                p["condition"] = self._cond_w.get_condition()
                self.node_changed.emit()
            self._cond_w.changed.connect(_on_cond)
            lay.addWidget(self._cond_w)

    def _edit_custom_py(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Custom Python Block")
        dlg.resize(520, 320)
        lay = QVBoxLayout(dlg)
        editor = QPlainTextEdit()
        editor.setFont(QFont("Courier New", 12))
        editor.setPlainText(self.node.params.get("code", "pass"))
        PythonHighlighter(editor.document())
        lay.addWidget(editor)
        brow = QHBoxLayout()
        ok_btn = QPushButton("OK"); cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(dlg.accept); cancel_btn.clicked.connect(dlg.reject)
        brow.addStretch(); brow.addWidget(ok_btn); brow.addWidget(cancel_btn)
        lay.addLayout(brow)
        if dlg.exec():
            self.node.params["code"] = editor.toPlainText()
            self.node_changed.emit()

    def _toggle_expand(self):
        self._expanded = not self._expanded
        if hasattr(self, "_body"):
            self._body.setVisible(self._expanded)
        if hasattr(self, "_exp_btn"):
            self._exp_btn.setText("▾" if self._expanded else "▸")

    def _handle_press(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = ev.position().toPoint()

    def _handle_move(self, ev):
        if self._drag_pos is None:
            return
        if (ev.position().toPoint() - self._drag_pos).manhattanLength() < 6:
            return
        self._drag_pos = None
        self.drag_started.emit(self)

    def set_devices(self, devices: dict):
        self._devices = devices

    def get_node(self) -> "FlowNode":
        return self.node


# ── _ChildrenArea ──────────────────────────────────────────────────────────────

class _ChildrenArea(QWidget):
    changed = Signal()
    drag_started = Signal(object)

    _DROP_STRIP_STYLE = "background: transparent;"
    _DROP_STRIP_HOVER = "background: #4a9eff; border-radius: 1px;"

    def __init__(self, node_list: list, devices: dict = None, parent=None):
        super().__init__(parent)
        self._nodes = node_list
        self._devices = devices or {}
        self._block_widgets: list = []
        self._drop_strips: list = []
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self.setAcceptDrops(True)
        self._rebuild()

    def _rebuild(self):
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        self._block_widgets = []
        self._drop_strips = []

        def make_strip():
            strip = QFrame()
            strip.setFixedHeight(4)
            strip.setStyleSheet(self._DROP_STRIP_STYLE)
            strip.setObjectName("drop_strip")
            return strip

        strip0 = make_strip()
        self._drop_strips.append(strip0)
        self._lay.addWidget(strip0)

        for node in self._nodes:
            bw = BlockWidget(node, self._devices)
            bw.node_changed.connect(self.changed)
            bw.delete_requested.connect(self._on_delete)
            bw.drag_started.connect(self.drag_started)
            self._block_widgets.append(bw)
            self._lay.addWidget(bw)

            strip = make_strip()
            self._drop_strips.append(strip)
            self._lay.addWidget(strip)

        if not self._nodes:
            empty = QLabel("  ← drop here")
            empty.setStyleSheet("color: palette(mid); font-size: 10px; padding: 2px;")
            empty.setObjectName("empty_hint")
            self._lay.addWidget(empty)

        self._lay.addStretch()
        self.updateGeometry()

    def _on_delete(self, bw: BlockWidget):
        node = bw.get_node()
        if node in self._nodes:
            self._nodes.remove(node)
        self._rebuild()
        self.changed.emit()

    def insert_node(self, node: "FlowNode", index: int = -1):
        if index < 0 or index > len(self._nodes):
            self._nodes.append(node)
        else:
            self._nodes.insert(index, node)
        self._rebuild()
        self.changed.emit()

    def remove_node(self, node: "FlowNode"):
        if node in self._nodes:
            self._nodes.remove(node)
            self._rebuild()
            self.changed.emit()

    def _drop_index(self, pos: QPoint) -> int:
        y = pos.y()
        for i, bw in enumerate(self._block_widgets):
            bw_top = bw.mapTo(self, QPoint(0, 0)).y()
            bw_mid = bw_top + bw.height() // 2
            if y < bw_mid:
                return i
        return len(self._nodes)

    def _highlight_strips(self, pos: QPoint):
        y = pos.y()
        best_i = 0
        best_dist = float("inf")
        for i, strip in enumerate(self._drop_strips):
            sy = strip.mapTo(self, QPoint(0, strip.height() // 2)).y()
            dist = abs(y - sy)
            if dist < best_dist:
                best_dist = dist
                best_i = i
        for i, strip in enumerate(self._drop_strips):
            strip.setStyleSheet(
                self._DROP_STRIP_HOVER if i == best_i else self._DROP_STRIP_STYLE)

    def _clear_strips(self):
        for strip in self._drop_strips:
            strip.setStyleSheet(self._DROP_STRIP_STYLE)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(_MIME):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasFormat(_MIME):
            ev.acceptProposedAction()
            self._highlight_strips(ev.position().toPoint())

    def dragLeaveEvent(self, ev):
        self._clear_strips()

    def dropEvent(self, ev):
        self._clear_strips()
        if not ev.mimeData().hasFormat(_MIME):
            return
        raw = bytes(ev.mimeData().data(_MIME)).decode()
        try:
            d = json.loads(raw)
        except Exception:
            return

        node_id = d.get("node_id")
        src_area = _drag_state.get("source_area")
        src_node = _drag_state.get("node")

        if src_node and src_area:
            if src_node in src_area._nodes:
                src_area._nodes.remove(src_node)
                src_area._rebuild()
                src_area.changed.emit()
            node = src_node
        else:
            node = FlowNode.from_dict(d)

        idx = self._drop_index(ev.position().toPoint())
        self._nodes.insert(idx, node)
        self._rebuild()
        self.changed.emit()
        ev.acceptProposedAction()

    def set_devices(self, devices: dict):
        self._devices = devices
        for bw in self._block_widgets:
            bw.set_devices(devices)


# ── FlowCanvas ─────────────────────────────────────────────────────────────────

class FlowCanvas(QScrollArea):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes: list = []
        self._devices: dict = {}

        self._root_area = _ChildrenArea(self._nodes, self._devices)
        self._root_area.changed.connect(self.changed)
        self._root_area.drag_started.connect(self._on_drag_started)

        self.setWidget(self._root_area)
        self.setWidgetResizable(True)
        self.setAcceptDrops(True)
        self.setStyleSheet("QScrollArea { border: 1px solid palette(mid); }")

    def _on_drag_started(self, bw: BlockWidget):
        node = bw.get_node()
        src = self._find_children_area(bw)
        _drag_state["node"] = node
        _drag_state["source_area"] = src

        mime = QMimeData()
        mime.setData(_MIME, QByteArray(json.dumps(node.to_dict()).encode()))
        drag = QDrag(bw)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)
        _drag_state.clear()
        self.changed.emit()

    def _find_children_area(self, widget) -> "_ChildrenArea | None":
        p = widget.parent()
        while p:
            if isinstance(p, _ChildrenArea):
                return p
            p = p.parent()
        return None

    def add_node(self, node: "FlowNode"):
        self._nodes.append(node)
        self._root_area._rebuild()
        self.changed.emit()

    def clear(self):
        self._nodes.clear()
        self._root_area._rebuild()
        self.changed.emit()

    def get_nodes(self) -> list:
        return self._nodes

    def set_nodes(self, nodes: list):
        self._nodes.clear()
        self._nodes.extend(nodes)
        self._root_area._rebuild()
        self.changed.emit()

    def set_devices(self, devices: dict):
        self._devices = devices
        self._root_area.set_devices(devices)

    def to_json(self) -> str:
        return json.dumps([n.to_dict() for n in self._nodes], indent=2)

    def from_json(self, s: str):
        try:
            data = json.loads(s)
            nodes = [FlowNode.from_dict(d) for d in data]
        except Exception as e:
            QMessageBox.warning(None, "Load Error", f"Cannot parse .ebplan file:\n{e}")
            return
        self.set_nodes(nodes)

    def keyPressEvent(self, ev):
        super().keyPressEvent(ev)


# ── PalettePanel ───────────────────────────────────────────────────────────────

class PalettePanel(QWidget):
    block_activated = Signal(str)
    plan_activated = Signal(str)

    _STRUCTURAL = [
        (KIND_MOVE,          "→  Move",
         "Move a motor to an absolute position.\nyield from bps.mv(motor, position)"),
        (KIND_RMOVE,         "↔  Relative Move",
         "Move a motor by a relative delta from its current position.\nyield from bps.mvr(motor, delta)"),
        (KIND_MOVE_MULTI,    "⇉  Move Multiple",
         "Move several motors simultaneously (e.g. sx, sy as a pair).\nyield from bps.mv(m1, p1, m2, p2)"),
        (KIND_SLEEP,         "⏱  Sleep",
         "Wait a fixed number of seconds.\nyield from bps.sleep(seconds)"),
        (KIND_SET_ATTR,      "⚙  Set Attribute",
         "Set a device attribute (e.g. cam.acquire_time) to a value.\nyield from bps.mv(device.attribute, value)"),
        (KIND_STAGE,         "▲  Stage Device",
         "Stage a device — arms it for acquisition (opens file, enables triggers).\nyield from bps.stage(device)"),
        (KIND_UNSTAGE,       "▼  Unstage Device",
         "Unstage a device — closes files and disarms triggers.\nyield from bps.unstage(device)"),
        (KIND_OPEN_SHUTTER,  "⊙  Open Shutter",
         "Open a shutter (set to 'open').\nyield from bps.mv(shutter, 'open')"),
        (KIND_CLOSE_SHUTTER, "⊗  Close Shutter",
         "Close a shutter (set to 'closed').\nyield from bps.mv(shutter, 'closed')"),
        (KIND_TRIGGER_READ,  "◎  Trigger & Read",
         "Trigger detectors and read their values into the data stream.\nyield from bps.trigger_and_read(detectors)"),
        (KIND_CUSTOM_PY,     "⌨  Custom Python",
         "Write arbitrary Bluesky plan code.\nUse 'yield from bps.*' or 'yield from bp.*' inside."),
    ]

    _FLOW = [
        (KIND_REPEAT,   "↻  Repeat N",
         "Repeat a block of steps N times.\nfor _i in range(N): ..."),
        (KIND_FOR_EACH, "∀  For Each",
         "Iterate over a list, range (linspace), or CSV column(s).\nPaired motors: var '_sx, _sy', motor 'sx, sy', list of (x, y) tuples\nor CSV cols '0, 1' — moved together before each iteration."),
        (KIND_STATS,    "Σ  Statistics",
         "Wrap scan block(s) to compute CEN / COM / MAX / MIN / FWHM of a signal.\n"
         "Results become variables (<prefix>_cen, _com, ...) for later blocks;\n"
         "optionally move a motor to the chosen one afterwards."),
        (KIND_WHILE,    "∞  While",
         "Repeat while a condition is True.\nwhile condition: ..."),
        (KIND_IF_ELSE,  "?  If / Else",
         "Conditional branch — True body and optional False body.\nif condition: ... else: ..."),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan_names: list = []
        self._plan_descs: dict = {}
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        title = QLabel("BLOCKS")
        title.setObjectName("section_title")
        lay.addWidget(title)

        outer = self

        class _DragList(QListWidget):
            def startDrag(self, actions):
                item = self.currentItem()
                if not item:
                    return
                data = item.data(Qt.ItemDataRole.UserRole)
                if not data:
                    return
                if data.get("type") == "block":
                    node = FlowNode(kind=data["kind"])
                elif data.get("type") == "plan":
                    node = FlowNode(kind=KIND_PLAN, params={"plan_name": data["name"]})
                else:
                    return
                mime = QMimeData()
                mime.setData(_MIME, QByteArray(json.dumps(node.to_dict()).encode()))
                drag = QDrag(self)
                drag.setMimeData(mime)
                drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)

        self._list = _DragList()
        self._list.setDragEnabled(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.setStyleSheet(
            "QListWidget { border: none; }"
            "QListWidget::item { padding: 4px 6px; }"
            "QListWidget::item:selected { background: palette(highlight); color: palette(highlighted-text); }"
        )
        lay.addWidget(self._list, 1)

        self._populate()

    def _add_section(self, title: str):
        item = QListWidgetItem(f"── {title} ──")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor("#888"))
        font = QFont()
        font.setBold(True)
        font.setPointSize(9)
        item.setFont(font)
        item.setData(Qt.ItemDataRole.UserRole, None)
        self._list.addItem(item)

    def _add_item(self, kind: str, label: str, tooltip: str = ""):
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, {"type": "block", "kind": kind})
        if tooltip:
            item.setToolTip(tooltip)
        self._list.addItem(item)

    def _populate(self):
        self._list.clear()
        self._add_section("Structural")
        for kind, label, tip in self._STRUCTURAL:
            self._add_item(kind, label, tip)
        self._add_section("Flow Control")
        for kind, label, tip in self._FLOW:
            self._add_item(kind, label, tip)
        if self._plan_names:
            self._add_section("RE Plans")
            for name in sorted(self._plan_names):
                item = QListWidgetItem(f"▶  {name}")
                item.setData(Qt.ItemDataRole.UserRole, {"type": "plan", "name": name})
                desc = self._plan_descs.get(name, "")
                if desc:
                    item.setToolTip(desc[:600])
                self._list.addItem(item)

    def set_plans(self, plans: dict):
        self._plan_names = list(plans.keys()) if plans else []
        # Extract docstrings / descriptions from plan metadata
        self._plan_descs = {}
        for name, meta in (plans or {}).items():
            if isinstance(meta, dict):
                desc = meta.get("description", "") or ""
                if desc:
                    self._plan_descs[name] = desc
        self._populate()

    def _on_double_click(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data.get("type") == "block":
            self.block_activated.emit(data["kind"])
        elif data.get("type") == "plan":
            self.plan_activated.emit(data["name"])



# ── Example plan (ASWAXS energy scan) ─────────────────────────────────────────

def _build_example_nodes() -> list:
    """Build FlowNodes representing aswaxs_energy_scan for the opening example."""

    # ── inner: frames per position (Repeat → open/trigger/close/sleep) ────────
    frame_nodes = [
        FlowNode(KIND_OPEN_SHUTTER,  params={"device": "shutter"}),
        FlowNode(KIND_SLEEP,         params={"seconds": 0.3}),
        FlowNode(KIND_TRIGGER_READ,  params={"detectors": "[Pil300K, scalers]"}),
        FlowNode(KIND_CLOSE_SHUTTER, params={"device": "shutter"}),
        FlowNode(KIND_SLEEP,         params={"seconds": 1.0}),
    ]
    repeat_frames = FlowNode(KIND_REPEAT, params={"n": 3}, children=frame_nodes)

    # ── inner: sample positions (sx, sy moved together as a pair) ─────────────
    foreach_pos = FlowNode(KIND_FOR_EACH,
                           params={"varname": "_sx, _sy", "iter_type": "list",
                                   "values": "(0.0, 0.0), (5.0, -2.5), (10.0, -5.0)",
                                   "motor": "sx, sy"},
                           children=[repeat_frames])
    foreach_sx = foreach_pos

    # ── inner: undulator tracking (If/Else) ───────────────────────────────────
    move_undulator = FlowNode(KIND_MOVE, params={"device": "undulator", "position": 0.0})
    # True branch: move undulator to same energy as mono
    ifelse_undulator = FlowNode(
        KIND_IF_ELSE,
        params={"condition": "move_undulator > 0"},
        children=[FlowNode(KIND_CUSTOM_PY,
                           params={"code": "yield from bps.mv(undulator, _energy)"})],
        children_else=[],
    )

    sleep_settle = FlowNode(KIND_SLEEP, params={"seconds": 2.0})

    # ── outer: energy loop (For Each, linspace, motor=mono) ───────────────────
    foreach_energy = FlowNode(KIND_FOR_EACH,
                              params={"varname": "_energy", "iter_type": "range",
                                      "start": 7.0, "stop": 7.5, "num": 6,
                                      "motor": "mono"},
                              children=[ifelse_undulator, sleep_settle, foreach_sx])

    # ── top-level blocks ──────────────────────────────────────────────────────
    save_initial = FlowNode(KIND_CUSTOM_PY, params={"code": (
        "# Record initial positions — restored in cleanup\n"
        "initial_energy = mono.user_readback.get()\n"
        "initial_sx = sx.user_readback.get()\n"
        "initial_sy = sy.user_readback.get()"
    )})
    set_acq_time = FlowNode(KIND_SET_ATTR,
                            params={"device": "Pil300K", "attribute": "cam.acquire_time",
                                    "value": "1.0"})
    stage_det = FlowNode(KIND_STAGE, params={"device": "Pil300K"})
    stage_sca = FlowNode(KIND_STAGE, params={"device": "scalers"})

    unstage_det = FlowNode(KIND_UNSTAGE, params={"device": "Pil300K"})
    unstage_sca = FlowNode(KIND_UNSTAGE, params={"device": "scalers"})
    restore = FlowNode(KIND_CUSTOM_PY, params={"code": (
        "# Return all devices to initial positions\n"
        "yield from bps.mv(mono, initial_energy,\n"
        "                  sx,   initial_sx,\n"
        "                  sy,   initial_sy)\n"
        "yield from bps.mv(shutter, 1)  # ensure shutter closed"
    )})

    return [
        save_initial,
        set_acq_time,
        stage_det,
        stage_sca,
        foreach_energy,
        unstage_det,
        unstage_sca,
        restore,
    ]


# ── _ParamEditor ──────────────────────────────────────────────────────────────

class _ParamEditor(QWidget):
    """Collapsible plan-parameter editor.

    Lets the user define named parameters that appear in the generated
    function signature:  def plan_name(param1 = default1, ..., md=None)
    """
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._params: list = []   # list of {name, default, annotation}
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)

        header_row = QHBoxLayout()
        self._toggle = QPushButton("▸ Plan Parameters (0)")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setStyleSheet(
            "text-align: left; padding: 2px 6px; border: none; "
            "background: transparent; color: #aaa; font-weight: bold;")
        self._toggle.toggled.connect(self._on_toggle)
        header_row.addWidget(self._toggle)

        btn_add = QPushButton("+ Add")
        btn_add.setFixedWidth(54)
        btn_add.setToolTip("Add a plan parameter (e.g. detectors, motor, acquire_time)")
        btn_add.clicked.connect(self._add_row)
        header_row.addWidget(btn_add)

        btn_preset = QPushButton("ASWAXS preset")
        btn_preset.setFixedWidth(100)
        btn_preset.setToolTip("Load the ASWAXS energy scan parameter preset")
        btn_preset.clicked.connect(self._load_aswaxs_preset)
        header_row.addWidget(btn_preset)
        header_row.addStretch()
        lay.addLayout(header_row)

        # Table: Name | Default | Type hint | (delete)
        from qtpy.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Name", "Default value", "Type hint", ""])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(3, 28)
        self._table.setMaximumHeight(180)
        self._table.setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.setToolTip(
            "Define plan parameters.\n"
            "Name: parameter name used in the plan body (e.g. energy_motor)\n"
            "Default: default value expression (e.g. mono, [Pil300K], 1.0)\n"
            "Type hint: optional annotation (e.g. float, int, list)")
        lay.addWidget(self._table)

    def _on_toggle(self, checked: bool):
        self._table.setVisible(checked)
        n = len(self.get_params())
        self._toggle.setText(f"{'▾' if checked else '▸'} Plan Parameters ({n})")

    def _add_row(self, name="param", default="", annotation=""):
        from qtpy.QtWidgets import QTableWidgetItem
        self._table.blockSignals(True)
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(name))
        self._table.setItem(row, 1, QTableWidgetItem(default))
        self._table.setItem(row, 2, QTableWidgetItem(annotation))
        del_btn = QToolButton()
        del_btn.setText("×")
        del_btn.setFixedSize(24, 24)
        del_btn.setStyleSheet("border: none; color: #aaa;")
        del_btn.clicked.connect(lambda _=False, r=row: self._delete_row(r))
        self._table.setCellWidget(row, 3, del_btn)
        self._table.blockSignals(False)
        self._update_toggle_label()
        self.changed.emit()

    def _delete_row(self, row: int):
        # Find the actual current row for this button
        btn = self.sender()
        for r in range(self._table.rowCount()):
            w = self._table.cellWidget(r, 3)
            if w is btn:
                self._table.removeRow(r)
                break
        self._update_toggle_label()
        self.changed.emit()

    def _on_cell_changed(self, row, col):
        self._update_toggle_label()
        self.changed.emit()

    def _update_toggle_label(self):
        n = len(self.get_params())
        checked = self._toggle.isChecked()
        self._toggle.setText(f"{'▾' if checked else '▸'} Plan Parameters ({n})")

    def get_params(self) -> list:
        from qtpy.QtWidgets import QTableWidgetItem
        params = []
        for row in range(self._table.rowCount()):
            name = (self._table.item(row, 0) or QTableWidgetItem()).text().strip()
            default = (self._table.item(row, 1) or QTableWidgetItem()).text().strip()
            ann = (self._table.item(row, 2) or QTableWidgetItem()).text().strip()
            if name:
                params.append({"name": name, "default": default, "annotation": ann})
        return params

    def set_params(self, params: list):
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for p in params:
            row = self._table.rowCount()
            self._table.insertRow(row)
            from qtpy.QtWidgets import QTableWidgetItem
            self._table.setItem(row, 0, QTableWidgetItem(p.get("name", "")))
            self._table.setItem(row, 1, QTableWidgetItem(p.get("default", "")))
            self._table.setItem(row, 2, QTableWidgetItem(p.get("annotation", "")))
            del_btn = QToolButton()
            del_btn.setText("×")
            del_btn.setFixedSize(24, 24)
            del_btn.setStyleSheet("border: none; color: #aaa;")
            del_btn.clicked.connect(lambda _=False: self._delete_row(-1))
            self._table.setCellWidget(row, 3, del_btn)
        self._table.blockSignals(False)
        self._update_toggle_label()

    _ASWAXS_PARAMS = [
        {"name": "energy_motor",  "default": "mono",                        "annotation": ""},
        {"name": "energy_list",   "default": "np.linspace(7.0, 7.5, 6)",   "annotation": ""},
        {"name": "detectors",     "default": "[Pil300K]",                   "annotation": "list"},
        {"name": "scalers",       "default": "scalers",                     "annotation": ""},
        {"name": "sx",            "default": "sx",                          "annotation": ""},
        {"name": "sy",            "default": "sy",                          "annotation": ""},
        {"name": "acquire_time",  "default": "1.0",                         "annotation": "float"},
        {"name": "num_frame",     "default": "3",                           "annotation": "int"},
        {"name": "delay_time",    "default": "1.0",                         "annotation": "float"},
        {"name": "sleep_time",    "default": "2.0",                         "annotation": "float"},
        {"name": "shutter",       "default": "None",                        "annotation": ""},
        {"name": "undulator",     "default": "None",                        "annotation": ""},
        {"name": "move_undulator","default": "False",                       "annotation": "bool"},
        {"name": "hdf_autosave",  "default": "True",                        "annotation": "bool"},
    ]

    def _load_aswaxs_preset(self):
        self.set_params(self._ASWAXS_PARAMS)
        if not self._toggle.isChecked():
            self._toggle.setChecked(True)
        self.changed.emit()


# ── ComposerWidget ─────────────────────────────────────────────────────────────

class ComposerWidget(QWidget):
    send_to_editor = Signal(str)

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self._devices: dict = {}
        self._plans: dict = {}
        self._build()
        self._load_example()

    def _build(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(4, 4, 4, 4)
        root_lay.setSpacing(4)

        # Top bar: plan name + save/load
        top = QHBoxLayout()
        top.addWidget(QLabel("Plan name:"))
        self._plan_name = QLineEdit("aswaxs_energy_scan")
        self._plan_name.setFixedWidth(200)
        self._plan_name.textChanged.connect(self._update_preview)
        top.addWidget(self._plan_name)
        top.addSpacing(12)
        btn_save = QPushButton("💾 Save .ebplan")
        btn_load = QPushButton("📂 Load .ebplan")
        btn_example = QPushButton("★ Load Example")
        btn_example.setToolTip("Load the ASWAXS energy scan example plan")
        btn_clear = QPushButton("🗑 Clear")
        btn_save.clicked.connect(self._save_ebplan)
        btn_load.clicked.connect(self._load_ebplan)
        btn_example.clicked.connect(self._load_example)
        btn_clear.clicked.connect(self._clear_canvas)
        top.addWidget(btn_save)
        top.addWidget(btn_load)
        top.addWidget(btn_example)
        top.addWidget(btn_clear)
        top.addStretch()
        root_lay.addLayout(top)

        # Parameters editor (collapsible)
        self._param_editor = _ParamEditor()
        self._param_editor.changed.connect(self._update_preview)
        root_lay.addWidget(self._param_editor)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: palette(mid);")
        root_lay.addWidget(sep)

        # Main split: palette | canvas | code preview
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Palette
        self._palette = PalettePanel()
        self._palette.setMinimumWidth(160)
        self._palette.setMaximumWidth(220)
        self._palette.block_activated.connect(self._add_block)
        self._palette.plan_activated.connect(self._add_plan)
        splitter.addWidget(self._palette)

        # Canvas
        self._canvas = FlowCanvas()
        self._canvas.changed.connect(self._update_preview)
        splitter.addWidget(self._canvas)

        # Code preview (right, toggleable)
        self._preview_pane = QWidget()
        prev_lay = QVBoxLayout(self._preview_pane)
        prev_lay.setContentsMargins(0, 0, 0, 0)
        prev_lbl = QLabel("CODE PREVIEW")
        prev_lbl.setObjectName("section_title")
        prev_lay.addWidget(prev_lbl)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setFont(QFont("Courier New", 11))
        PythonHighlighter(self._preview.document())
        prev_lay.addWidget(self._preview)
        self._preview_pane.setVisible(True)
        splitter.addWidget(self._preview_pane)

        splitter.setSizes([185, 480, 360])
        root_lay.addWidget(splitter, 1)

        # Bottom bar
        bottom = QHBoxLayout()
        self._preview_toggle = QPushButton("▼ Hide Preview")
        self._preview_toggle.setCheckable(True)
        self._preview_toggle.setChecked(True)
        self._preview_toggle.toggled.connect(self._toggle_preview)
        bottom.addWidget(self._preview_toggle)
        bottom.addStretch()
        btn_send = QPushButton("→ Send to Code Editor")
        btn_send.setStyleSheet("font-weight: bold; padding: 4px 12px;")
        btn_send.clicked.connect(self._send_to_editor)
        bottom.addWidget(btn_send)
        root_lay.addLayout(bottom)

    def _toggle_preview(self, checked: bool):
        self._preview_pane.setVisible(checked)
        self._preview_toggle.setText("▼ Hide Preview" if checked else "▶ Show Preview")
        if checked:
            self._update_preview()

    def _update_preview(self):
        code = generate_code(
            self._canvas.get_nodes(),
            self._plan_name.text() or "my_plan",
            self._param_editor.get_params(),
        )
        self._preview.setPlainText(code)

    def _load_example(self):
        self._plan_name.setText("aswaxs_energy_scan")
        self._canvas.set_nodes(_build_example_nodes())
        if hasattr(self, "_param_editor"):
            self._param_editor.set_params(_ParamEditor._ASWAXS_PARAMS)
        # Defer preview update so widget is visible before we render
        QTimer.singleShot(0, self._update_preview)

    def _add_block(self, kind: str):
        self._canvas.add_node(FlowNode(kind=kind))

    def _add_plan(self, plan_name: str):
        self._canvas.add_node(FlowNode(kind=KIND_PLAN, params={"plan_name": plan_name}))

    def _send_to_editor(self):
        code = generate_code(
            self._canvas.get_nodes(),
            self._plan_name.text() or "my_plan",
            self._param_editor.get_params(),
        )
        self.send_to_editor.emit(code)

    def _save_ebplan(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plan", str(Path.home()), "EasyBluesky Plan (*.ebplan)")
        if not path:
            return
        if not path.endswith(".ebplan"):
            path += ".ebplan"
        data = {
            "plan_name": self._plan_name.text(),
            "params": self._param_editor.get_params(),
            "nodes": [n.to_dict() for n in self._canvas.get_nodes()],
        }
        try:
            Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "Save Error", str(e))

    def _load_ebplan(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Plan", str(Path.home()), "EasyBluesky Plan (*.ebplan)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "Load Error", str(e))
            return
        if "plan_name" in data:
            self._plan_name.setText(data["plan_name"])
        if "params" in data:
            self._param_editor.set_params(data["params"])
        if "nodes" in data:
            nodes = [FlowNode.from_dict(d) for d in data["nodes"]]
            self._canvas.set_nodes(nodes)
        else:
            self._canvas.from_json(Path(path).read_text(encoding="utf-8"))

    def _clear_canvas(self):
        if self._canvas.get_nodes():
            r = QMessageBox.question(self, "Clear Canvas", "Remove all blocks?")
            if r != QMessageBox.StandardButton.Yes:
                return
        self._canvas.clear()

    def set_devices(self, devices: dict):
        self._devices = devices
        self._canvas.set_devices(devices)

    def set_plans(self, plans: dict):
        self._plans = plans
        self._palette.set_plans(plans)


# ── Main PlanBuilder widget (two tabs) ─────────────────────────────────────────

# ── Plan file tree (drag-and-drop aware) ──────────────────────────────────────

class _PlanFileTree(QTreeWidget):
    """QTreeWidget that accepts folder drops from the OS file manager."""
    folder_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if Path(url.toLocalFile()).is_dir():
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).is_dir():
                self.folder_dropped.emit(p)
        event.acceptProposedAction()


# ── SFTP background threads ────────────────────────────────────────────────────

class _RemoteFileLister(QThread):
    result = Signal(list)
    error  = Signal(str)

    def __init__(self, conn_settings, profile, parent=None):
        super().__init__(parent)
        self._cs, self._pr = conn_settings, profile

    def run(self):
        from .ssh_manager import list_remote_plan_files
        try:
            self.result.emit(list_remote_plan_files(self._cs, self._pr))
        except Exception as e:
            self.error.emit(str(e))


class _RemoteFileReader(QThread):
    result = Signal(str, str)   # (filename, content)
    error  = Signal(str, str)   # (filename, msg)

    def __init__(self, conn_settings, profile, filename, parent=None):
        super().__init__(parent)
        self._cs, self._pr, self._fn = conn_settings, profile, filename

    def run(self):
        from .ssh_manager import read_remote_plan_file
        try:
            self.result.emit(self._fn, read_remote_plan_file(self._cs, self._pr, self._fn))
        except Exception as e:
            self.error.emit(self._fn, str(e))


class _RemoteFileSaver(QThread):
    done = Signal(bool, str)   # (success, message)

    def __init__(self, conn_settings, profile, filename, content, parent=None):
        super().__init__(parent)
        self._cs, self._pr = conn_settings, profile
        self._fn, self._content = filename, content

    def run(self):
        from .ssh_manager import write_remote_plan_file
        ok, msg = write_remote_plan_file(self._cs, self._pr, self._fn, self._content)
        self.done.emit(ok, msg)


# ── _DiffDialog ───────────────────────────────────────────────────────────────

class _DiffDialog(QDialog):
    """Show a unified diff of a local editor buffer vs a remote copy."""

    def __init__(self, local_text: str, remote_text: str, filename: str, parent=None):
        super().__init__(parent)
        import difflib
        self.setWindowTitle(f"↔  Compare: {filename}")
        self.resize(900, 640)

        lay = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>Local</b> ← {filename}"))
        header.addStretch()
        header.addWidget(QLabel(f"<b>Remote</b> → {filename}"))
        lay.addLayout(header)

        browser = QTextBrowser()
        browser.setFont(QFont("Courier New", 12))
        browser.setOpenLinks(False)

        local_lines  = local_text.splitlines(keepends=True)
        remote_lines = remote_text.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            remote_lines, local_lines,
            fromfile=f"remote/{filename}",
            tofile=f"local/{filename}",
        ))

        if not diff:
            browser.setPlainText("Files are identical.")
        else:
            html_parts = []
            for line in diff:
                line_esc = (line.rstrip("\n")
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;"))
                if line.startswith("---") or line.startswith("+++"):
                    style = "color:#888; font-style:italic;"
                elif line.startswith("@@"):
                    style = "color:#4d9fe8; background:#1a2a3a;"
                elif line.startswith("+"):
                    style = "color:#2ecc71; background:#0d2b1a;"
                elif line.startswith("-"):
                    style = "color:#e74c3c; background:#2b0d0d;"
                else:
                    style = "color:#ccc;"
                html_parts.append(
                    f'<div style="margin:0;padding:0;white-space:pre;{style}">'
                    f'{line_esc}</div>'
                )
            browser.setHtml(
                '<html><body style="background:#1e1e1e;font-family:Courier New,monospace;">'
                + "".join(html_parts)
                + "</body></html>"
            )

        lay.addWidget(browser, 1)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btns = QHBoxLayout()
        btns.addStretch()
        btns.addWidget(btn_close)
        lay.addLayout(btns)


# ── PlanFileTreePanel ─────────────────────────────────────────────────────────

class PlanFileTreePanel(QWidget):
    """Reusable file-tree panel for browsing remote and local plan .py files.

    Emits ``file_open_requested(tier, name_or_path)`` when the user clicks a
    file.  The caller decides what to do with it (open in editor, switch tabs,
    etc.).  ``output_message(str)`` carries status/error text for the caller to
    display however it likes.
    """

    file_open_requested  = Signal(str, str)   # (tier, name_or_path)
    output_message       = Signal(str)
    local_plans_added    = Signal()           # folder added — triggers env restart
    local_plans_removed  = Signal(str)        # folder removed (path) — env restart needed
    remote_file_names_ready = Signal(set)     # emitted when remote list arrives

    def __init__(self, show_new_remote_btn: bool = True, parent=None):
        super().__init__(parent)
        self._conn_settings:    dict = {}
        self._profile_name:     str  = ""
        self._active_threads:   list = []
        self._show_new_remote = show_new_remote_btn
        self._remote_file_names: set = set()
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lbl = QLabel("PLAN FILES")
        lbl.setObjectName("section_title")
        lay.addWidget(lbl)

        self._tree = _PlanFileTree()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.folder_dropped.connect(self._do_add_folder)
        lay.addWidget(self._tree, 1)

        btns = QHBoxLayout()
        btn_add = QPushButton("＋ Add Folder")
        btn_add.clicked.connect(self._on_add_folder_clicked)
        btns.addWidget(btn_add)
        if self._show_new_remote:
            btn_new = QPushButton("＋ Remote File")
            btn_new.clicked.connect(self._on_new_remote_file)
            btns.addWidget(btn_new)
        lay.addLayout(btns)

    # ── profile ────────────────────────────────────────────────────────────────

    def set_profile(self, conn_settings: dict) -> None:
        from .connection_settings import get_active_profile
        self._conn_settings = conn_settings
        self._profile_name  = get_active_profile(conn_settings).get("name", "")
        self._refresh()

    def clear_profile(self) -> None:
        self._conn_settings = {}
        self._profile_name  = ""
        self._tree.clear()

    def _refresh(self) -> None:
        from .plans_manager import get_user_dirs
        self._tree.clear()
        bold = QFont()
        bold.setBold(True)

        self._remote_header = QTreeWidgetItem(
            [f"📁 {self._profile_name} Plans  (remote)"])
        self._remote_header.setFont(0, bold)
        self._remote_header.setData(
            0, Qt.ItemDataRole.UserRole, {"tier": "header_remote"})
        self._tree.addTopLevelItem(self._remote_header)
        self._remote_header.setExpanded(True)

        ph = QTreeWidgetItem(["  ⏳ Loading…"])
        ph.setFlags(ph.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._remote_header.addChild(ph)

        if self._conn_settings:
            self._start_remote_list()

        for d in get_user_dirs(self._profile_name):
            self._add_local_dir(d)

    def _start_remote_list(self) -> None:
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileLister(self._conn_settings, profile, self)
        t.result.connect(self._on_remote_listed)
        t.error.connect(self._on_remote_error)
        t.start()
        self._active_threads.append(t)

    def _on_remote_listed(self, files: list) -> None:
        self._remote_header.takeChildren()
        self._remote_header.setText(
            0, f"📁 {self._profile_name} Plans  (remote, {len(files)} files)")
        if not files:
            empty = QTreeWidgetItem(["  (empty — use ＋ Remote File to create)"])
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._remote_header.addChild(empty)
        else:
            for fn in files:
                item = QTreeWidgetItem([f"  {fn}"])
                item.setData(0, Qt.ItemDataRole.UserRole, {"tier": "remote", "name": fn})
                self._remote_header.addChild(item)
        self._remote_file_names = set(files)
        self._update_local_indicators()
        self.remote_file_names_ready.emit(self._remote_file_names)

    def _on_remote_error(self, msg: str) -> None:
        self._remote_header.takeChildren()
        err = QTreeWidgetItem([f"  ⚠ {msg}"])
        err.setFlags(err.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._remote_header.addChild(err)

    def _update_local_indicators(self) -> None:
        """Mark local file items with ↕ when they also exist on the remote."""
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            top = root.child(i)
            top_data = top.data(0, Qt.ItemDataRole.UserRole) or {}
            if top_data.get("tier") != "header_local":
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                cdata = child.data(0, Qt.ItemDataRole.UserRole) or {}
                if cdata.get("tier") != "local":
                    continue
                fn = cdata.get("name", "")
                if fn in self._remote_file_names:
                    child.setText(0, f"  ↕ {fn}")
                    child.setToolTip(0, "Also exists on beamline computer")
                else:
                    child.setText(0, f"  {fn}")
                    child.setToolTip(0, "")

    def _add_local_dir(self, dir_path: str) -> None:
        bold = QFont()
        bold.setBold(True)
        p = Path(dir_path)
        header = QTreeWidgetItem([f"📂 {p.name}  ({dir_path})"])
        header.setFont(0, bold)
        header.setData(0, Qt.ItemDataRole.UserRole,
                       {"tier": "header_local", "path": dir_path})
        self._tree.addTopLevelItem(header)
        header.setExpanded(True)
        py_files = sorted(p.glob("*.py")) if p.is_dir() else []
        if not py_files:
            empty = QTreeWidgetItem(["  (no .py files)"])
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            header.addChild(empty)
        for f in py_files:
            label = f"  ↕ {f.name}" if f.name in self._remote_file_names else f"  {f.name}"
            tooltip = "Also exists on beamline computer" if f.name in self._remote_file_names else ""
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole,
                         {"tier": "local", "name": f.name, "path": str(f)})
            item.setToolTip(0, tooltip)
            header.addChild(item)

    # ── interactions ───────────────────────────────────────────────────────────

    def _on_item_clicked(self, item, _col) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        tier = data.get("tier", "")
        if tier == "remote":
            self.file_open_requested.emit("remote", data["name"])
        elif tier == "local":
            self.file_open_requested.emit("local", data["path"])

    def _on_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        tier = data.get("tier", "")
        menu = QMenu(self)
        if tier == "remote":
            menu.addAction("Delete from remote",
                           lambda: self._delete_remote_file(data["name"]))
        elif tier == "local":
            menu.addAction("Delete file",
                           lambda: self._delete_local_file(data["path"]))
        elif tier == "header_local":
            menu.addAction("Remove folder from list",
                           lambda: self._remove_local_dir(data["path"]))
        if not menu.isEmpty():
            menu.exec(self._tree.mapToGlobal(pos))

    def _delete_remote_file(self, filename: str) -> None:
        from .connection_settings import get_active_profile
        from .ssh_manager import delete_remote_plan_file
        r = QMessageBox.question(
            self, "Delete File",
            f"Delete '{filename}' from the remote plans directory?")
        if r != QMessageBox.StandardButton.Yes:
            return
        profile = get_active_profile(self._conn_settings)
        ok, msg = delete_remote_plan_file(self._conn_settings, profile, filename)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output_message.emit(f"[{ts}] {'✓' if ok else '✗'} {msg}")
        if ok:
            self._start_remote_list()

    def _remove_local_dir(self, dir_path: str) -> None:
        from .plans_manager import remove_user_dir
        remove_user_dir(self._profile_name, dir_path)
        self._refresh()
        ts = datetime.now().strftime("%H:%M:%S")
        self.output_message.emit(
            f"[{ts}] Removed folder: {dir_path}\n"
            f"  Plans from this folder remain in Available Plans until you "
            f"Close Env → Open Env.")
        self.local_plans_removed.emit(dir_path)

    def _delete_local_file(self, file_path: str) -> None:
        from qtpy.QtWidgets import QMessageBox
        name = Path(file_path).name
        if QMessageBox.question(
                self, "Delete file",
                f"Permanently delete '{name}' from disk?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            Path(file_path).unlink()
        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S")
            self.output_message.emit(f"[{ts}] Could not delete {name}: {e}")
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.output_message.emit(f"[{ts}] Deleted: {file_path}")
        self._refresh()
        self.local_plans_removed.emit(str(Path(file_path).parent))

    def _on_add_folder_clicked(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Plan Folder")
        if path:
            self._do_add_folder(path)

    def _do_add_folder(self, path: str) -> None:
        from .plans_manager import add_user_dir
        if add_user_dir(self._profile_name, path):
            self._add_local_dir(path)
            ts = datetime.now().strftime("%H:%M:%S")
            self.output_message.emit(f"[{ts}] Added folder: {path}")
            self.local_plans_added.emit()
        else:
            self.output_message.emit(f"  Folder already in list: {path}")

    def _on_new_remote_file(self) -> None:
        if not self._conn_settings or not self._profile_name:
            QMessageBox.warning(self, "Not Connected", "Connect to a profile first.")
            return
        name, ok = QInputDialog.getText(
            self, "New Remote Plan File", "Filename (e.g. my_plan.py):")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not name.endswith(".py"):
            name += ".py"
        from .connection_settings import get_active_profile
        from .ssh_manager import write_remote_plan_file
        profile = get_active_profile(self._conn_settings)
        ok2, msg = write_remote_plan_file(
            self._conn_settings, profile, name,
            f"# {name}\nimport bluesky.plans as bp\nimport bluesky.plan_stubs as bps\n\n\n"
        )
        ts = datetime.now().strftime("%H:%M:%S")
        if ok2:
            self.output_message.emit(f"[{ts}] ✓ Created {name}")
            self._start_remote_list()
            self.file_open_requested.emit("remote", name)
        else:
            self.output_message.emit(f"[{ts}] ✗ {msg}")


# ── PlanBuilder ────────────────────────────────────────────────────────────────

class PlanBuilder(QWidget):
    plans_uploading = Signal(str)   # emitted when local plan upload starts (msg)

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker  = worker
        self.plans   = {}
        self.devices = {}
        self._env_reload_attempts = 0
        self._conn_settings:    dict = {}
        self._profile_name:     str  = ""
        self._file_tier:        str  = ""
        self._file_name:        str  = ""
        self._dirty:            bool = False
        self._active_threads:   list = []
        self._remote_file_names: set = set()
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        tabs.setTabPosition(QTabWidget.TabPosition.North)

        # Tab 1: Visual Composer
        self._composer = ComposerWidget(worker=self.worker)
        tabs.addTab(self._composer, "🎛  Visual Composer")

        # Tab 1b: Blockly Composer (Scratch-style blocks, needs QtWebEngine)
        self._blockly = None
        try:
            from .blockly_composer import BlocklyComposerWidget
            self._blockly = BlocklyComposerWidget(worker=self.worker)
            tabs.addTab(self._blockly, "🧩  Blockly Composer")
        except ImportError as exc:
            note = QLabel(
                "The Blockly Composer needs Qt WebEngine.\n\n"
                "    pip install PyQt6-WebEngine\n\n"
                f"({exc})")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tabs.addTab(note, "🧩  Blockly Composer")

        # Tab 2: Code Editor (kept for advanced use)
        self._editor_tab_index = tabs.count()
        tabs.addTab(self._build_code_editor(), "📝  Code Editor")

        self._tabs = tabs
        self._composer.send_to_editor.connect(self._on_send_to_editor)
        if self._blockly is not None:
            self._blockly.send_to_editor.connect(self._on_send_to_editor)

        lay.addWidget(tabs)

    # ── Code editor tab (unchanged from original) ──────────────────────────────

    def _build_code_editor(self) -> QWidget:
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(8, 8, 8, 8)
        outer_lay.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: editor pane ──────────────────────────────────────────────
        editor_pane = QWidget()
        lay = QVBoxLayout(editor_pane)
        lay.setContentsMargins(0, 0, 4, 0)

        title_row = QHBoxLayout()
        lbl = QLabel("CODE EDITOR")
        lbl.setObjectName("section_title")
        title_row.addWidget(lbl)
        title_row.addStretch()
        self._editor_file_lbl = QLabel("")
        self._editor_file_lbl.setStyleSheet("font-size: 11px; color: #888;")
        title_row.addWidget(self._editor_file_lbl)
        lay.addLayout(title_row)

        self.editor = CodeEditor()
        self.editor.setFont(QFont("Courier New", 13))
        self.editor.setPlaceholderText(
            "# Write a custom plan here — it will be uploaded to the RE Manager\n\n"
            "def my_plan(detector, motor, start, stop, num):\n"
            "    yield from bp.scan([detector], motor, start, stop, num)\n"
        )
        self.highlighter = PythonHighlighter(self.editor.document())
        self.editor.document().contentsChanged.connect(self._on_editor_changed)
        lay.addWidget(self.editor, 1)

        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self.tmpl_combo = QComboBox()
        self.tmpl_combo.addItems([
            "-- select --",
            "Simple scan",
            "Multi-motor scan",
            "Scan with per-step",
            "Scan with shutter + AD",
            "Grid scan",
            "Count",
            "Move and count",
            "Custom loop",
        ])
        self.tmpl_combo.currentTextChanged.connect(self._insert_template)
        tmpl_row.addWidget(self.tmpl_combo, 1)
        lay.addLayout(tmpl_row)

        e_btns = QHBoxLayout()
        btn_open   = QPushButton("📂  Open file")
        btn_save   = QPushButton("💾  Save")
        btn_check  = QPushButton("✔  Check")
        btn_check.setToolTip("Check for syntax errors, undefined names, and missing imports")
        btn_upload = QPushButton("⬆  Upload to RE Manager")
        btn_reload = QPushButton("↺  Reload RE env")
        self._btn_compare = QPushButton("↔  Compare with Remote")
        self._btn_compare.setToolTip("Show diff of local file vs version on beamline computer")
        self._btn_compare.setVisible(False)
        btn_open.clicked.connect(self._open_script)
        btn_save.clicked.connect(self._save_script)
        btn_check.clicked.connect(self._check_plan)
        btn_upload.clicked.connect(self._upload_script)
        btn_reload.clicked.connect(self._reload_environment)
        self._btn_compare.clicked.connect(self._compare_with_remote)
        e_btns.addWidget(btn_open)
        e_btns.addWidget(btn_save)
        e_btns.addWidget(btn_check)
        e_btns.addWidget(btn_upload)
        e_btns.addWidget(btn_reload)
        e_btns.addWidget(self._btn_compare)
        lay.addLayout(e_btns)

        lbl_out = QLabel("OUTPUT")
        lbl_out.setObjectName("section_title")
        lay.addWidget(lbl_out)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(100)
        self.output.setFont(QFont("Courier New", 11))
        lay.addWidget(self.output)

        splitter.addWidget(editor_pane)

        # ── Right: file tree pane ──────────────────────────────────────────
        self._file_panel = PlanFileTreePanel(show_new_remote_btn=True)
        self._file_panel.file_open_requested.connect(self._on_file_panel_open)
        self._file_panel.output_message.connect(self.output.appendPlainText)
        self._file_panel.local_plans_added.connect(self._on_local_plans_added)
        self._file_panel.local_plans_removed.connect(self._on_local_plans_removed)
        self._file_panel.remote_file_names_ready.connect(self._on_remote_names_ready)
        splitter.addWidget(self._file_panel)
        splitter.setSizes([620, 260])

        outer_lay.addWidget(splitter, 1)
        return outer

    # ── Code checking ──────────────────────────────────────────────────────────

    def _syntax_check(self, content: str) -> tuple:
        try:
            compile(content, "<plan>", "exec")
        except SyntaxError as e:
            return False, f"Syntax error on line {e.lineno}: {e.msg}", e.lineno or 0
        return True, "Syntax OK", 0

    def _import_check(self, content: str) -> list:
        import ast
        import importlib.util
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        missing = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            for mod in mods:
                try:
                    if importlib.util.find_spec(mod) is None:
                        missing.add(mod)
                except (ValueError, ModuleNotFoundError):
                    missing.add(mod)
        return sorted(missing)

    def _pyflakes_check(self, content: str) -> list:
        try:
            import io
            from pyflakes import api as _pf_api
            from pyflakes import reporter as _pf_reporter
        except ImportError:
            return []
        buf = io.StringIO()
        r = _pf_reporter.Reporter(buf, buf)
        _pf_api.check(content, "<plan>", reporter=r)
        lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
        clean = []
        for ln in lines:
            if ":" in ln:
                parts = ln.split(":", 3)
                if len(parts) >= 4:
                    clean.append(f"line {parts[1]}: {parts[3].strip()}")
                    continue
            clean.append(ln)
        return clean

    def _jump_to_line(self, lineno: int):
        if lineno < 1:
            return
        block = self.editor.document().findBlockByLineNumber(lineno - 1)
        if block.isValid():
            cursor = self.editor.textCursor()
            cursor.setPosition(block.position())
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()

    def _check_plan(self) -> bool:
        """Run all checks; log results to output. Returns True if syntax is valid."""
        content = self.editor.toPlainText()
        ts = datetime.now().strftime("%H:%M:%S")

        ok, msg, lineno = self._syntax_check(content)
        if not ok:
            self._jump_to_line(lineno)
            self.output.appendPlainText(f"[{ts}] ✗ {msg}")
            return False

        lines = ["✓ Syntax OK"]

        pf_issues = self._pyflakes_check(content)
        if pf_issues:
            for issue in pf_issues[:5]:
                lines.append(f"       ⚠ {issue}")
            if len(pf_issues) > 5:
                lines.append(f"       … +{len(pf_issues) - 5} more issue(s)")
        else:
            lines.append("       (no undefined names or unused imports)")

        missing = self._import_check(content)
        if missing:
            lines.append(
                f"       ⚠ Modules not found locally (may be fine on RE machine): "
                f"{', '.join(missing)}"
            )

        self.output.appendPlainText(f"[{ts}] " + "\n".join(lines))
        return True

    def _insert_template(self, name):
        _ANN = (
            "from typing import List\n"
            "from bluesky.protocols import Readable, Movable\n"
        )
        templates = {
            "Simple scan": (
                _ANN + "\n"
                "def my_scan(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        start: float,\n"
                "        stop: float,\n"
                "        num: int):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.scan(detectors, motor, start, stop, num)\n"
            ),
            "Multi-motor scan": (
                _ANN + "\n"
                "def multi_motor_scan(\n"
                "        detectors: List[Readable],\n"
                "        motor1: Movable, start1: float, stop1: float,\n"
                "        motor2: Movable, start2: float, stop2: float,\n"
                "        num: int):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.scan(\n"
                "        detectors,\n"
                "        motor1, start1, stop1,\n"
                "        motor2, start2, stop2,\n"
                "        num)\n"
            ),
            "Grid scan": (
                _ANN + "\n"
                "def my_grid_scan(\n"
                "        detectors: List[Readable],\n"
                "        motor1: Movable, start1: float, stop1: float, num1: int,\n"
                "        motor2: Movable, start2: float, stop2: float, num2: int,\n"
                "        snake_axes: bool = False):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.grid_scan(\n"
                "        detectors,\n"
                "        motor1, start1, stop1, num1,\n"
                "        motor2, start2, stop2, num2,\n"
                "        snake_axes=snake_axes)\n"
            ),
            "Count": (
                _ANN + "\n"
                "def my_count(\n"
                "        detectors: List[Readable],\n"
                "        num: int = 10,\n"
                "        delay: float = 0.1):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.count(detectors, num=num, delay=delay)\n"
            ),
            "Move and count": (
                _ANN + "\n"
                "def move_and_count(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        position: float,\n"
                "        num: int = 5):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n"
                "    yield from bps.mv(motor, position)\n"
                "    yield from bp.count(detectors, num=num)\n"
            ),
            "Scan with per-step": (
                _ANN + "\n"
                "def scan_with_per_step(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        start: float,\n"
                "        stop: float,\n"
                "        num: int,\n"
                "        exposure_time: float = 1.0,\n"
                "        sleep_time: float = 0.0):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n\n"
                "    for det in detectors:\n"
                "        yield from bps.mv(det.cam.acquire_time, exposure_time)\n\n"
                "    def _per_step(detectors, step, pos_cache):\n"
                "        yield from bps.move_per_step(step, pos_cache)\n"
                "        yield from bps.trigger_and_read(detectors)\n"
                "        if sleep_time > 0:\n"
                "            yield from bps.sleep(sleep_time)\n\n"
                "    yield from bp.scan(\n"
                "        detectors, motor, start, stop, num,\n"
                "        per_step=_per_step)\n"
            ),
            "Scan with shutter + AD": (
                _ANN + "\n"
                "def scan_with_shutter_ad(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        start: float,\n"
                "        stop: float,\n"
                "        num: int,\n"
                "        shutter: Movable,\n"
                "        file_path: str = '/data/',\n"
                "        file_name: str = 'scan',\n"
                "        exposure_time: float = 1.0,\n"
                "        sleep_time: float = 0.5):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n\n"
                "    for det in detectors:\n"
                "        yield from bps.mv(det.cam.acquire_time, exposure_time)\n"
                "        yield from bps.abs_set(det.hdf1.file_path, file_path, wait=True)\n"
                "        yield from bps.abs_set(det.hdf1.file_name, file_name, wait=True)\n\n"
                "    def _per_step(detectors, step, pos_cache):\n"
                "        yield from bps.move_per_step(step, pos_cache)\n"
                "        yield from bps.mv(shutter, 'open')\n"
                "        yield from bps.trigger_and_read(detectors)\n"
                "        yield from bps.mv(shutter, 'closed')\n"
                "        yield from bps.sleep(sleep_time)\n\n"
                "    yield from bp.scan(\n"
                "        detectors, motor, start, stop, num,\n"
                "        per_step=_per_step)\n"
            ),
            "Custom loop": (
                _ANN + "\n"
                "def custom_loop(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        positions: List[float],\n"
                "        num: int = 3):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n"
                "    for pos in positions:\n"
                "        yield from bps.mv(motor, pos)\n"
                "        yield from bp.count(detectors, num=num)\n"
            ),
        }
        if name in templates:
            self.editor.setPlainText(templates[name])
            self.tmpl_combo.setCurrentIndex(0)

    def _upload_script(self):
        script = self.editor.toPlainText().strip()
        if not script:
            QMessageBox.warning(self, "Empty", "Write a plan before uploading.")
            return
        ok_syntax, err_msg, lineno = self._syntax_check(self.editor.toPlainText())
        if not ok_syntax:
            self._jump_to_line(lineno)
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] ✗ Upload blocked — {err_msg}")
            return

        ts = datetime.now().strftime("%H:%M:%S")
        if not self.worker or not self.worker.rm:
            self.output.appendPlainText(f"[{ts}] ✗ Not connected to RE Manager")
            return

        self.output.appendPlainText(f"[{ts}] Uploading to RE Manager…")
        ok, msg = self.worker.upload_script(script)
        ts = datetime.now().strftime("%H:%M:%S")
        if ok:
            self.output.appendPlainText(
                f"[{ts}] ✓ Plan injected into running session\n"
                f"  (session-only — save to a remote file to persist across restarts)")
            QTimer.singleShot(500, self.worker.reload_plans_devices)
        else:
            self.output.appendPlainText(f"[{ts}] ✗ Upload failed:")
            for line in (msg or "unknown error").splitlines():
                self.output.appendPlainText(f"  {line}")

    def _open_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Plan", str(Path.home()), "Python files (*.py);;All files (*)")
        if path:
            self._open_local_file(path)

    def _save_script(self):
        ts = datetime.now().strftime("%H:%M:%S")
        if self._file_tier == "remote" and self._file_name:
            self._save_remote(self.editor.toPlainText())
        elif self._file_tier == "local" and self._file_name:
            self._save_local(self.editor.toPlainText())
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Plan", str(Path.home()), "Python files (*.py)")
            if path:
                try:
                    Path(path).write_text(self.editor.toPlainText(), encoding="utf-8")
                    self._file_tier = "local"
                    self._file_name = path
                    self._dirty = False
                    self._editor_file_lbl.setText(Path(path).name)
                    self.output.appendPlainText(f"[{ts}] ✓ Saved to {path}")
                except Exception as e:
                    self.output.appendPlainText(f"[{ts}] ✗ Save failed: {e}")

    def _reload_environment(self):
        r = QMessageBox.question(
            self, "Reload Environment",
            "Close and reopen the RE environment?\n"
            "This reloads all startup scripts including your uploaded plan.")
        if r == QMessageBox.StandardButton.Yes:
            self.worker.close_environment()
            QTimer.singleShot(2000, self.worker.open_environment)
            self.output.appendPlainText("Reloading environment…")
            self._env_reload_attempts = 0
            QTimer.singleShot(4000, self._poll_for_env_ready)

    def _poll_for_env_ready(self):
        """Check RE status after reload; refresh plans once environment is idle."""
        self._env_reload_attempts += 1
        try:
            status = self.worker.rm.status() if self.worker.rm else {}
            env_state = status.get("worker_environment_state", "")
            if env_state == "idle":
                self.worker.reload_plans_devices()
                ts = datetime.now().strftime("%H:%M:%S")
                self.output.appendPlainText(f"[{ts}] ✓ Environment ready — plan list refreshed")
                return
        except Exception:
            pass
        # Retry up to 15 times (every 2 s → up to 30 s total)
        if self._env_reload_attempts < 15:
            QTimer.singleShot(2000, self._poll_for_env_ready)
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] ⚠ Environment not ready after 30 s — click Reconnect to refresh plans")

    def _on_send_to_editor(self, code: str):
        self.editor.setPlainText(code)
        self._tabs.setCurrentIndex(self._editor_tab_index)

    # ── File tree ──────────────────────────────────────────────────────────────

    def set_profile(self, conn_settings: dict) -> None:
        from .connection_settings import get_active_profile
        self._conn_settings = conn_settings
        self._profile_name  = get_active_profile(conn_settings).get("name", "")
        self._file_panel.set_profile(conn_settings)

    def clear_profile(self) -> None:
        self._conn_settings = {}
        self._profile_name  = ""
        self._file_panel.clear_profile()

    def _on_file_panel_open(self, tier: str, name_or_path: str) -> None:
        """Check unsaved state then open the file in the code editor."""
        if self._dirty:
            label = (Path(name_or_path).name if tier == "local" else name_or_path)
            r = QMessageBox.question(
                self, "Unsaved Changes",
                f"Discard unsaved changes to '{self._file_name or 'buffer'}' "
                f"and open '{label}'?")
            if r != QMessageBox.StandardButton.Yes:
                return
        if tier == "remote":
            self._open_remote_file(name_or_path)
        else:
            self._open_local_file(name_or_path)

    def open_file(self, tier: str, name_or_path: str) -> None:
        """Open a plan file from an external widget; switches to the code editor."""
        self._tabs.setCurrentIndex(self._editor_tab_index)
        self._on_file_panel_open(tier, name_or_path)

    def _open_remote_file(self, filename: str) -> None:
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileReader(self._conn_settings, profile, filename, self)
        t.result.connect(self._on_remote_file_read)
        t.error.connect(self._on_remote_file_error)
        t.start()
        self._active_threads.append(t)
        self.output.appendPlainText(f"  Loading {filename}…")

    def _on_remote_file_read(self, filename: str, content: str) -> None:
        self.editor.setPlainText(content)
        self._file_tier = "remote"
        self._file_name = filename
        self._dirty = False
        self._editor_file_lbl.setText(filename)
        self._btn_compare.setVisible(False)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] Opened remote:{filename}")

    def _on_remote_file_error(self, filename: str, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] ✗ Cannot open {filename}: {msg}")

    def _open_local_file(self, path: str) -> None:
        try:
            content = Path(path).read_text(encoding="utf-8")
            self.editor.setPlainText(content)
            self._file_tier = "local"
            self._file_name = path
            self._dirty = False
            self._editor_file_lbl.setText(Path(path).name)
            self._update_compare_btn()
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] Opened {path}")
        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] ✗ Cannot open {path}: {e}")

    def _on_remote_names_ready(self, names: set) -> None:
        self._remote_file_names = names
        self._update_compare_btn()

    def _update_compare_btn(self) -> None:
        """Show Compare button only when a local file is open and also exists on remote."""
        if self._file_tier != "local" or not self._file_name:
            self._btn_compare.setVisible(False)
            return
        fn = Path(self._file_name).name
        has_remote = bool(self._conn_settings) and fn in self._remote_file_names
        self._btn_compare.setVisible(has_remote)

    def _compare_with_remote(self) -> None:
        if not self._file_name or self._file_tier != "local":
            return
        fn = Path(self._file_name).name
        local_text = self.editor.toPlainText()
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileReader(self._conn_settings, profile, fn, self)
        t.result.connect(lambda _fn, content: _DiffDialog(
            local_text, content, fn, parent=self).exec())
        t.error.connect(lambda _fn, msg: self.output.appendPlainText(
            f"  ✗ Cannot fetch remote {fn}: {msg}"))
        t.start()
        self._active_threads.append(t)
        self.output.appendPlainText(f"  Fetching remote copy of {fn}…")

    def _on_editor_changed(self) -> None:
        if not self._dirty:
            self._dirty = True
            base = Path(self._file_name).name if self._file_name else ""
            self._editor_file_lbl.setText((base + " *") if base else "*")

    def _save_remote(self, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] Saving {self._file_name} to remote…")
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileSaver(self._conn_settings, profile,
                             self._file_name, content, self)
        t.done.connect(self._on_remote_save_done)
        t.start()
        self._active_threads.append(t)

    def _on_remote_save_done(self, ok: bool, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] {'✓' if ok else '✗'} {msg}")
        if ok:
            self._dirty = False
            self._editor_file_lbl.setText(self._file_name)
            self._file_panel._start_remote_list()
            self.worker.close_environment()
            self.output.appendPlainText(f"[{ts}] ↻ Reloading RE environment…")
            self._env_reload_attempts = 0
            QTimer.singleShot(2000, self.worker.open_environment)
            QTimer.singleShot(4000, self._poll_for_env_ready)

    def _save_local(self, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            Path(self._file_name).write_text(content, encoding="utf-8")
            self._dirty = False
            self._editor_file_lbl.setText(Path(self._file_name).name)
            self.output.appendPlainText(f"[{ts}] ✓ Saved to {self._file_name}")
        except Exception as e:
            self.output.appendPlainText(f"[{ts}] ✗ Save failed: {e}")
            return
        if self.worker and self.worker.rm:
            ok, msg = self.worker.upload_script(content)
            self.output.appendPlainText(
                f"[{ts}] {'✓ Uploaded' if ok else '✗ Upload failed'}: {msg}")

    def reupload_local_plans(self, _attempt: int = 0) -> None:
        """Re-upload all local user-dir plan files via script_upload.

        Called after pv_names_ready fires (env is guaranteed idle at that point).
        Retries up to 3 times on transient busy errors.
        """
        if not self.worker or not self.worker.rm:
            return
        from .plans_manager import get_user_dirs, get_catalog, PLAN_TYPE_SESSION
        scripts = []
        paths   = []
        for d in get_user_dirs(self._profile_name):
            p = Path(d)
            if p.is_dir():
                for f in sorted(p.glob("*.py")):
                    try:
                        code = f.read_text(encoding="utf-8")
                        scripts.append(code)
                        paths.append((str(f), code))
                    except Exception:
                        pass
        if not scripts:
            return
        if _attempt == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            n = len(scripts)
            self.output.appendPlainText(f"[{ts}] ↻ Uploading {n} local plan file(s)…")
            self.plans_uploading.emit(
                f"uploading {n} plan file{'s' if n > 1 else ''}")
        results = self.worker.upload_scripts(scripts)
        catalog = get_catalog()
        n_ok    = 0
        busy    = False
        # dir_path → [(fname, short_msg), ...]
        failed: dict = {}
        _TRANSIENT = ("not idle", "environment is not open", "executing_task",
                      "must be in idle")
        for (path, code), (ok, msg) in zip(paths, results):
            fname    = Path(path).name
            dir_path = str(Path(path).parent)
            if ok:
                n_ok += 1
                self.output.appendPlainText(f"  ✓ {fname}")
                if catalog:
                    catalog.register_code(code, path, PLAN_TYPE_SESSION)
            elif any(t in msg.lower() for t in _TRANSIENT):
                busy = True
            elif msg != "skipped":
                self.output.appendPlainText(f"  ✗ {fname}: {msg}")
                failed.setdefault(dir_path, []).append((fname, msg))

        if n_ok:
            self.worker.reload_plans_devices()

        if failed and _attempt == 0:
            QTimer.singleShot(0, lambda: self._prompt_remove_bad_plans(failed))

        if busy and _attempt < 3:
            delay = (1 + _attempt) * 2000
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(
                f"  [busy] RE Manager not idle — retry #{_attempt + 1} in {delay // 1000} s")
            QTimer.singleShot(delay, lambda: self.reupload_local_plans(_attempt + 1))

    def _prompt_remove_bad_plans(self, failed: dict) -> None:
        """Show one dialog per folder that had upload errors, offering removal."""
        from qtpy.QtWidgets import QMessageBox, QPushButton
        for dir_path, errors in failed.items():
            folder_name = Path(dir_path).name
            detail = "\n".join(f"• {f}:\n  {m[:200]}" for f, m in errors)

            mb = QMessageBox(self.window())
            mb.setWindowTitle("Plan Files Failed to Load")
            mb.setIcon(QMessageBox.Icon.Warning)
            mb.setText(
                f"<b>{len(errors)} file(s)</b> in folder "
                f"<i>{folder_name}</i> could not be loaded into the RE Manager."
            )
            mb.setInformativeText(
                "This usually means the file uses packages that are not "
                "installed in the remote conda environment, or it is not a "
                "valid Bluesky plan file.\n\n"
                "Remove this folder from Plan Files so the app does not try "
                "to reload these files on every startup?"
            )
            mb.setDetailedText(detail)
            remove_btn = mb.addButton("Remove Folder from Plan Files",
                                      QMessageBox.ButtonRole.DestructiveRole)
            mb.addButton("Keep (I will fix the files manually)",
                         QMessageBox.ButtonRole.RejectRole)
            mb.exec()

            if mb.clickedButton() is remove_btn:
                self._on_local_plans_removed(dir_path)

    def _on_local_plans_added(self) -> None:
        """Close and reopen the RE environment so the new plan files are
        uploaded via reupload_local_plans() on the next env_opened cycle."""
        if not self.worker or not self.worker.rm:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(
            f"[{ts}] Plan folder added — restarting environment to load new plans…")
        self.worker.env_closed.connect(self._reopen_env_after_close)
        self.worker.close_environment()

    def _on_local_plans_removed(self, dir_path: str) -> None:
        """Remove catalog entries for session plans from the removed folder,
        then close and reopen the RE environment to unload them from the Worker.
        """
        from .plans_manager import get_catalog, PLAN_TYPE_SESSION
        import ast as _ast
        catalog = get_catalog()
        if catalog:
            p = Path(dir_path)
            for f in p.glob("*.py"):
                try:
                    code = f.read_text(encoding="utf-8")
                    tree = _ast.parse(code)
                    for node in _ast.walk(tree):
                        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                            if catalog.get_type(node.name) == PLAN_TYPE_SESSION:
                                catalog._types.pop(node.name, None)
                                catalog._sources.pop(node.name, None)
                except Exception:
                    pass

        if not self.worker or not self.worker.rm:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(
            f"[{ts}] Closing RE environment to unload removed plans…")
        # Reopen as soon as the env_closed signal fires, then env_opened will
        # trigger reupload_local_plans for any remaining local-folder plans.
        self.worker.env_closed.connect(self._reopen_env_after_close)
        self.worker.close_environment()

    def _reopen_env_after_close(self) -> None:
        self.worker.env_closed.disconnect(self._reopen_env_after_close)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] Reopening RE environment…")
        self.worker.open_environment()

    # ── Public update slots ────────────────────────────────────────────────────

    def update_plans(self, plans: dict):
        self.plans = plans
        self._composer.set_plans(plans)
        if getattr(self, "_blockly", None) is not None:
            self._blockly.set_plans(plans)

    def update_devices(self, devices: dict):
        self.devices = devices
        self._composer.set_devices(devices)
        if getattr(self, "_blockly", None) is not None:
            self._blockly.set_devices(devices)
