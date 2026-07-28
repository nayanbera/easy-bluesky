"""code_editor.py — QPlainTextEdit with line numbers, auto-indentation, and auto-completion."""

try:
    import jedi
    JEDI_AVAILABLE = True
except ImportError:
    JEDI_AVAILABLE = False

from PyQt6.QtWidgets import (
    QPlainTextEdit, QCompleter, QAbstractItemView, QWidget, QTextEdit,
    QLineEdit, QLabel, QPushButton, QCheckBox, QHBoxLayout, QVBoxLayout,
)
from PyQt6.QtCore import Qt, QStringListModel, QRect, QSize, QEvent
from PyQt6.QtGui import (
    QTextCursor, QKeyEvent, QFont, QPainter, QColor,
    QTextCharFormat, QPalette, QTextDocument,
)


# ── Line number gutter ─────────────────────────────────────────────────────────

class _LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor._gutter_width(), 0)

    def paintEvent(self, event):
        self._editor._paint_gutter(event)


# ── Find / Replace bar ─────────────────────────────────────────────────────────

class FindBar(QWidget):
    """
    Floating find/replace bar overlaid in the top-right corner of a
    QPlainTextEdit viewport.  Attach to any editor with::

        self._find_bar = FindBar(some_plain_text_edit)

    Ctrl+F focuses the search field; Ctrl+H (editable editors) also shows
    the replace row.  Escape hides the bar and returns focus to the editor.
    """

    _MATCH_BG   = QColor("#b5890044")   # amber — all matches
    _CURRENT_BG = QColor("#ff8800")     # orange — current match
    _NO_MATCH   = "background: #6b2020; color: #ffffff;"
    _NORMAL     = ""

    def __init__(self, editor: QPlainTextEdit):
        super().__init__(editor.viewport())
        self._editor   = editor
        self._matches: list = []
        self._current:  int = -1
        self._build()
        self.adjustSize()
        self._reposition()
        self.hide()
        editor.installEventFilter(self)

    # ── UI construction ────────────────────────────────────────────────────────

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
            QPushButton:hover { background: palette(highlight); color: palette(highlighted-text); }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 5, 6, 5)
        outer.setSpacing(4)

        # ── Search row ──────────────────────────────────────────────────────
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
            b.clicked.connect(slot)
            row1.addWidget(b)

        self._case_cb = QCheckBox("Aa")
        self._case_cb.setToolTip("Match case")
        self._case_cb.toggled.connect(self._on_text_changed)
        row1.addWidget(self._case_cb)

        close_btn = QPushButton("✕")
        close_btn.setFixedWidth(28)
        close_btn.setToolTip("Close  (Esc)")
        close_btn.clicked.connect(self.hide_bar)
        row1.addWidget(close_btn)

        outer.addLayout(row1)

        # ── Replace row (hidden until Ctrl+H) ───────────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(4)

        self._replace = QLineEdit()
        self._replace.setPlaceholderText("Replace with…")
        row2.addWidget(self._replace)

        for text, slot in (("Replace", self._replace_one), ("All", self._replace_all)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row2.addWidget(b)

        self._replace_row = QWidget()
        self._replace_row.setLayout(row2)
        self._replace_row.hide()
        outer.addWidget(self._replace_row)

    # ── Positioning ────────────────────────────────────────────────────────────

    def _reposition(self):
        vp = self._editor.viewport()
        w  = min(420, max(280, vp.width() - 20))
        self.setFixedWidth(w)
        self.adjustSize()
        self.move(vp.width() - self.width() - 4, 4)
        self.raise_()

    # ── Public API ─────────────────────────────────────────────────────────────

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
        self._clear_highlights()
        self._editor.setFocus()

    # ── Event filter — intercepts Ctrl+F / Ctrl+H on the host editor ──────────

    def eventFilter(self, obj, event):
        if obj is not self._editor:
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

    # ── Search logic ───────────────────────────────────────────────────────────

    def _find_flags(self) -> QTextDocument.FindFlag:
        f = QTextDocument.FindFlag(0)
        if self._case_cb.isChecked():
            f |= QTextDocument.FindFlag.FindCaseSensitively
        return f

    def _on_text_changed(self):
        text = self._search.text()
        self._matches.clear()
        self._current = -1

        if not text:
            self._count.setText("")
            self._search.setStyleSheet(self._NORMAL)
            self._clear_highlights()
            return

        doc   = self._editor.document()
        flags = self._find_flags()
        cur   = doc.find(text, 0, flags)
        while not cur.isNull():
            self._matches.append(QTextCursor(cur))
            cur = doc.find(text, cur, flags)

        if self._matches:
            self._current = 0
            self._search.setStyleSheet(self._NORMAL)
        else:
            self._search.setStyleSheet(self._NO_MATCH)

        self._update_highlights()
        self._scroll_to_current()
        self._update_count()

    def _next(self):
        if not self._matches:
            return
        self._current = (self._current + 1) % len(self._matches)
        self._update_highlights()
        self._scroll_to_current()
        self._update_count()

    def _prev(self):
        if not self._matches:
            return
        self._current = (self._current - 1) % len(self._matches)
        self._update_highlights()
        self._scroll_to_current()
        self._update_count()

    def _update_count(self):
        if not self._matches:
            self._count.setText("0 / 0")
        else:
            self._count.setText(f"{self._current + 1} / {len(self._matches)}")

    def _scroll_to_current(self):
        if 0 <= self._current < len(self._matches):
            self._editor.setTextCursor(self._matches[self._current])
            self._editor.ensureCursorVisible()

    # ── Replace ────────────────────────────────────────────────────────────────

    def _replace_one(self):
        if not (0 <= self._current < len(self._matches)):
            return
        cur = self._matches[self._current]
        if cur.hasSelection():
            cur.insertText(self._replace.text())
        self._on_text_changed()

    def _replace_all(self):
        if not self._matches:
            return
        replacement = self._replace.text()
        cursor = self._editor.textCursor()
        cursor.beginEditBlock()
        for cur in reversed(self._matches):
            cur.insertText(replacement)
        cursor.endEditBlock()
        self._on_text_changed()

    # ── Highlight helpers ──────────────────────────────────────────────────────

    def _make_sel(self, cur: QTextCursor, bg: QColor) -> "QTextEdit.ExtraSelection":
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(bg)
        sel.format.setForeground(QColor("#000000"))
        sel.cursor = cur
        return sel

    def _update_highlights(self):
        sels = []
        for i, cur in enumerate(self._matches):
            bg = self._CURRENT_BG if i == self._current else self._MATCH_BG
            sels.append(self._make_sel(cur, bg))
        self._apply_extra_selections(sels)

    def _clear_highlights(self):
        self._apply_extra_selections([])

    def _apply_extra_selections(self, match_sels):
        # Merge with CodeEditor's current-line highlight if present
        line_sel = getattr(self._editor, '_line_sel', None)
        combined = ([line_sel] if line_sel is not None else []) + match_sels
        self._editor.setExtraSelections(combined)
        # Store so CodeEditor._update_extra_selections can include them
        self._editor._match_sels = match_sels


# ── Static word lists ──────────────────────────────────────────────────────────

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


# ── Editor widget ──────────────────────────────────────────────────────────────

class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit with line numbers, current-line highlight, auto-indentation, and auto-completion."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._completer  = None
        self._gutter     = _LineNumberArea(self)
        self._line_sel   = None   # current-line ExtraSelection
        self._match_sels = []     # FindBar match ExtraSelections

        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)

        self._update_gutter_width(0)
        self._highlight_current_line()
        self._setup_completer()
        self._find_bar = FindBar(self)

    # ── Line number gutter ─────────────────────────────────────────────────────

    def _gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 8 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, _=0):
        self.setViewportMargins(self._gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(
            QRect(cr.left(), cr.top(), self._gutter_width(), cr.height())
        )
        if hasattr(self, '_find_bar') and self._find_bar.isVisible():
            self._find_bar._reposition()

    def _paint_gutter(self, event):
        pal = self.palette()
        bg = pal.color(QPalette.ColorRole.AlternateBase)
        fg = pal.color(QPalette.ColorRole.PlaceholderText)

        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), bg)
        painter.setFont(self.font())

        block = self.firstVisibleBlock()
        num   = block.blockNumber()
        top   = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bot   = top + round(self.blockBoundingRect(block).height())
        lh    = self.fontMetrics().height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bot >= event.rect().top():
                painter.setPen(fg)
                painter.drawText(
                    0, top, self._gutter.width() - 4, lh,
                    Qt.AlignmentFlag.AlignRight,
                    str(num + 1),
                )
            block = block.next()
            top   = bot
            bot   = top + round(self.blockBoundingRect(block).height())
            num  += 1

    def _highlight_current_line(self):
        pal = self.palette()
        color = pal.color(QPalette.ColorRole.AlternateBase)
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(color)
        sel.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self._line_sel = sel if not self.isReadOnly() else None
        self._update_extra_selections()

    def _update_extra_selections(self):
        sels = []
        if self._line_sel is not None:
            sels.append(self._line_sel)
        sels.extend(self._match_sels)
        self.setExtraSelections(sels)

    def _setup_completer(self):
        self._completer = QCompleter(self)
        self._completer.setModel(QStringListModel(_ALL_WORDS, self._completer))
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseSensitive)
        self._completer.popup().setFont(QFont("Courier New", 11))
        self._completer.activated.connect(self._insert_completion)

    # ── Key handling ───────────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent):
        c = self._completer
        popup_visible = c and c.popup().isVisible()

        # Let completer consume navigation keys while popup is open
        if popup_visible and event.key() in (
            Qt.Key.Key_Enter, Qt.Key.Key_Return,
            Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
        ):
            event.ignore()
            return

        is_ctrl_space = (
            event.modifiers() == Qt.KeyboardModifier.ControlModifier
            and event.key() == Qt.Key.Key_Space
        )

        # Auto-indent on Enter
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._auto_indent()
            self._maybe_hide_completer()
            return

        # Tab → 4 spaces
        if event.key() == Qt.Key.Key_Tab:
            self._insert_tab_spaces()
            return

        # Smart backspace — remove one indent level
        if event.key() == Qt.Key.Key_Backspace:
            if self._smart_backspace():
                self._maybe_update_completer(is_ctrl_space)
                return

        super().keyPressEvent(event)

        # Hide completer on Escape or bare modifier keys
        if event.key() == Qt.Key.Key_Escape:
            c and c.popup().hide()
            return
        if not event.text() and not is_ctrl_space:
            return

        self._maybe_update_completer(is_ctrl_space)

    # ── Auto-indent ────────────────────────────────────────────────────────────

    def _auto_indent(self):
        cursor = self.textCursor()
        line = cursor.block().text()
        indent = len(line) - len(line.lstrip(' '))
        new_indent = ' ' * indent
        if line.rstrip().endswith(':'):
            new_indent += '    '
        cursor.insertText('\n' + new_indent)
        self.setTextCursor(cursor)

    def _insert_tab_spaces(self):
        cursor = self.textCursor()
        col = cursor.positionInBlock()
        spaces = 4 - (col % 4)
        cursor.insertText(' ' * spaces)
        self.setTextCursor(cursor)

    def _smart_backspace(self) -> bool:
        """Remove a full indent block (4 spaces) when cursor is at indent boundary."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        line = cursor.block().text()
        col = cursor.positionInBlock()
        before = line[:col]
        if before and before == ' ' * len(before) and len(before) % 4 == 0 and len(before) > 0:
            for _ in range(4):
                cursor.deletePreviousChar()
            return True
        return False

    # ── Completion ─────────────────────────────────────────────────────────────

    def _maybe_hide_completer(self):
        if self._completer:
            self._completer.popup().hide()

    def _maybe_update_completer(self, force: bool = False):
        c = self._completer
        prefix = self._completion_prefix()

        if not force and len(prefix) < 2:
            c.popup().hide()
            return

        self._update_model(prefix)

        if c.completionPrefix() != prefix:
            c.setCompletionPrefix(prefix)
            c.popup().setCurrentIndex(c.completionModel().index(0, 0))

        if c.completionCount() == 0:
            c.popup().hide()
            return

        # Position popup just below the cursor
        cr = self.cursorRect()
        cr.setWidth(
            c.popup().sizeHintForColumn(0)
            + c.popup().verticalScrollBar().sizeHint().width()
            + 8
        )
        c.complete(cr)

    def _completion_prefix(self) -> str:
        """Return the word (including module prefix) under the cursor."""
        cursor = self.textCursor()
        pos = cursor.position()
        text = self.toPlainText()
        start = pos
        while start > 0 and text[start - 1] in (
            'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.'
        ):
            start -= 1
        return text[start:pos]

    def _update_model(self, prefix: str):
        if JEDI_AVAILABLE:
            words = self._jedi_completions()
            if words:
                self._completer.model().setStringList(words)
                return

        # Context-aware static fallback
        if '.' in prefix:
            module = prefix.rsplit('.', 1)[0]
            if module == 'bps':
                words = _BPS_METHODS
            elif module == 'bp':
                words = _BP_METHODS
            else:
                words = _ALL_WORDS
        else:
            words = _ALL_WORDS
        self._completer.model().setStringList(words)

    def _jedi_completions(self) -> list:
        try:
            source = self.toPlainText()
            cursor = self.textCursor()
            line = cursor.blockNumber() + 1
            col  = cursor.positionInBlock()
            script = jedi.Script(source)
            return [c.name for c in script.complete(line, col)][:120]
        except Exception:
            return []

    def _insert_completion(self, completion: str):
        cursor = self.textCursor()
        prefix = self._completer.completionPrefix()
        # Only replace the part after the last dot
        replace_len = len(prefix.rsplit('.', 1)[-1]) if '.' in prefix else len(prefix)
        cursor.movePosition(
            QTextCursor.MoveOperation.Left,
            QTextCursor.MoveMode.KeepAnchor,
            replace_len,
        )
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def focusOutEvent(self, event):
        if self._completer:
            self._completer.popup().hide()
        super().focusOutEvent(event)
