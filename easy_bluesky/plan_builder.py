"""plan_builder.py — Visual Plan Builder tab widget."""

import json
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea, QPlainTextEdit,
    QComboBox, QLineEdit, QAbstractItemView, QMessageBox, QMenu,
    QFileDialog,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from .highlighter import PythonHighlighter
from .widgets import ParamForm

class PlanBuilder(QWidget):
    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker  = worker
        self.plans   = {}
        self.devices = {}
        self._build()

    def _build(self):
        main = QHBoxLayout(self)
        main.setSpacing(0)
        main.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: Step palette ─────────────────────────────────────────────────
        palette = QWidget()
        play    = QVBoxLayout(palette)
        play.setContentsMargins(8, 8, 8, 8)

        lbl = QLabel("PLAN STEPS")
        lbl.setObjectName("section_title")
        play.addWidget(lbl)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search plans...")
        self.search_box.textChanged.connect(self._filter_palette)
        play.addWidget(self.search_box)

        self.palette_list = QListWidget()
        self.palette_list.setDragEnabled(True)
        self.palette_list.setToolTip("Double-click or drag to canvas to add")
        self.palette_list.itemDoubleClicked.connect(self._add_step_from_palette)
        play.addWidget(self.palette_list, 1)

        splitter.addWidget(palette)

        # ── Middle: Visual canvas ──────────────────────────────────────────────
        canvas_w = QWidget()
        cplay    = QVBoxLayout(canvas_w)
        cplay.setContentsMargins(8, 8, 8, 8)

        lbl2 = QLabel("VISUAL PLAN CANVAS")
        lbl2.setObjectName("section_title")
        cplay.addWidget(lbl2)

        self.canvas = QListWidget()
        self.canvas.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.canvas.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.canvas.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(self._canvas_context_menu)
        self.canvas.currentItemChanged.connect(self._on_canvas_selection)
        self.canvas.setMinimumWidth(280)
        cplay.addWidget(self.canvas, 1)

        # Canvas controls
        c_btns = QHBoxLayout()
        btn_up   = QPushButton("↑ Up")
        btn_dn   = QPushButton("↓ Down")
        btn_del  = QPushButton("Remove")
        btn_clr  = QPushButton("Clear")
        btn_up.clicked.connect(self._step_up)
        btn_dn.clicked.connect(self._step_down)
        btn_del.clicked.connect(self._remove_step)
        btn_clr.clicked.connect(self._clear_canvas)
        c_btns.addWidget(btn_up)
        c_btns.addWidget(btn_dn)
        c_btns.addWidget(btn_del)
        c_btns.addStretch()
        c_btns.addWidget(btn_clr)
        cplay.addLayout(c_btns)

        # Step param editor
        lbl3 = QLabel("STEP PARAMETERS")
        lbl3.setObjectName("section_title")
        cplay.addWidget(lbl3)

        self.step_scroll = QScrollArea()
        self.step_scroll.setWidgetResizable(True)
        self.step_scroll.setMaximumHeight(280)
        self.step_scroll.setWidget(QLabel("Select a step to edit parameters"))
        cplay.addWidget(self.step_scroll)

        splitter.addWidget(canvas_w)

        # ── Right: Code editor ─────────────────────────────────────────────────
        editor_w = QWidget()
        elay     = QVBoxLayout(editor_w)
        elay.setContentsMargins(8, 8, 8, 8)

        lbl4 = QLabel("CODE EDITOR")
        lbl4.setObjectName("section_title")
        elay.addWidget(lbl4)

        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Courier New", 13))
        self.editor.setPlaceholderText(
            "# Write a custom plan here\n"
            "# It will be uploaded to the RE Manager\n\n"
            "def my_plan(detector, motor, start, stop, num):\n"
            "    yield from bp.scan([detector], motor, start, stop, num)\n"
        )
        self.highlighter = PythonHighlighter(self.editor.document())
        elay.addWidget(self.editor, 1)

        # Template selector
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self.tmpl_combo = QComboBox()
        self.tmpl_combo.addItems([
            "-- select --",
            "Simple scan",
            "Grid scan",
            "Count",
            "Move and count",
            "Custom loop",
            "Multi-detector scan",
        ])
        self.tmpl_combo.currentTextChanged.connect(self._insert_template)
        tmpl_row.addWidget(self.tmpl_combo, 1)
        elay.addLayout(tmpl_row)

        # Editor buttons
        e_btns = QHBoxLayout()
        btn_gen    = QPushButton("← Generate from canvas")
        btn_upload = QPushButton("⬆ Upload to RE")
        btn_save   = QPushButton("💾 Save to file")
        btn_reload = QPushButton("↺ Reload RE env")
        btn_upload.setObjectName("btn_primary")
        btn_reload.setObjectName("btn_warning")

        btn_gen.clicked.connect(self._generate_from_canvas)
        btn_upload.clicked.connect(self._upload_script)
        btn_save.clicked.connect(self._save_script)
        btn_reload.clicked.connect(self._reload_environment)

        e_btns.addWidget(btn_gen)
        e_btns.addWidget(btn_upload)
        e_btns.addWidget(btn_save)
        e_btns.addWidget(btn_reload)
        elay.addLayout(e_btns)

        # Output log
        lbl5 = QLabel("OUTPUT")
        lbl5.setObjectName("section_title")
        elay.addWidget(lbl5)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(120)
        self.output.setFont(QFont("Courier New", 11))
        elay.addWidget(self.output)

        splitter.addWidget(editor_w)
        splitter.setSizes([180, 280, 380])
        main.addWidget(splitter)

    def _filter_palette(self, text):
        for i in range(self.palette_list.count()):
            item = self.palette_list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def _add_step_from_palette(self, item):
        plan_name = item.text()
        self._add_to_canvas(plan_name, {})

    def _add_to_canvas(self, plan_name, params):
        li = QListWidgetItem(f"▶  {plan_name}")
        li.setData(Qt.ItemDataRole.UserRole,     plan_name)
        li.setData(Qt.ItemDataRole.UserRole + 1, params)
        self.canvas.addItem(li)

    def _on_canvas_selection(self, current, previous):
        if not current:
            return
        plan_name = current.data(Qt.ItemDataRole.UserRole)
        if plan_name not in self.plans:
            return
        params = self.plans[plan_name].get("parameters", [])
        form   = ParamForm(params, self.devices)
        self.step_scroll.setWidget(form)
        self._current_form = form

    def _canvas_context_menu(self, pos):
        item = self.canvas.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.addAction("Remove", self._remove_step)
        menu.addAction("Move up",   self._step_up)
        menu.addAction("Move down", self._step_down)
        menu.exec(self.canvas.viewport().mapToGlobal(pos))

    def _step_up(self):
        row = self.canvas.currentRow()
        if row > 0:
            item = self.canvas.takeItem(row)
            self.canvas.insertItem(row-1, item)
            self.canvas.setCurrentRow(row-1)

    def _step_down(self):
        row = self.canvas.currentRow()
        if row < self.canvas.count()-1:
            item = self.canvas.takeItem(row)
            self.canvas.insertItem(row+1, item)
            self.canvas.setCurrentRow(row+1)

    def _remove_step(self):
        row = self.canvas.currentRow()
        if row >= 0:
            self.canvas.takeItem(row)

    def _clear_canvas(self):
        self.canvas.clear()

    def _generate_from_canvas(self):
        """Generate Python code from the visual canvas."""
        lines = [
            "import bluesky.plans as bp",
            "import bluesky.plan_stubs as bps",
            "",
            "def custom_plan():",
        ]
        if self.canvas.count() == 0:
            lines.append("    pass")
        else:
            for i in range(self.canvas.count()):
                item      = self.canvas.item(i)
                plan_name = item.data(Qt.ItemDataRole.UserRole)
                lines.append(f"    # Step {i+1}: {plan_name}")
                lines.append(f"    yield from bp.{plan_name}()")
                lines.append("")
        self.editor.setPlainText("\n".join(lines))

    def _insert_template(self, name):
        templates = {
            "Simple scan": (
                "import bluesky.plans as bp\n\n"
                "def my_scan(detector, motor, start, stop, num):\n"
                "    \"\"\"\n"
                "    Simple 1D scan.\n"
                "    \"\"\"\n"
                "    yield from bp.scan([detector], motor, start, stop, num)\n"
            ),
            "Grid scan": (
                "import bluesky.plans as bp\n\n"
                "def my_grid_scan(detector, motor1, start1, stop1, num1,\n"
                "                 motor2, start2, stop2, num2):\n"
                "    \"\"\"\n"
                "    2D grid scan over two motors.\n"
                "    \"\"\"\n"
                "    yield from bp.grid_scan(\n"
                "        [detector],\n"
                "        motor1, start1, stop1, num1,\n"
                "        motor2, start2, stop2, num2,\n"
                "    )\n"
            ),
            "Count": (
                "import bluesky.plans as bp\n\n"
                "def my_count(detector, num=10, delay=0.1):\n"
                "    \"\"\"\n"
                "    Count detector readings.\n"
                "    \"\"\"\n"
                "    yield from bp.count([detector], num=num, delay=delay)\n"
            ),
            "Move and count": (
                "import bluesky.plans as bp\n"
                "import bluesky.plan_stubs as bps\n\n"
                "def move_and_count(detector, motor, position, num=5):\n"
                "    \"\"\"\n"
                "    Move motor to position then count.\n"
                "    \"\"\"\n"
                "    yield from bps.mv(motor, position)\n"
                "    yield from bp.count([detector], num=num)\n"
            ),
            "Custom loop": (
                "import bluesky.plans as bp\n"
                "import bluesky.plan_stubs as bps\n\n"
                "def custom_loop(detector, motor, positions):\n"
                "    \"\"\"\n"
                "    Visit a list of positions and count at each.\n"
                "    positions: list of float values\n"
                "    \"\"\"\n"
                "    for pos in positions:\n"
                "        yield from bps.mv(motor, pos)\n"
                "        yield from bp.count([detector], num=3)\n"
            ),
            "Multi-detector scan": (
                "import bluesky.plans as bp\n\n"
                "def multi_det_scan(detectors, motor, start, stop, num):\n"
                "    \"\"\"\n"
                "    Scan with multiple detectors simultaneously.\n"
                "    detectors: list of detector objects\n"
                "    \"\"\"\n"
                "    yield from bp.scan(detectors, motor, start, stop, num)\n"
            ),
        }
        if name in templates:
            self.editor.setPlainText(templates[name])
            self.tmpl_combo.setCurrentIndex(0)

    def _upload_script(self):
        script = self.editor.toPlainText().strip()
        if not script:
            QMessageBox.warning(self, "Empty", "Write a plan before uploading")
            return
        ok, msg = self.worker.upload_script(script)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(
            f"[{ts}] {'✓ Uploaded' if ok else '✗ Failed'}: {msg}"
        )

    def _save_script(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plan", str(Path.home()), "Python files (*.py)")
        if path:
            Path(path).write_text(self.editor.toPlainText())
            self.output.appendPlainText(f"Saved to {path}")

    def _reload_environment(self):
        r = QMessageBox.question(
            self, "Reload Environment",
            "Close and reopen the RE environment?\n"
            "This will reload all startup scripts including your uploaded plan.")
        if r == QMessageBox.StandardButton.Yes:
            self.worker.close_environment()
            QTimer.singleShot(2000, self.worker.open_environment)
            self.output.appendPlainText("Reloading environment...")

    def update_plans(self, plans):
        self.plans = plans
        self.palette_list.clear()
        for name in sorted(plans.keys()):
            desc = plans[name].get("description", "")
            item = QListWidgetItem(name)
            item.setToolTip(desc[:200] if desc else name)
            self.palette_list.addItem(item)

    def update_devices(self, devices):
        self.devices = devices
