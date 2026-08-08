"""code_editor.py — Python code editor with find/replace and jedi completion.

Uses QsciScintilla when PyQtScintilla is installed (pip install PyQtScintilla).
Falls back to a QPlainTextEdit-based editor with Tab/Shift+Tab indent if not.
"""

import threading

try:
    import jedi
    JEDI_AVAILABLE = True
except ImportError:
    JEDI_AVAILABLE = False

try:
    from PyQt6.Qsci import QsciScintilla, QsciLexerPython, QsciAPIs
    QSCI_AVAILABLE = True
except ImportError:
    QSCI_AVAILABLE = False

from PyQt6.QtWidgets import (
    QPlainTextEdit, QTextEdit, QWidget, QLineEdit, QLabel, QPushButton, QCheckBox,
    QHBoxLayout, QVBoxLayout,
)
from PyQt6.QtCore import Qt, QEvent, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QKeyEvent, QPalette,
    QTextCursor, QTextCharFormat,
)


# ── Static completion word lists ───────────────────────────────────────────────

_KEYWORDS = [
    "False", "None", "True", "and", "as", "assert", "async", "await",
    "break", "class", "continue", "def", "del", "elif", "else", "except",
    "finally", "for", "from", "global", "if", "import", "in", "is",
    "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
    "while", "with", "yield",
]

_BPS_METHODS = [
    "mv", "mvr", "sleep", "trigger_and_read", "abs_set", "rel_set",
    "read", "stage", "unstage", "move_per_step", "kickoff", "complete",
    "collect", "configure", "checkpoint", "clear_checkpoint",
    "open_run", "close_run", "create", "save", "monitor", "unmonitor",
    "null", "stop", "wait", "pause",
]

_BP_METHODS = [
    "scan", "rel_scan", "count", "grid_scan", "rel_grid_scan",
    "list_scan", "log_scan", "spiral", "spiral_fermat",
    "adaptive_scan", "tune_centroid", "fly",
]

_BLUESKY_GLOBALS = [
    "bps", "bp",
    "import bluesky.plans as bp",
    "import bluesky.plan_stubs as bps",
    "yield from bps.",
    "yield from bp.",
]

_ALL_WORDS = sorted(set(
    _KEYWORDS + _BLUESKY_GLOBALS +
    [f"bps.{m}" for m in _BPS_METHODS] +
    [f"bp.{m}" for m in _BP_METHODS]
))


# ── Jedi background worker ─────────────────────────────────────────────────────

def _jedi_thread(source: str, line: int, col: int, prefix: str,
                 cancel: threading.Event, signal):
    """Run jedi.Script.complete() off the main thread and emit results via signal."""
    try:
        script = jedi.Script(source)
        if cancel.is_set():
            return
        completions = script.complete(line, col)
        if cancel.is_set():
            return
        words = [c.name for c in completions][:120]
        if words and not cancel.is_set():
            signal.emit(words, prefix)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# QScintilla implementation — used when PyQtScintilla is installed
# ══════════════════════════════════════════════════════════════════════════════

if QSCI_AVAILABLE:

    class FindBar(QWidget):
        """Floating find/replace bar overlaid on a QsciScintilla editor.

        Ctrl+F focuses search; Ctrl+R shows replace. Esc closes the bar.
        """

        _IND_ALL = 8    # indicator: all matches (amber outline)
        _IND_CUR = 9    # indicator: current match (orange outline)
        _NO_MATCH_STYLE = "background: #6b2020; color: #ffffff;"

        def __init__(self, editor: "CodeEditor"):
            super().__init__(editor)
            self._editor = editor
            self._results: list = []
            self._current: int  = -1
            self._build()
            self._setup_indicators()
            self.adjustSize()
            self._reposition()
            self.hide()
            editor.installEventFilter(self)

        def _build(self):
            self.setAutoFillBackground(True)
            self.setStyleSheet("""
                FindBar {
                    background: palette(window);
                    border: 1px solid palette(mid);
                    border-radius: 4px;
                }
                QLineEdit {
                    padding: 2px 4px;
                    border: 1px solid palette(mid);
                    border-radius: 3px;
                    min-width: 160px;
                }
                QPushButton {
                    padding: 2px 8px;
                    border: 1px solid palette(mid);
                    border-radius: 3px;
                    min-width: 24px;
                }
                QPushButton:hover {
                    background: palette(highlight);
                    color: palette(highlighted-text);
                }
            """)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(6, 5, 6, 5)
            outer.setSpacing(4)

            row1 = QHBoxLayout()
            row1.setSpacing(4)
            self._search = QLineEdit()
            self._search.setPlaceholderText("Find…")
            self._search.textChanged.connect(self._on_text_changed)
            self._search.returnPressed.connect(self._next)
            row1.addWidget(self._search)

            self._count = QLabel("0 / 0")
            self._count.setFixedWidth(52)
            self._count.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._count.setStyleSheet("font-size: 11px; color: palette(placeholder-text);")
            row1.addWidget(self._count)

            for text, slot, tip in (
                ("▲", self._prev, "Previous match  (Shift+Enter)"),
                ("▼", self._next, "Next match  (Enter)"),
            ):
                b = QPushButton(text)
                b.setFixedWidth(28)
                b.setToolTip(tip)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.clicked.connect(slot)
                row1.addWidget(b)

            self._case_cb = QCheckBox("Aa")
            self._case_cb.setToolTip("Match case")
            self._case_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._case_cb.toggled.connect(self._on_text_changed)
            row1.addWidget(self._case_cb)

            close_btn = QPushButton("✕")
            close_btn.setFixedWidth(28)
            close_btn.setToolTip("Close  (Esc)")
            close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            close_btn.clicked.connect(self.hide_bar)
            row1.addWidget(close_btn)
            outer.addLayout(row1)

            row2 = QHBoxLayout()
            row2.setSpacing(4)
            self._replace = QLineEdit()
            self._replace.setPlaceholderText("Replace with…")
            row2.addWidget(self._replace)
            for text, slot in (("Replace", self._replace_one), ("All", self._replace_all)):
                b = QPushButton(text)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.clicked.connect(slot)
                row2.addWidget(b)
            self._replace_row = QWidget()
            self._replace_row.setLayout(row2)
            self._replace_row.hide()
            outer.addWidget(self._replace_row)

        def _setup_indicators(self):
            ed = self._editor
            try:
                all_style = QsciScintilla.IndicatorStyle.RoundBoxIndicator
                cur_style = QsciScintilla.IndicatorStyle.BoxIndicator
            except AttributeError:
                all_style = 7   # INDIC_ROUNDBOX
                cur_style = 6   # INDIC_BOX
            ed.indicatorDefine(all_style, self._IND_ALL)
            ed.setIndicatorForegroundColor(QColor("#ffa500"), self._IND_ALL)
            ed.indicatorDefine(cur_style, self._IND_CUR)
            ed.setIndicatorForegroundColor(QColor("#ff4400"), self._IND_CUR)

        def _clear_indicators(self, ind: int):
            ed = self._editor
            n = ed.lines()
            if n == 0:
                return
            last = n - 1
            last_len = len(ed.text(last).rstrip('\r\n'))
            ed.clearIndicatorRange(0, 0, last, last_len, ind)

        def _reposition(self):
            vp  = self._editor.viewport()
            geo = vp.geometry()
            w   = min(420, max(280, geo.width() - 20))
            self.setFixedWidth(w)
            self.adjustSize()
            self.move(geo.right() - self.width() - 4, geo.top() + 4)
            self.raise_()

        def show_search(self):
            self._replace_row.hide()
            self.adjustSize()
            self._reposition()
            self.show()
            self._search.setFocus()
            self._search.selectAll()

        def show_replace(self):
            self._replace_row.show()
            self.adjustSize()
            self._reposition()
            self.show()
            self._search.setFocus()
            self._search.selectAll()

        def hide_bar(self):
            self.hide()
            self._clear_indicators(self._IND_ALL)
            self._clear_indicators(self._IND_CUR)
            self._editor.setFocus()

        def eventFilter(self, obj, event):
            if obj is not self._editor:
                return False
            if event.type() == QEvent.Type.Resize:
                if self.isVisible():
                    self._reposition()
                return False
            if event.type() != QEvent.Type.KeyPress:
                return False
            mod  = event.modifiers()
            key  = event.key()
            ctrl = mod == Qt.KeyboardModifier.ControlModifier
            if ctrl and key == Qt.Key.Key_F:
                self.show_search()
                return True
            if ctrl and key == Qt.Key.Key_R and not self._editor.isReadOnly():
                self.show_replace()
                return True
            return False

        def keyPressEvent(self, event: QKeyEvent):
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.hide_bar()
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._prev()
                else:
                    self._next()
            else:
                super().keyPressEvent(event)

        def _on_text_changed(self):
            text = self._search.text()
            self._results = []
            self._current = -1
            self._clear_indicators(self._IND_ALL)
            self._clear_indicators(self._IND_CUR)

            if not text:
                self._count.setText("")
                self._search.setStyleSheet("")
                return

            ed = self._editor
            cs = self._case_cb.isChecked()
            saved_line, saved_col = ed.getCursorPosition()

            found = ed.findFirst(text, False, cs, False, False, True, 0, 0)
            seen: set = set()
            while found:
                sel = ed.getSelection()
                key = (sel[0], sel[1])
                if key in seen:
                    break
                seen.add(key)
                self._results.append(sel)
                ed.fillIndicatorRange(sel[0], sel[1], sel[2], sel[3], self._IND_ALL)
                found = ed.findNext()

            ed.setCursorPosition(saved_line, saved_col)

            if self._results:
                self._search.setStyleSheet("")
                self._current = 0
                self._highlight_current()
                self._scroll_to_current()
            else:
                self._search.setStyleSheet(self._NO_MATCH_STYLE)

            self._update_count()

        def _highlight_current(self):
            self._clear_indicators(self._IND_CUR)
            if 0 <= self._current < len(self._results):
                lf, idx_f, lt, idx_t = self._results[self._current]
                self._editor.fillIndicatorRange(lf, idx_f, lt, idx_t, self._IND_CUR)

        def _scroll_to_current(self):
            if 0 <= self._current < len(self._results):
                lf, idx_f, lt, idx_t = self._results[self._current]
                self._editor.setSelection(lf, idx_f, lt, idx_t)
                self._editor.ensureLineVisible(lf)

        def _next(self):
            if not self._results:
                return
            self._current = (self._current + 1) % len(self._results)
            self._highlight_current()
            self._scroll_to_current()
            self._update_count()

        def _prev(self):
            if not self._results:
                return
            self._current = (self._current - 1) % len(self._results)
            self._highlight_current()
            self._scroll_to_current()
            self._update_count()

        def _update_count(self):
            if not self._results:
                self._count.setText("0 / 0")
            else:
                self._count.setText(f"{self._current + 1} / {len(self._results)}")

        def _replace_one(self):
            if not (0 <= self._current < len(self._results)):
                return
            lf, idx_f, lt, idx_t = self._results[self._current]
            self._editor.setSelection(lf, idx_f, lt, idx_t)
            self._editor.replaceSelectedText(self._replace.text())
            self._on_text_changed()

        def _replace_all(self):
            if not self._results:
                return
            replacement = self._replace.text()
            ed = self._editor
            ed.beginUndoAction()
            for lf, idx_f, lt, idx_t in reversed(self._results):
                ed.setSelection(lf, idx_f, lt, idx_t)
                ed.replaceSelectedText(replacement)
            ed.endUndoAction()
            self._on_text_changed()

    # ── QScintilla-based CodeEditor ────────────────────────────────────────────

    class CodeEditor(QsciScintilla):
        """QsciScintilla-based Python editor: syntax highlighting, line numbers,
        code folding, brace matching, Tab/Shift+Tab indent, jedi completion,
        Ctrl+/ comment toggle, Ctrl+F find / Ctrl+R replace."""

        _jedi_done = pyqtSignal(list, str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._jedi_cancel = threading.Event()
            self._setup_editor()
            self._setup_lexer()
            self._setup_margins()
            self._setup_completer()
            self._find_bar = FindBar(self)
            self._jedi_timer = QTimer(self)
            self._jedi_timer.setSingleShot(True)
            self._jedi_timer.setInterval(300)
            self._jedi_timer.timeout.connect(self._start_jedi_async)
            self._jedi_done.connect(self._apply_jedi_completions)

        def _setup_editor(self):
            self.setIndentationsUseTabs(False)
            self.setTabWidth(4)
            self.setAutoIndent(True)
            self.setBackspaceUnindents(True)
            self.setTabIndents(True)
            self.setCaretLineVisible(True)
            self.setCaretLineBackgroundColor(
                self.palette().color(QPalette.ColorRole.AlternateBase)
            )
            try:
                self.setBraceMatching(QsciScintilla.BraceMatch.SloppyBraceMatch)
            except AttributeError:
                self.setBraceMatching(QsciScintilla.SloppyBraceMatch)
            try:
                self.setFolding(QsciScintilla.FoldStyle.BoxedTreeFoldStyle)
            except AttributeError:
                self.setFolding(QsciScintilla.BoxedTreeFoldStyle)

        def _setup_lexer(self):
            self._lexer = QsciLexerPython(self)
            self._lexer.setDefaultFont(self.font())
            self.setLexer(self._lexer)

        def _setup_margins(self):
            try:
                num_margin = QsciScintilla.MarginType.NumberMargin
                sym_margin = QsciScintilla.MarginType.SymbolMargin
            except AttributeError:
                num_margin = 1
                sym_margin = 0
            self.setMarginType(0, num_margin)
            self.setMarginWidth(0, "0000")
            self.setMarginsForegroundColor(QColor("#888888"))
            self.setMarginsBackgroundColor(
                self.palette().color(QPalette.ColorRole.AlternateBase)
            )
            self.setMarginType(1, sym_margin)
            self.setMarginWidth(1, 12)
            self.setMarginSensitivity(1, True)

        def _setup_completer(self):
            self._api = QsciAPIs(self._lexer)
            for word in _ALL_WORDS:
                self._api.add(word)
            self._api.prepare()
            try:
                acs_apis = QsciScintilla.AutoCompletionSource.AcsAPIs
            except AttributeError:
                acs_apis = QsciScintilla.AcsAPIs
            self.setAutoCompletionSource(acs_apis)
            self.setAutoCompletionThreshold(2)
            self.setAutoCompletionCaseSensitivity(True)
            self.setAutoCompletionReplaceWord(True)
            if JEDI_AVAILABLE:
                self._api.apiPreparationFinished.connect(self._on_api_ready)

        def _on_api_ready(self):
            prefix = self._completion_prefix()
            if len(prefix) >= 2:
                self.autoCompleteFromAPIs()

        def setFont(self, font: QFont):
            super().setFont(font)
            if hasattr(self, '_lexer'):
                self._lexer.setDefaultFont(font)
                self.setMarginsFont(font)

        def keyPressEvent(self, event: QKeyEvent):
            if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                    and event.key() == Qt.Key.Key_Slash):
                self._toggle_comment()
                return

            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.isReadOnly():
                line, _ = self.getCursorPosition()
                ends_colon = self.text(line).rstrip('\r\n').rstrip().endswith(':')
                super().keyPressEvent(event)
                if ends_colon:
                    cur_line, cur_col = self.getCursorPosition()
                    self.insert('    ')
                    self.setCursorPosition(cur_line, cur_col + 4)
                return

            super().keyPressEvent(event)

            if JEDI_AVAILABLE and event.text() and not self.isReadOnly():
                self._jedi_timer.start()

        def _toggle_comment(self):
            lf, idx_f, lt, idx_t = self.getSelection()
            cur_line, _ = self.getCursorPosition()
            if lf == -1:
                lf = lt = cur_line
            elif idx_t == 0 and lt > lf:
                lt -= 1
            lines_text = [self.text(i).rstrip('\r\n') for i in range(lf, lt + 1)]
            non_empty = [t for t in lines_text if t.strip()]
            all_commented = bool(non_empty) and all(
                t.lstrip().startswith('#') for t in non_empty
            )
            self.beginUndoAction()
            for line_num in range(lf, lt + 1):
                text = self.text(line_num).rstrip('\r\n')
                if all_commented:
                    stripped = text.lstrip()
                    indent = len(text) - len(stripped)
                    if stripped.startswith('# '):
                        self.setSelection(line_num, indent, line_num, indent + 2)
                        self.removeSelectedText()
                    elif stripped.startswith('#'):
                        self.setSelection(line_num, indent, line_num, indent + 1)
                        self.removeSelectedText()
                else:
                    if text.strip():
                        indent = len(text) - len(text.lstrip())
                        self.insertAt('# ', line_num, indent)
            self.endUndoAction()

        def _completion_prefix(self) -> str:
            line, col = self.getCursorPosition()
            text = self.text(line)[:col]
            start = len(text)
            while start > 0 and text[start - 1] in (
                'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.'
            ):
                start -= 1
            return text[start:]

        def _start_jedi_async(self):
            self._jedi_cancel.set()
            self._jedi_cancel = threading.Event()
            source = self.text()
            line, col = self.getCursorPosition()
            prefix = self._completion_prefix()
            cancel = self._jedi_cancel
            signal = self._jedi_done
            threading.Thread(
                target=_jedi_thread,
                args=(source, line + 1, col, prefix, cancel, signal),
                daemon=True,
            ).start()

        def _apply_jedi_completions(self, words: list, prefix: str):
            if not words or self._completion_prefix() != prefix:
                return
            self._api.clear()
            for w in sorted(set(_ALL_WORDS + words)):
                self._api.add(w)
            self._api.prepare()

        def set_word_list(self, words: list):
            """Replace the autocomplete word list (e.g. to add ophyd device names)."""
            self._api.clear()
            for w in words:
                self._api.add(w)
            self._api.prepare()

        # ── QPlainTextEdit compatibility shims ─────────────────────────────────
        def toPlainText(self) -> str:
            return self.text()

        def setPlainText(self, text: str):
            self.setText(text)

        def setPlaceholderText(self, _text: str):
            pass   # not supported by QScintilla

        def ensureCursorVisible(self):
            line, _ = self.getCursorPosition()
            self.ensureLineVisible(line)


# ══════════════════════════════════════════════════════════════════════════════
# Fallback implementation — used when PyQtScintilla is NOT installed
# ══════════════════════════════════════════════════════════════════════════════

else:

    class CodeEditor(QPlainTextEdit):
        """Fallback code editor (PyQtScintilla not installed).

        Provides Tab/Shift+Tab multi-line indent, Ctrl+/ comment toggle,
        auto-indent after ':', and Ctrl+F find.  Install PyQtScintilla for
        full syntax highlighting, code folding, and jedi autocomplete:
            pip install PyQtScintilla
        """

        _jedi_done = pyqtSignal(list, str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._jedi_cancel = threading.Event()
            self.setFont(QFont("Courier New", 11))
            self.cursorPositionChanged.connect(self._highlight_line)
            self._highlight_line()

        def _highlight_line(self):
            if self.isReadOnly():
                self.setExtraSelections([])
                return
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(self.palette().color(QPalette.ColorRole.AlternateBase))
            sel.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            self.setExtraSelections([sel])

        def keyPressEvent(self, event: QKeyEvent):
            # Ctrl+/ — comment toggle
            if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                    and event.key() == Qt.Key.Key_Slash):
                self._toggle_comment()
                return

            # Tab — indent selection or insert spaces at cursor
            if event.key() == Qt.Key.Key_Tab:
                cursor = self.textCursor()
                if cursor.hasSelection():
                    doc = self.document()
                    s_block = doc.findBlock(cursor.selectionStart()).blockNumber()
                    e_block = doc.findBlock(cursor.selectionEnd()).blockNumber()
                    if s_block != e_block:
                        self._indent_blocks(cursor, dedent=False)
                        return
                col = cursor.positionInBlock()
                cursor.insertText(' ' * (4 - col % 4))
                return

            # Shift+Tab — dedent selection or current line
            if event.key() == Qt.Key.Key_Backtab:
                self._indent_blocks(self.textCursor(), dedent=True)
                return

            # Enter — auto-indent, extra level after ':'
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                cursor = self.textCursor()
                line = cursor.block().text()
                indent = len(line) - len(line.lstrip(' '))
                extra = '    ' if line.rstrip().endswith(':') else ''
                cursor.insertText('\n' + ' ' * indent + extra)
                self.setTextCursor(cursor)
                return

            super().keyPressEvent(event)

        def _indent_blocks(self, cursor: QTextCursor, dedent: bool):
            doc = self.document()
            sel_start = cursor.selectionStart()
            sel_end   = cursor.selectionEnd()
            start_block = doc.findBlock(sel_start)
            end_block   = doc.findBlock(sel_end)
            if sel_end == end_block.position() and end_block != start_block:
                end_block = end_block.previous()
            cursor.beginEditBlock()
            b = start_block
            while b.isValid():
                bc = QTextCursor(b)
                if dedent:
                    text   = b.text()
                    spaces = len(text) - len(text.lstrip(' '))
                    remove = min(4, spaces)
                    if remove:
                        bc.movePosition(
                            QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.KeepAnchor, remove,
                        )
                        bc.removeSelectedText()
                else:
                    bc.insertText('    ')
                if b == end_block:
                    break
                b = b.next()
            cursor.endEditBlock()

        def _toggle_comment(self):
            cursor = self.textCursor()
            doc = self.document()
            sel_start = cursor.selectionStart()
            sel_end   = cursor.selectionEnd()
            start_block = doc.findBlock(sel_start)
            end_block   = doc.findBlock(sel_end)
            if sel_end == end_block.position() and end_block != start_block:
                end_block = end_block.previous()
            blocks = []
            b = start_block
            while b.isValid():
                blocks.append(b)
                if b == end_block:
                    break
                b = b.next()
            non_empty = [b.text() for b in blocks if b.text().strip()]
            all_commented = bool(non_empty) and all(
                t.lstrip().startswith('#') for t in non_empty
            )
            cursor.beginEditBlock()
            for block in blocks:
                text = block.text()
                bc   = QTextCursor(block)
                if all_commented:
                    stripped = text.lstrip()
                    if stripped.startswith('# '):
                        idx = text.index('#')
                        bc.setPosition(block.position() + idx)
                        bc.movePosition(
                            QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.KeepAnchor, 2,
                        )
                        bc.removeSelectedText()
                    elif stripped.startswith('#'):
                        idx = text.index('#')
                        bc.setPosition(block.position() + idx)
                        bc.movePosition(
                            QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.KeepAnchor, 1,
                        )
                        bc.removeSelectedText()
                else:
                    if text.strip():
                        indent = len(text) - len(text.lstrip())
                        bc.setPosition(block.position() + indent)
                        bc.insertText('# ')
            cursor.endEditBlock()

        # ── QScintilla-compatible API used by plan_builder / devices_editor ────

        def set_word_list(self, words: list):
            pass   # no autocomplete in fallback mode

        def setCursorPosition(self, line: int, col: int):
            block = self.document().findBlockByNumber(line)
            if block.isValid():
                cursor = QTextCursor(block)
                cursor.movePosition(
                    QTextCursor.MoveOperation.Right,
                    QTextCursor.MoveMode.MoveAnchor,
                    min(col, block.length() - 1),
                )
                self.setTextCursor(cursor)

        def ensureLineVisible(self, line: int):
            block = self.document().findBlockByNumber(line)
            if block.isValid():
                self.setTextCursor(QTextCursor(block))
                self.ensureCursorVisible()

        def getCursorPosition(self) -> tuple:
            c = self.textCursor()
            return c.blockNumber(), c.positionInBlock()

        def text(self, line: int = None) -> str:
            if line is None:
                return self.toPlainText()
            block = self.document().findBlockByNumber(line)
            return block.text() if block.isValid() else ''

        def lines(self) -> int:
            return self.document().blockCount()
