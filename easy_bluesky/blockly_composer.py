"""blockly_composer.py — Scratch-style block composer built on Google Blockly.

Blockly runs inside a QWebEngineView (assets in ``blockly_web/``).  Custom
bluesky blocks generate ``yield from bps.*`` plan code; the Qt side wraps the
generated body in imports + a function signature (same parameter editor and
"Send to Code Editor" flow as the classic Visual Composer).

Requires ``PyQt6-WebEngine`` — import errors propagate so the caller can show a hint.
"""

import json
from pathlib import Path

from qtpy.QtCore import QEvent, QObject, QTimer, QUrl, Signal, Slot, Qt
from qtpy.QtGui import QFont
from qtpy.QtWebChannel import QWebChannel
from qtpy.QtWebEngineWidgets import QWebEngineView
from qtpy.QtWebEngineCore import QWebEngineSettings
from qtpy.QtWidgets import (
    QApplication, QDialog, QFileDialog, QMenu, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget, QFrame,
)

from .highlighter import PythonHighlighter
from .plan_builder import _ParamEditor, assemble_plan_code

_WEB_DIR = Path(__file__).parent / "blockly_web"


class _Bridge(QObject):
    """Receives callbacks from the Blockly page via QWebChannel."""
    changed = Signal(str, str)   # workspace JSON, generated python body
    ready = Signal()
    code_edit_requested = Signal(str, str)   # block id, current code

    @Slot()
    def onReady(self):
        self.ready.emit()

    @Slot(str, str)
    def editCode(self, block_id: str, code: str):
        self.code_edit_requested.emit(block_id, code)

    @Slot(str, str)
    def onChanged(self, workspace_json: str, body: str):
        self.changed.emit(workspace_json, body)


class BlocklyComposerWidget(QWidget):
    send_to_editor = Signal(str)

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self._devices: dict = {}
        self._plans: dict = {}
        self._body = ""
        self._ready = False
        self._pending_js: list = []
        self._build()
        self._load_example_aswaxs()   # opening example, loaded once the page is ready

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.addWidget(QLabel("Plan name:"))
        self._plan_name = QLineEdit("blockly_plan")
        self._plan_name.setFixedWidth(200)
        self._plan_name.textChanged.connect(self._update_preview)
        top.addWidget(self._plan_name)
        top.addSpacing(12)
        for text, slot, tip in (
            ("💾 Save .ebplan", self._save, "Save the block workspace"),
            ("📂 Load .ebplan", self._load, "Load a Blockly workspace"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            top.addWidget(b)
        btn_ex = QPushButton("★ Examples")
        btn_ex.setToolTip("Load an example plan")
        menu = QMenu(btn_ex)
        menu.addAction("ASWAXS energy scan", self._load_example_aswaxs)
        menu.addAction("Paired sx/sy positions + statistics", self._load_example_pairs)
        btn_ex.setMenu(menu)
        top.addWidget(btn_ex)
        btn_clear = QPushButton("🗑 Clear")
        btn_clear.setToolTip("Remove all blocks")
        btn_clear.clicked.connect(self._clear)
        top.addWidget(btn_clear)
        top.addStretch()
        root.addLayout(top)

        self._param_editor = _ParamEditor()
        self._param_editor.changed.connect(self._update_preview)
        root.addWidget(self._param_editor)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: palette(mid);")
        root.addWidget(sep)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._view = QWebEngineView()
        self._bridge = _Bridge()
        self._bridge.ready.connect(self._on_ready)
        self._bridge.changed.connect(self._on_changed)
        self._bridge.code_edit_requested.connect(self._edit_code)
        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        s = self._view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self._view.load(QUrl.fromLocalFile(str(_WEB_DIR / "index.html")))
        splitter.addWidget(self._view)

        prev = QWidget()
        pl = QVBoxLayout(prev)
        pl.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("CODE PREVIEW")
        lbl.setObjectName("section_title")
        pl.addWidget(lbl)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setFont(QFont("Courier New", 11))
        PythonHighlighter(self._preview.document())
        pl.addWidget(self._preview)
        splitter.addWidget(prev)
        splitter.setSizes([760, 380])
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        hint = QLabel("Blockly blocks — drag from the toolbox; blocks snap together.")
        hint.setStyleSheet("color: palette(mid);")
        bottom.addWidget(hint)
        bottom.addStretch()
        btn = QPushButton("→ Send to Code Editor")
        btn.setStyleSheet("font-weight: bold; padding: 4px 12px;")
        btn.clicked.connect(lambda: self.send_to_editor.emit(self._full_code()))
        bottom.addWidget(btn)
        root.addLayout(bottom)

    # ── JS plumbing ────────────────────────────────────────────────────────────
    def _js(self, code: str):
        if self._ready:
            self._view.page().runJavaScript(code)
        else:
            self._pending_js.append(code)

    def _on_ready(self):
        self._ready = True
        self._push_devices()
        self._push_colors()
        for code in self._pending_js:
            self._view.page().runJavaScript(code)
        self._pending_js.clear()
        self._js("EB.resize()")

    def _edit_code(self, block_id: str, code: str):
        dlg = QDialog(self)
        dlg.setWindowTitle("Python block")
        dlg.resize(560, 340)
        lay = QVBoxLayout(dlg)
        editor = QPlainTextEdit()
        editor.setFont(QFont("Courier New", 12))
        editor.setPlainText(code)
        PythonHighlighter(editor.document())
        lay.addWidget(editor)
        row = QHBoxLayout()
        ok, cancel = QPushButton("OK"), QPushButton("Cancel")
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        row.addStretch()
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)
        if dlg.exec():
            self._js(f"EB.setCode({json.dumps(block_id)}, {json.dumps(editor.toPlainText())})")

    def _on_changed(self, workspace_json: str, body: str):
        self._body = body
        self._update_preview()

    def _push_colors(self):
        pal = QApplication.palette()
        c = {
            "bg": pal.window().color().name(),
            "panel": pal.alternateBase().color().name(),
            "fg": pal.windowText().color().name(),
            "scroll": pal.mid().color().name(),
        }
        self._js(f"EB.setColors({json.dumps(json.dumps(c))})")

    def _push_devices(self):
        motors = sorted(n for n, d in self._devices.items()
                        if isinstance(d, dict) and d.get("is_movable"))
        data = {"motors": motors, "all": sorted(self._devices),
                "plans": sorted(self._plans)}
        self._js(f"EB.setDevices({json.dumps(json.dumps(data))})")

    def changeEvent(self, ev):
        if ev.type() in (QEvent.Type.ApplicationPaletteChange, QEvent.Type.PaletteChange):
            if self._ready:
                QTimer.singleShot(50, self._push_colors)
        super().changeEvent(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._ready:
            QTimer.singleShot(50, lambda: self._js("EB.resize()"))

    # ── Code ───────────────────────────────────────────────────────────────────
    def _full_code(self) -> str:
        lines = self._body.rstrip().splitlines()
        body = "\n".join(("    " + ln) if ln.strip() else "" for ln in lines) or "    pass"
        return assemble_plan_code(
            body, self._plan_name.text() or "my_plan",
            self._param_editor.get_params(), "PeakStats(" in self._body)

    def _update_preview(self):
        self._preview.setPlainText(self._full_code())

    # ── File / actions ─────────────────────────────────────────────────────────
    def _load_example(self, filename: str, plan_name: str, params: list):
        path = _WEB_DIR / "examples" / filename
        self._plan_name.setText(plan_name)
        self._param_editor.set_params(params)
        self._js(f"EB.load({json.dumps(path.read_text(encoding='utf-8'))})")

    def _load_example_aswaxs(self):
        self._load_example("aswaxs_energy_scan.json", "aswaxs_energy_scan",
                           _ParamEditor._ASWAXS_PARAMS)

    def _load_example_pairs(self):
        self._load_example("aswaxs_pairs.json", "aswaxs_pair_scan", [])

    def _clear(self):
        r = QMessageBox.question(self, "Clear", "Remove all blocks?")
        if r == QMessageBox.StandardButton.Yes:
            self._js("EB.clear()")

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plan", str(Path.home()), "EasyBluesky Plan (*.ebplan)")
        if not path:
            return
        if not path.endswith(".ebplan"):
            path += ".ebplan"

        def _write(ws_json):
            data = {
                "format": "blockly",
                "plan_name": self._plan_name.text(),
                "params": self._param_editor.get_params(),
                "workspace": json.loads(ws_json),
            }
            try:
                Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception as e:
                QMessageBox.warning(self, "Save Error", str(e))

        self._view.page().runJavaScript("EB.save()", _write)

    def _load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Plan", str(Path.home()), "EasyBluesky Plan (*.ebplan)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "Load Error", str(e))
            return
        if data.get("format") != "blockly":
            QMessageBox.information(
                self, "Different plan type",
                "This .ebplan was made with the classic Visual Composer.\n"
                "Open it in the 'Visual Composer' tab.")
            return
        self._plan_name.setText(data.get("plan_name", "blockly_plan"))
        self._param_editor.set_params(data.get("params", []))
        self._js(f"EB.load({json.dumps(json.dumps(data.get('workspace', {})))})")

    # ── Device / plan lists ────────────────────────────────────────────────────
    def set_devices(self, devices: dict):
        self._devices = devices or {}
        self._push_devices()

    def set_plans(self, plans: dict):
        self._plans = plans or {}
        self._push_devices()
