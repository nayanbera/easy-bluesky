"""re_console.py — Dedicated RE Manager console tab."""

from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QCheckBox,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat


class REConsoleWidget(QWidget):
    """Displays live console output from the RE Manager."""

    diagnose_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._auto_scroll = True
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Toolbar
        bar = QHBoxLayout()
        lbl = QLabel("RE MANAGER CONSOLE")
        lbl.setObjectName("section_title")
        bar.addWidget(lbl)
        bar.addStretch()

        self._scroll_chk = QCheckBox("Auto-scroll")
        self._scroll_chk.setChecked(True)
        self._scroll_chk.toggled.connect(lambda v: setattr(self, "_auto_scroll", v))
        bar.addWidget(self._scroll_chk)

        btn_diag = QPushButton("Diagnose")
        btn_diag.setMaximumWidth(80)
        btn_diag.setToolTip("Check ZMQ info socket and RE Manager process flags")
        btn_diag.clicked.connect(self.diagnose_requested)
        bar.addWidget(btn_diag)

        btn_clear = QPushButton("Clear")
        btn_clear.setMaximumWidth(70)
        btn_clear.clicked.connect(self._output.clear if hasattr(self, "_output") else lambda: None)
        bar.addWidget(btn_clear)
        lay.addLayout(bar)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(QFont("Courier New", 11))
        self._output.setMaximumBlockCount(5000)   # keep last 5000 lines
        self._output.setStyleSheet(
            "QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border: 1px solid #444; }"
        )
        lay.addWidget(self._output, 1)

        # Fix the clear button now that _output exists
        btn_clear.clicked.disconnect()
        btn_clear.clicked.connect(self._output.clear)

        # Status bar
        self._status = QLabel("Waiting for connection…")
        self._status.setObjectName("dim_text")
        self._status.setStyleSheet("font-size: 11px; padding: 2px;")
        lay.addWidget(self._status)

    def append(self, text: str):
        """Append new text from the RE Manager console monitor."""
        if not text:
            return

        cursor = self._output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        for line in text.splitlines(keepends=True):
            fmt = QTextCharFormat()
            lower = line.lower()
            if any(w in lower for w in ("error", "traceback", "exception", "failed")):
                fmt.setForeground(QColor("#d62728"))
            elif any(w in lower for w in ("warning", "warn")):
                fmt.setForeground(QColor("#ff7f0e"))
            elif any(w in lower for w in ("success", "complete", "loaded", "started")):
                fmt.setForeground(QColor("#2ca02c"))
            else:
                fmt.setForeground(QColor("#d4d4d4"))
            cursor.insertText(line, fmt)

        if self._auto_scroll:
            self._output.moveCursor(QTextCursor.MoveOperation.End)
            self._output.ensureCursorVisible()

        ts = datetime.now().strftime("%H:%M:%S")
        self._status.setText(f"Last update: {ts}")

    def on_connected(self):
        self._status.setText("Connected — monitoring RE Manager output")
        self._status.setStyleSheet("font-size: 11px; padding: 2px; color: #2ca02c;")

    def on_disconnected(self):
        self._status.setText("Disconnected")
        self._status.setStyleSheet("font-size: 11px; padding: 2px; color: #d62728;")
