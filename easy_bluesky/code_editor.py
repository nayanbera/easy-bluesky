"""code_editor.py — QPlainTextEdit with line numbers, auto-indentation, and auto-completion."""

try:
    import jedi
    JEDI_AVAILABLE = True
except ImportError:
    JEDI_AVAILABLE = False

from PyQt6.QtWidgets import QPlainTextEdit, QCompleter, QAbstractItemView, QWidget, QTextEdit
from PyQt6.QtCore import Qt, QStringListModel, QRect, QSize
from PyQt6.QtGui import (
    QTextCursor, QKeyEvent, QFont, QPainter, QColor,
    QTextCharFormat, QPalette,
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
        self._completer = None
        self._gutter = _LineNumberArea(self)

        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)

        self._update_gutter_width(0)
        self._highlight_current_line()
        self._setup_completer()

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
        self.setExtraSelections([sel] if not self.isReadOnly() else [])

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
