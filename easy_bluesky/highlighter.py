"""highlighter.py — Python syntax highlighter with multi-line string support."""

import re
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor


def _fmt(color: str, bold: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(700)
    return f


class PythonHighlighter(QSyntaxHighlighter):
    """
    Highlights Python syntax including multi-line triple-quoted strings.

    Block states:
      0 — normal
      1 — inside triple-double-quote string  (\"\"\" ... \"\"\")
      2 — inside triple-single-quote string  (\'\'\' ... \'\'\')
    """

    _NORMAL            = 0
    _IN_TRIPLE_DOUBLE  = 1
    _IN_TRIPLE_SINGLE  = 2

    def __init__(self, doc):
        super().__init__(doc)

        self._str_fmt      = _fmt("#ce9178")
        self._keyword_fmt  = _fmt("#569cd6", bold=True)
        self._builtin_fmt  = _fmt("#4ec9b0")
        self._comment_fmt  = _fmt("#6a9955")
        self._number_fmt   = _fmt("#b5cea8")
        self._deco_fmt     = _fmt("#c586c0")

        # Single-line rules (applied first; multi-line strings override them)
        self._rules = [
            (re.compile(
                r'\b(def|class|return|yield|from|import|as|if|elif|else|for|while|'
                r'with|try|except|finally|raise|pass|break|continue|and|or|not|in|'
                r'is|None|True|False|lambda|async|await)\b'
            ), self._keyword_fmt),
            (re.compile(
                r'\b(int|float|str|list|dict|tuple|set|bool|type|len|range|'
                r'print|super|self)\b'
            ), self._builtin_fmt),
            (re.compile(r'\b\d+\.?\d*\b'),  self._number_fmt),
            (re.compile(r'@\w+'),            self._deco_fmt),
        ]
        # Single-line strings: "..." and '...' (not triple-quoted)
        self._single_str_re = re.compile(
            r'"(?!"")(\\.|[^"\\])*"'
            r"|'(?!'')(\\.|[^'\\])*'"
        )
        # Comments: # to end of line
        self._comment_re = re.compile(r'#[^\n]*')

    # ── Main entry point ───────────────────────────────────────────────────────

    def highlightBlock(self, text: str):
        # 1. Keywords, builtins, numbers, decorators
        for pattern, fmt in self._rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)

        # 2. Single-line strings
        for m in self._single_str_re.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._str_fmt)

        # 3. Comments (# not inside a triple-quoted string; step 4 corrects any
        #    false positives by overriding with string colour)
        for m in self._comment_re.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._comment_fmt)

        # 4. Multi-line triple-quoted strings (override everything above)
        self._apply_multiline(text)

    # ── Multi-line triple-quote handling ───────────────────────────────────────

    def _apply_multiline(self, text: str):
        """
        Walk the line carrying forward previousBlockState.
        Highlights every character inside triple-quoted strings and sets
        currentBlockState so the next line continues correctly.
        """
        self.setCurrentBlockState(self._NORMAL)
        pos = 0
        prev = self.previousBlockState()

        # If the previous line ended inside a triple-quoted string, consume
        # the continuation at the start of this line first.
        if prev in (self._IN_TRIPLE_DOUBLE, self._IN_TRIPLE_SINGLE):
            delim = '"""' if prev == self._IN_TRIPLE_DOUBLE else "'''"
            end = text.find(delim, pos)
            if end == -1:
                # Entire line is still inside the string
                self.setCurrentBlockState(prev)
                self.setFormat(0, len(text), self._str_fmt)
                return
            # Closing delimiter found on this line
            self.setFormat(0, end + 3, self._str_fmt)
            pos = end + 3

        # Scan the rest of the line for new triple-quoted strings
        while pos < len(text):
            pd = text.find('"""', pos)
            ps = text.find("'''", pos)

            if pd == -1 and ps == -1:
                break

            # Pick whichever delimiter comes first
            if pd == -1 or (ps != -1 and ps < pd):
                start, delim, state = ps, "'''", self._IN_TRIPLE_SINGLE
            else:
                start, delim, state = pd, '"""', self._IN_TRIPLE_DOUBLE

            end = text.find(delim, start + 3)
            if end == -1:
                # String not closed on this line — continues to next
                self.setCurrentBlockState(state)
                self.setFormat(start, len(text) - start, self._str_fmt)
                return

            # String opens and closes on this line
            self.setFormat(start, end + 3 - start, self._str_fmt)
            pos = end + 3
