"""plan_builder.py — Plan Composer: visual sequence builder + code editor."""

import uuid
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QPlainTextEdit, QComboBox, QLineEdit, QMessageBox,
    QFormLayout, QDoubleSpinBox, QSpinBox, QFrame, QScrollArea, QTabWidget,
    QFileDialog,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor

from .highlighter import PythonHighlighter
from .code_editor import CodeEditor
from .widgets import ParamForm


# ── RE Console Monitor Dialog ──────────────────────────────────────────────────

# ── Block type registry ────────────────────────────────────────────────────────

BLOCK_DEFS = {
    "move": {
        "label": "Move", "category": "Motion", "icon": "→",
        "params": [
            {"name": "device",   "type": "str",   "default": "", "hint": "motor name"},
            {"name": "position", "type": "float", "default": 0.0, "hint": "target position"},
        ],
    },
    "rel_move": {
        "label": "Relative Move", "category": "Motion", "icon": "↔",
        "params": [
            {"name": "device", "type": "str",   "default": "", "hint": "motor name"},
            {"name": "delta",  "type": "float", "default": 0.0, "hint": "relative distance"},
        ],
    },
    "sleep": {
        "label": "Sleep", "category": "Timing", "icon": "⏱",
        "params": [
            {"name": "seconds", "type": "float", "default": 1.0, "hint": "delay in seconds"},
        ],
    },
    "set_attr": {
        "label": "Set Attribute", "category": "Device", "icon": "⚙",
        "params": [
            {"name": "device",    "type": "str", "default": "", "hint": "device name"},
            {"name": "attribute", "type": "str", "default": "", "hint": "e.g. cam.acquire_time"},
            {"name": "value",     "type": "str", "default": "", "hint": "value to set"},
        ],
    },
    "set_exposure": {
        "label": "Set Exposure", "category": "Detector", "icon": "⏲",
        "params": [
            {"name": "detectors",     "type": "str",   "default": "",  "hint": "comma-separated detector names"},
            {"name": "exposure_attr", "type": "str",   "default": "cam.acquire_time", "hint": "attribute path on each detector"},
            {"name": "exposure_time", "type": "float", "default": 1.0, "hint": "exposure time in seconds"},
        ],
    },
    "set_file": {
        "label": "Set AD File", "category": "Detector", "icon": "🗂",
        "params": [
            {"name": "detector",   "type": "str", "default": "",         "hint": "AreaDetector device name"},
            {"name": "plugin",     "type": "str", "default": "hdf1",     "hint": "file plugin (hdf1, tiff1, etc.)"},
            {"name": "file_path",  "type": "str", "default": "/data/",   "hint": "save directory"},
            {"name": "file_name",  "type": "str", "default": "scan",     "hint": "file name prefix"},
        ],
    },
    "stage": {
        "label": "Stage Device", "category": "Device", "icon": "▲",
        "params": [
            {"name": "device", "type": "str", "default": "", "hint": "device to stage"},
        ],
    },
    "unstage": {
        "label": "Unstage Device", "category": "Device", "icon": "▼",
        "params": [
            {"name": "device", "type": "str", "default": "", "hint": "device to unstage"},
        ],
    },
    "open_shutter": {
        "label": "Open Shutter", "category": "Shutter", "icon": "◉",
        "params": [
            {"name": "shutter", "type": "str", "default": "", "hint": "shutter device name"},
        ],
    },
    "close_shutter": {
        "label": "Close Shutter", "category": "Shutter", "icon": "○",
        "params": [
            {"name": "shutter", "type": "str", "default": "", "hint": "shutter device name"},
        ],
    },
    "trigger_read": {
        "label": "Trigger & Read", "category": "Detector", "icon": "📷",
        "params": [
            {"name": "detectors", "type": "str", "default": "",
             "hint": "comma-separated detector names"},
        ],
    },
    "scan": {
        "label": "Scan", "category": "Plans", "icon": "⟳",
        "params": [
            {"name": "detectors", "type": "str",   "default": "", "hint": "comma-separated detectors"},
            {"name": "motor",     "type": "str",   "default": "", "hint": "motor name"},
            {"name": "start",     "type": "float", "default": 0.0, "hint": "start position"},
            {"name": "stop",      "type": "float", "default": 1.0, "hint": "stop position"},
            {"name": "num",       "type": "int",   "default": 11,  "hint": "number of points"},
        ],
    },
    "count": {
        "label": "Count", "category": "Plans", "icon": "●",
        "params": [
            {"name": "detectors", "type": "str",   "default": "", "hint": "comma-separated detectors"},
            {"name": "num",       "type": "int",   "default": 1,  "hint": "number of acquisitions"},
            {"name": "delay",     "type": "float", "default": 0.0, "hint": "delay between acquisitions"},
        ],
    },
    "plan_stub": {
        "label": "Plan Stub", "category": "Plans", "icon": "⋯",
        "params": [
            {"name": "stub_name", "type": "str", "default": "", "hint": "e.g. bps.abs_set"},
            {"name": "args",      "type": "str", "default": "", "hint": "comma-separated args"},
        ],
    },
}

_PLAN_BLOCKS = {"scan", "count"}   # blocks that support per-step injection

_CATEGORY_ORDER = ["Motion", "Timing", "Detector", "Shutter", "Device", "Plans"]

# Convenience groups shown as workflow hints in the palette tooltip
_WORKFLOW_HINT = (
    "Typical scan workflow:\n"
    "  Main:     Set Exposure → Set AD File → Scan\n"
    "  Per-step: Open Shutter → Trigger & Read → Close Shutter → Sleep"
)


# ── Block helpers ──────────────────────────────────────────────────────────────

def _new_block(btype: str) -> dict:
    defn = BLOCK_DEFS[btype]
    return {
        "id":     str(uuid.uuid4())[:8],
        "type":   btype,
        "params": {p["name"]: p["default"] for p in defn["params"]},
    }


def _block_summary(block: dict) -> str:
    defn = BLOCK_DEFS[block["type"]]
    p = block["params"]
    icon = defn["icon"]
    name = defn["label"]
    btype = block["type"]
    if btype == "move":
        return f"{icon}  Move  {p['device']} → {p['position']}"
    if btype == "rel_move":
        return f"{icon}  Rel Move  {p['device']} ±{p['delta']}"
    if btype == "sleep":
        return f"{icon}  Sleep  {p['seconds']} s"
    if btype == "set_attr":
        return f"{icon}  Set  {p['device']}.{p['attribute']} = {p['value']}"
    if btype == "open_shutter":
        return f"{icon}  Open  {p['shutter']}"
    if btype == "close_shutter":
        return f"{icon}  Close  {p['shutter']}"
    if btype == "set_exposure":
        return f"{icon}  Set Exposure  [{p['detectors']}]  {p['exposure_attr']}={p['exposure_time']}s"
    if btype == "set_file":
        return f"{icon}  Set AD File  {p['detector']}.{p['plugin']}  {p['file_path']}{p['file_name']}"
    if btype == "stage":
        return f"{icon}  Stage  {p['device']}"
    if btype == "unstage":
        return f"{icon}  Unstage  {p['device']}"
    if btype == "trigger_read":
        return f"{icon}  Trigger & Read  [{p['detectors']}]"
    if btype == "scan":
        return f"{icon}  Scan  {p['motor']}  {p['start']}→{p['stop']}  ×{p['num']}"
    if btype == "count":
        return f"{icon}  Count  [{p['detectors']}]  ×{p['num']}"
    if btype == "plan_stub":
        return f"{icon}  {p['stub_name']}({p['args']})"
    return f"{icon}  {name}"


def _block_to_code(block: dict, indent: int = 4, per_step_name: str = None) -> str:
    p = block["params"]
    pad = " " * indent
    btype = block["type"]
    if btype == "move":
        return f"{pad}yield from bps.mv({p['device']}, {p['position']})"
    if btype == "rel_move":
        return f"{pad}yield from bps.mvr({p['device']}, {p['delta']})"
    if btype == "sleep":
        return f"{pad}yield from bps.sleep({p['seconds']})"
    if btype == "set_attr":
        return f"{pad}yield from bps.mv({p['device']}.{p['attribute']}, {p['value']})"
    if btype == "open_shutter":
        return f"{pad}yield from bps.mv({p['shutter']}, 'open')"
    if btype == "close_shutter":
        return f"{pad}yield from bps.mv({p['shutter']}, 'closed')"
    if btype == "set_exposure":
        dets = [d.strip() for d in p["detectors"].split(",") if d.strip()]
        lines = [f"{pad}yield from bps.mv({d}.{p['exposure_attr']}, {p['exposure_time']})"
                 for d in dets]
        return "\n".join(lines) if lines else f"{pad}pass  # no detectors specified"
    if btype == "set_file":
        det, plug = p["detector"], p["plugin"]
        return (
            f"{pad}yield from bps.abs_set({det}.{plug}.file_path, '{p['file_path']}', wait=True)\n"
            f"{pad}yield from bps.abs_set({det}.{plug}.file_name, '{p['file_name']}', wait=True)"
        )
    if btype == "stage":
        return f"{pad}yield from bps.stage({p['device']})"
    if btype == "unstage":
        return f"{pad}yield from bps.unstage({p['device']})"
    if btype == "trigger_read":
        return f"{pad}yield from bps.trigger_and_read([{p['detectors']}])"
    if btype == "scan":
        ps_arg = f", per_step={per_step_name}" if per_step_name else ""
        return (f"{pad}yield from bp.scan([{p['detectors']}], {p['motor']}, "
                f"{p['start']}, {p['stop']}, {p['num']}{ps_arg})")
    if btype == "count":
        ps_arg = f", per_step={per_step_name}" if per_step_name else ""
        return (f"{pad}yield from bp.count([{p['detectors']}], "
                f"num={p['num']}, delay={p['delay']}{ps_arg})")
    if btype == "plan_stub":
        return f"{pad}yield from {p['stub_name']}({p['args']})"
    return f"{pad}pass  # unknown: {btype}"


# ── Sequence list widget ───────────────────────────────────────────────────────

class SequenceList(QListWidget):
    """Drag-to-reorder list of plan blocks. Each item stores a block dict."""
    block_selected = pyqtSignal(object)   # emits block dict or None
    sequence_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMinimumHeight(120)
        self.currentItemChanged.connect(self._on_selection)
        self.model().rowsMoved.connect(lambda *_: self.sequence_changed.emit())

    def add_block(self, block: dict):
        item = QListWidgetItem(self._make_label(block))
        item.setData(Qt.ItemDataRole.UserRole, block)
        if block["type"] in _PLAN_BLOCKS:
            item.setForeground(QColor("#1f77b4"))
            f = QFont(); f.setBold(True)
            item.setFont(f)
        self.addItem(item)
        self.setCurrentItem(item)
        self.sequence_changed.emit()

    def remove_selected(self):
        row = self.currentRow()
        if row >= 0:
            self.takeItem(row)
            self.sequence_changed.emit()

    def get_blocks(self) -> list:
        return [self.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.count())]

    def refresh_labels(self):
        for i in range(self.count()):
            item = self.item(i)
            block = item.data(Qt.ItemDataRole.UserRole)
            if block:
                item.setText(self._make_label(block))

    def _make_label(self, block: dict) -> str:
        return _block_summary(block)

    def _on_selection(self, current, _prev):
        self.block_selected.emit(
            current.data(Qt.ItemDataRole.UserRole) if current else None
        )

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.remove_selected()
        else:
            super().keyPressEvent(event)


# ── Property panel ─────────────────────────────────────────────────────────────

class PropertyPanel(QWidget):
    """Dynamic parameter form for the selected block."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._block = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self._title = QLabel("Select a block to edit")
        self._title.setObjectName("section_title")
        lay.addWidget(self._title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._form.setHorizontalSpacing(12)
        self._form.setVerticalSpacing(6)
        scroll.setWidget(self._form_host)
        lay.addWidget(scroll, 1)

    def load_block(self, block):
        self._block = block
        while self._form.rowCount():
            self._form.removeRow(0)

        if not block:
            self._title.setText("Select a block to edit")
            return

        defn = BLOCK_DEFS[block["type"]]
        self._title.setText(f"{defn['icon']}  {defn['label']}")

        for param in defn["params"]:
            name  = param["name"]
            ptype = param["type"]
            value = block["params"].get(name, param["default"])
            hint  = param.get("hint", "")

            if ptype == "float":
                w = QDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(4)
                w.setValue(float(value))
                w.setSingleStep(0.1)
                w.valueChanged.connect(lambda v, n=name: self._update(n, v))
            elif ptype == "int":
                w = QSpinBox()
                w.setRange(1, 1000000)
                w.setValue(int(value))
                w.valueChanged.connect(lambda v, n=name: self._update(n, v))
            else:
                w = QLineEdit(str(value))
                w.setPlaceholderText(hint)
                w.textChanged.connect(lambda v, n=name: self._update(n, v))

            label = name.replace("_", " ").title() + ":"
            self._form.addRow(label, w)

    def _update(self, name, value):
        if self._block:
            self._block["params"][name] = value
            self.changed.emit()


# ── Code generation ────────────────────────────────────────────────────────────

def generate_plan_code(main_blocks: list, ps_blocks: list, plan_name: str = "") -> tuple:
    """Return (code_str, plan_name)."""
    import re
    name = re.sub(r"[^a-zA-Z0-9_]", "_", plan_name.strip()) if plan_name.strip() else ""
    if not name:
        name = f"composed_plan_{datetime.now().strftime('%H%M%S')}"

    lines = [
        "import bluesky.plans as bp",
        "import bluesky.plan_stubs as bps",
        "",
        f"def {plan_name}():",
    ]

    if not main_blocks:
        lines.append("    pass")
        return "\n".join(lines), plan_name

    has_ps = bool(ps_blocks)

    for block in main_blocks:
        if block["type"] in _PLAN_BLOCKS and has_ps:
            ps_name = "_per_step"
            lines.append(f"    def {ps_name}(detectors, step, pos_cache):")
            lines.append(f"        yield from bps.move_per_step(step, pos_cache)")
            for ps in ps_blocks:
                lines.append(_block_to_code(ps, indent=8))
            lines.append("")
            lines.append(_block_to_code(block, indent=4, per_step_name=ps_name))
        else:
            lines.append(_block_to_code(block, indent=4))

    return "\n".join(lines), plan_name


# ── Composer widget ────────────────────────────────────────────────────────────

class ComposerWidget(QWidget):
    """Three-panel visual plan composer."""
    send_to_editor = pyqtSignal(str)   # emits generated code

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self._active_seq = None   # which sequence last had focus
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_palette())
        splitter.addWidget(self._build_sequences())
        splitter.addWidget(self._build_properties())
        splitter.setSizes([190, 440, 280])
        lay.addWidget(splitter)

        # Connect property panel (built last)
        self._main_seq.block_selected.connect(self._props.load_block)
        self._main_seq.block_selected.connect(lambda _: self._update_preview())
        self._main_seq.sequence_changed.connect(self._update_preview)
        self._perstep_seq.block_selected.connect(self._props.load_block)
        self._perstep_seq.block_selected.connect(lambda _: self._update_preview())
        self._perstep_seq.sequence_changed.connect(self._update_preview)
        self._props.changed.connect(self._on_prop_changed)

        # Track active sequence for palette "add" buttons
        self._main_seq.focusInEvent = lambda e: self._set_active(self._main_seq)
        self._perstep_seq.focusInEvent = lambda e: self._set_active(self._perstep_seq)
        self._active_seq = self._main_seq

    def _set_active(self, seq):
        self._active_seq = seq

    # ── Palette panel ──────────────────────────────────────────────────────────

    def _build_palette(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        lbl = QLabel("BLOCK PALETTE")
        lbl.setObjectName("section_title")
        lay.addWidget(lbl)

        self._palette = QTreeWidget()
        self._palette.setHeaderHidden(True)
        self._palette.setRootIsDecorated(True)
        self._palette.itemDoubleClicked.connect(self._palette_double_clicked)

        cats = {}
        for btype, defn in BLOCK_DEFS.items():
            cats.setdefault(defn["category"], []).append((btype, defn))

        bold = QFont(); bold.setBold(True)
        for cat in _CATEGORY_ORDER:
            if cat not in cats:
                continue
            cat_item = QTreeWidgetItem([cat])
            cat_item.setFont(0, bold)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for btype, defn in cats[cat]:
                child = QTreeWidgetItem([f"{defn['icon']}  {defn['label']}"])
                child.setData(0, Qt.ItemDataRole.UserRole, btype)
                child.setToolTip(0, "Double-click or use buttons below to add")
                cat_item.addChild(child)
            self._palette.addTopLevelItem(cat_item)

        self._palette.expandAll()
        lay.addWidget(self._palette, 1)

        hint = QLabel(_WORKFLOW_HINT)
        hint.setObjectName("dim_text")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 10px; border: 1px solid #444; padding: 4px; border-radius: 3px;")
        lay.addWidget(hint)

        note = QLabel("Double-click or use buttons below:")
        note.setObjectName("dim_text")
        note.setStyleSheet("font-size: 11px;")
        lay.addWidget(note)

        btn_main = QPushButton("Add to Main ↑")
        btn_main.setToolTip("Add selected block to the Main Sequence")
        btn_main.clicked.connect(lambda: self._add_from_palette(self._main_seq))
        lay.addWidget(btn_main)

        btn_ps = QPushButton("Add to Per-Step ↓")
        btn_ps.setToolTip("Add selected block to the Per-Step Sequence")
        btn_ps.clicked.connect(lambda: self._add_from_palette(self._perstep_seq))
        lay.addWidget(btn_ps)

        return w

    def _palette_double_clicked(self, item, col):
        btype = item.data(0, Qt.ItemDataRole.UserRole)
        if btype and self._active_seq:
            self._active_seq.add_block(_new_block(btype))
            self._active_seq.setFocus()

    def _add_from_palette(self, target_seq: SequenceList):
        item = self._palette.currentItem()
        if not item:
            return
        btype = item.data(0, Qt.ItemDataRole.UserRole)
        if btype:
            target_seq.add_block(_new_block(btype))
            target_seq.setFocus()

    # ── Sequences panel ────────────────────────────────────────────────────────

    def _build_sequences(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Plan name field
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Plan name:"))
        self._plan_name_edit = QLineEdit("my_plan")
        self._plan_name_edit.setPlaceholderText("e.g. xrd_scan")
        self._plan_name_edit.textChanged.connect(self._update_preview)
        name_row.addWidget(self._plan_name_edit, 1)
        lay.addLayout(name_row)

        # Main sequence
        lbl_main = QLabel("MAIN SEQUENCE")
        lbl_main.setObjectName("section_title")
        lay.addWidget(lbl_main)

        hint_main = QLabel("Pre-steps, scan/count block, post-steps. Drag to reorder. Del to remove.")
        hint_main.setObjectName("dim_text")
        hint_main.setStyleSheet("font-size: 11px;")
        lay.addWidget(hint_main)

        self._main_seq = SequenceList()
        lay.addWidget(self._main_seq, 3)

        btn_del_main = QPushButton("Remove selected")
        btn_del_main.clicked.connect(self._main_seq.remove_selected)
        lay.addWidget(btn_del_main)

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #444;")
        lay.addWidget(line)

        # Per-step sequence
        lbl_ps = QLabel("PER-STEP SEQUENCE")
        lbl_ps.setObjectName("section_title")
        lay.addWidget(lbl_ps)

        hint_ps = QLabel("Injected at every point of the scan/count above.")
        hint_ps.setObjectName("dim_text")
        hint_ps.setStyleSheet("font-size: 11px;")
        lay.addWidget(hint_ps)

        self._perstep_seq = SequenceList()
        lay.addWidget(self._perstep_seq, 2)

        btn_del_ps = QPushButton("Remove selected")
        btn_del_ps.clicked.connect(self._perstep_seq.remove_selected)
        lay.addWidget(btn_del_ps)

        # Divider
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("color: #444;")
        lay.addWidget(line2)

        # Code preview
        lbl_prev = QLabel("GENERATED CODE")
        lbl_prev.setObjectName("section_title")
        lay.addWidget(lbl_prev)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMaximumHeight(130)
        f = QFont("Courier New", 9)
        self._preview.setFont(f)
        lay.addWidget(self._preview)

        btn_send = QPushButton("→   Send to Code Editor")
        btn_send.setStyleSheet(
            "QPushButton { background: #1f77b4; color: white; "
            "font-weight: bold; padding: 6px; border-radius: 4px; }"
            "QPushButton:hover { background: #2a8fd4; }"
        )
        btn_send.clicked.connect(self._on_send_to_editor)
        lay.addWidget(btn_send)

        return w

    # ── Properties panel ───────────────────────────────────────────────────────

    def _build_properties(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("BLOCK PROPERTIES")
        lbl.setObjectName("section_title")
        lbl.setContentsMargins(8, 8, 8, 4)
        lay.addWidget(lbl)

        self._props = PropertyPanel()
        lay.addWidget(self._props, 1)

        return w

    # ── Updates ────────────────────────────────────────────────────────────────

    def _on_prop_changed(self):
        self._main_seq.refresh_labels()
        self._perstep_seq.refresh_labels()
        self._update_preview()

    def _update_preview(self):
        code, _ = generate_plan_code(
            self._main_seq.get_blocks(),
            self._perstep_seq.get_blocks(),
            self._plan_name_edit.text(),
        )
        self._preview.setPlainText(code)

    # ── Send to editor ─────────────────────────────────────────────────────────

    def _on_send_to_editor(self):
        main_blocks = self._main_seq.get_blocks()
        if not main_blocks:
            QMessageBox.warning(self, "Empty", "Add at least one block to the main sequence.")
            return
        code, _ = generate_plan_code(
            main_blocks, self._perstep_seq.get_blocks(), self._plan_name_edit.text()
        )
        self.send_to_editor.emit(code)

    def set_devices(self, devices: dict):
        pass   # reserved for future autocomplete

    def set_plans(self, plans: dict):
        pass   # reserved for future autocomplete


# ── Main PlanBuilder widget (two tabs) ─────────────────────────────────────────

class PlanBuilder(QWidget):
    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker  = worker
        self.plans   = {}
        self.devices = {}
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        tabs.setTabPosition(QTabWidget.TabPosition.North)

        # Tab 1: Visual Composer
        self._composer = ComposerWidget(worker=self.worker)
        tabs.addTab(self._composer, "🎛  Visual Composer")

        # Tab 2: Code Editor (kept for advanced use)
        self._editor_tab_index = tabs.count()
        tabs.addTab(self._build_code_editor(), "📝  Code Editor")

        self._tabs = tabs
        self._composer.send_to_editor.connect(self._on_send_to_editor)

        lay.addWidget(tabs)

    # ── Code editor tab (unchanged from original) ──────────────────────────────

    def _build_code_editor(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        lbl = QLabel("CODE EDITOR")
        lbl.setObjectName("section_title")
        lay.addWidget(lbl)

        self.editor = CodeEditor()
        self.editor.setFont(QFont("Courier New", 13))
        self.editor.setPlaceholderText(
            "# Write a custom plan here — it will be uploaded to the RE Manager\n\n"
            "def my_plan(detector, motor, start, stop, num):\n"
            "    yield from bp.scan([detector], motor, start, stop, num)\n"
        )
        self.highlighter = PythonHighlighter(self.editor.document())
        lay.addWidget(self.editor, 1)

        # Template selector
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self.tmpl_combo = QComboBox()
        self.tmpl_combo.addItems([
            "-- select --", "Simple scan", "Scan with per-step",
            "Scan with shutter + AD", "Grid scan", "Count",
            "Move and count", "Custom loop",
        ])
        self.tmpl_combo.currentTextChanged.connect(self._insert_template)
        tmpl_row.addWidget(self.tmpl_combo, 1)
        lay.addLayout(tmpl_row)

        e_btns = QHBoxLayout()
        btn_open   = QPushButton("📂  Open file")
        btn_save   = QPushButton("💾  Save to file")
        btn_upload = QPushButton("⬆  Upload to RE Manager")
        btn_reload = QPushButton("↺  Reload RE env")
        btn_open.clicked.connect(self._open_script)
        btn_save.clicked.connect(self._save_script)
        btn_upload.clicked.connect(self._upload_script)
        btn_reload.clicked.connect(self._reload_environment)
        e_btns.addWidget(btn_open)
        e_btns.addWidget(btn_save)
        e_btns.addWidget(btn_upload)
        e_btns.addWidget(btn_reload)
        lay.addLayout(e_btns)

        lbl_out = QLabel("OUTPUT")
        lbl_out.setObjectName("section_title")
        lay.addWidget(lbl_out)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(100)
        self.output.setFont(QFont("Courier New", 11))
        lay.addWidget(self.output)

        return w

    def _insert_template(self, name):
        templates = {
            "Simple scan": (
                "import bluesky.plans as bp\n\n"
                "def my_scan(detector, motor, start, stop, num):\n"
                "    yield from bp.scan([detector], motor, start, stop, num)\n"
            ),
            "Grid scan": (
                "import bluesky.plans as bp\n\n"
                "def my_grid_scan(detector, motor1, s1, e1, n1, motor2, s2, e2, n2):\n"
                "    yield from bp.grid_scan(\n"
                "        [detector], motor1, s1, e1, n1, motor2, s2, e2, n2)\n"
            ),
            "Count": (
                "import bluesky.plans as bp\n\n"
                "def my_count(detector, num=10, delay=0.1):\n"
                "    yield from bp.count([detector], num=num, delay=delay)\n"
            ),
            "Move and count": (
                "import bluesky.plans as bp\nimport bluesky.plan_stubs as bps\n\n"
                "def move_and_count(detector, motor, position, num=5):\n"
                "    yield from bps.mv(motor, position)\n"
                "    yield from bp.count([detector], num=num)\n"
            ),
            "Scan with per-step": (
                "import bluesky.plans as bp\n"
                "import bluesky.plan_stubs as bps\n\n"
                "def scan_with_per_step(\n"
                "        detectors, motor, start, stop, num,\n"
                "        exposure_time=1.0, sleep_time=0.0):\n"
                "    # Set exposure time on all detectors before scan\n"
                "    for det in detectors:\n"
                "        yield from bps.mv(det.cam.acquire_time, exposure_time)\n\n"
                "    def _per_step(detectors, step, pos_cache):\n"
                "        # Move motors to the next position\n"
                "        yield from bps.move_per_step(step, pos_cache)\n"
                "        # Trigger all detectors and read\n"
                "        yield from bps.trigger_and_read(detectors)\n"
                "        # Optional sleep between points\n"
                "        if sleep_time > 0:\n"
                "            yield from bps.sleep(sleep_time)\n\n"
                "    yield from bp.scan(\n"
                "        detectors, motor, start, stop, num,\n"
                "        per_step=_per_step)\n"
            ),
            "Scan with shutter + AD": (
                "import bluesky.plans as bp\n"
                "import bluesky.plan_stubs as bps\n\n"
                "def scan_with_shutter_ad(\n"
                "        detectors, motor, start, stop, num,\n"
                "        shutter,\n"
                "        file_path='/data/', file_name='scan',\n"
                "        exposure_time=1.0, sleep_time=0.5):\n"
                "    # Pre-scan: set exposure time and file paths\n"
                "    for det in detectors:\n"
                "        yield from bps.mv(det.cam.acquire_time, exposure_time)\n"
                "        yield from bps.abs_set(det.hdf1.file_path, file_path, wait=True)\n"
                "        yield from bps.abs_set(det.hdf1.file_name, file_name, wait=True)\n\n"
                "    def _per_step(detectors, step, pos_cache):\n"
                "        # Move to position\n"
                "        yield from bps.move_per_step(step, pos_cache)\n"
                "        # Open shutter\n"
                "        yield from bps.mv(shutter, 'open')\n"
                "        # Trigger all detectors and read\n"
                "        yield from bps.trigger_and_read(detectors)\n"
                "        # Close shutter\n"
                "        yield from bps.mv(shutter, 'closed')\n"
                "        # Sleep between points\n"
                "        yield from bps.sleep(sleep_time)\n\n"
                "    yield from bp.scan(\n"
                "        detectors, motor, start, stop, num,\n"
                "        per_step=_per_step)\n"
            ),
            "Custom loop": (
                "import bluesky.plans as bp\nimport bluesky.plan_stubs as bps\n\n"
                "def custom_loop(detector, motor, positions):\n"
                "    for pos in positions:\n"
                "        yield from bps.mv(motor, pos)\n"
                "        yield from bp.count([detector], num=3)\n"
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
        ok, msg = self.worker.upload_script(script)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] {'✓ Uploaded' if ok else '✗ Failed'}: {msg}")
        if ok:
            self.worker.reload_plans_devices()
            self.output.appendPlainText(f"[{ts}] ↻ Plan list refreshed")

    def _open_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Plan", str(Path.home()), "Python files (*.py);;All files (*)")
        if path:
            self.editor.setPlainText(Path(path).read_text())
            self.output.appendPlainText(f"Opened {path}")

    def _save_script(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plan", str(Path.home()), "Python files (*.py)")
        if path:
            Path(path).write_text(self.editor.toPlainText())
            self.output.appendPlainText(f"Saved to {path}")

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

    # ── Public update slots ────────────────────────────────────────────────────

    def update_plans(self, plans: dict):
        self.plans = plans

    def update_devices(self, devices: dict):
        self.devices = devices
