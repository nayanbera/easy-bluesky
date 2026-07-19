"""highlighter.py — Python syntax highlighter for the code editor."""

from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor

class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.rules = []

        def add(pattern, color, bold=False):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(700)
            import re
            self.rules.append((re.compile(pattern), fmt))

        add(r'\b(def|class|return|yield|from|import|as|if|elif|else|for|while|with|try|except|finally|raise|pass|break|continue|and|or|not|in|is|None|True|False|lambda|async|await)\b', "#569cd6", bold=True)
        add(r'\b(int|float|str|list|dict|tuple|set|bool|type|len|range|print|super|self)\b', "#4ec9b0")
        add(r'(\"\"\"[\s\S]*?\"\"\"|\'\'\'[\s\S]*?\'\'\'|\"[^\"]*\"|\'[^\']*\')', "#ce9178")
        add(r'#[^\n]*', "#6a9955")
        add(r'\b\d+\.?\d*\b', "#b5cea8")
        add(r'@\w+', "#c586c0")

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end()-m.start(), fmt)
