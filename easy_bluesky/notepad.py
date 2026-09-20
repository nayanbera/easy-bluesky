"""notepad.py — Floating note-taking window tied to the active experiment."""

import json
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)


class NotepadWindow(QMainWindow):
    """Floating note-taking window tied to the active experiment.

    Notes are appended to ``<exp_dir>/notes.jsonl``.
    Screenshots are saved to ``<exp_dir>/screenshots/`` and listed as
    attachments on the next saved note.

    Call ``set_experiment(exp_dir)`` when the active experiment changes.
    Pass ``main_window`` so screenshot helpers can grab the live plot and
    the full app window.
    """

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._exp_dir: Path | None = None
        self._notes: list = []
        self._pending_attachments: list = []
        self._setup_ui()
        self.setWindowTitle("📝 Notes")
        self.resize(520, 640)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Notes viewer
        self._viewer = QTextEdit()
        self._viewer.setReadOnly(True)
        self._viewer.setFont(QFont("Menlo, Consolas, Monaco, 'Courier New'", 11))
        lay.addWidget(self._viewer, stretch=3)

        # Scan link + screenshot row
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Scan #:"))
        self._scan_spin = QSpinBox()
        self._scan_spin.setRange(0, 99999)
        self._scan_spin.setSpecialValueText("—")
        self._scan_spin.setFixedWidth(80)
        self._scan_spin.setToolTip("Link this note to a scan number (— = no link)")
        ctrl_row.addWidget(self._scan_spin)
        ctrl_row.addStretch()

        self._btn_shot_plot = QPushButton("📷 Live Plot")
        self._btn_shot_plot.setToolTip("Capture live plot and attach to note")
        self._btn_shot_app  = QPushButton("📷 App Window")
        self._btn_shot_app.setToolTip("Capture full app window and attach to note")
        self._btn_shot_plot.clicked.connect(self._screenshot_plot)
        self._btn_shot_app.clicked.connect(self._screenshot_app)
        ctrl_row.addWidget(self._btn_shot_plot)
        ctrl_row.addWidget(self._btn_shot_app)
        lay.addLayout(ctrl_row)

        # Pending attachments indicator
        self._attach_label = QLabel("")
        self._attach_label.setStyleSheet("color: #aaa; font-size: 10px;")
        self._attach_label.setWordWrap(True)
        lay.addWidget(self._attach_label)

        # Text entry
        self._entry = QPlainTextEdit()
        self._entry.setPlaceholderText("Type a note… (Ctrl+Enter to save)")
        self._entry.setFont(QFont("Menlo, Consolas, Monaco, 'Courier New'", 11))
        self._entry.setMaximumHeight(120)
        lay.addWidget(self._entry)

        # Save row
        save_row = QHBoxLayout()
        save_row.addStretch()
        self._btn_save = QPushButton("Save Note")
        save_row.addWidget(self._btn_save)
        lay.addLayout(save_row)

        self._btn_save.clicked.connect(self._save_note)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._save_note)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_experiment(self, exp_dir: str):
        """Switch to a new experiment's notes file."""
        self._exp_dir = Path(exp_dir) if exp_dir else None
        self._pending_attachments.clear()
        self._attach_label.setText("")
        self._load_notes()
        exp_name = self._exp_dir.name if self._exp_dir else "no experiment"
        self.setWindowTitle(f"📝 Notes — {exp_name}")

    # ── Notes I/O ─────────────────────────────────────────────────────────────

    def _notes_file(self) -> Path | None:
        return (self._exp_dir / "notes.jsonl") if self._exp_dir else None

    def _load_notes(self):
        self._notes = []
        f = self._notes_file()
        if f and f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        self._notes.append(json.loads(line))
                    except Exception:
                        pass
        self._render()

    def _render(self):
        if not self._notes:
            exp_name = self._exp_dir.name if self._exp_dir else "no experiment"
            self._viewer.setHtml(
                f'<p style="color:#666">No notes yet for <b>{exp_name}</b>.</p>'
            )
            return

        rows = []
        for n in self._notes:
            ts   = n.get("ts", "")
            text = (
                n.get("text", "")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            scan_num    = n.get("scan_num")
            source      = n.get("source", "manual")
            attachments = n.get("attachments", [])

            src_icon = {"manual": "✏️", "voice": "🎤", "ai": "🤖"}.get(source, "✏️")
            scan_tag = (
                f'&nbsp;<span style="color:#aaa;font-size:10px;">scan #{scan_num}</span>'
                if scan_num else ""
            )
            header = (
                f'<div style="color:#888;font-size:10px;margin-top:10px">'
                f'{src_icon} {ts}{scan_tag}</div>'
            )
            body = f'<div style="margin:2px 0 4px 0">{text}</div>'
            attach_html = "".join(
                f'<div style="margin:4px 0">'
                f'<img src="file:///{Path(p).as_posix()}" width="460" '
                f'style="border:1px solid #444;border-radius:4px"><br>'
                f'<span style="color:#6af;font-size:10px">📎 {Path(p).name}</span>'
                f'</div>'
                if Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
                else f'<div style="color:#6af;font-size:10px">📎 {Path(p).name}</div>'
                for p in attachments
            )
            rows.append(header + body + attach_html)

        self._viewer.setHtml(
            '<html><body style="background:#1e1e1e;color:#ddd;'
            "font-family:Menlo,Consolas,monospace;font-size:12px;padding:4px\">"
            + "".join(rows)
            + "</body></html>"
        )
        sb = self._viewer.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _save_note(self):
        text = self._entry.toPlainText().strip()
        if not text and not self._pending_attachments:
            return
        if self._notes_file() is None:
            QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
            return

        scan_num = self._scan_spin.value() or None
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        note = {
            "ts": ts,
            "text": text,
            "scan_num": scan_num,
            "source": "manual",
            "attachments": list(self._pending_attachments),
        }
        f = self._notes_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(note) + "\n")

        self._notes.append(note)
        self._render()
        self._entry.clear()
        self._scan_spin.setValue(0)
        self._pending_attachments.clear()
        self._attach_label.setText("")

    # ── Screenshots ───────────────────────────────────────────────────────────

    def _screenshot_plot(self):
        try:
            widget = self._main_window.experiments_tab.live_viewer
        except AttributeError:
            return
        self._capture_widget(widget, "plot")

    def _screenshot_app(self):
        if self._main_window is None:
            return
        self._capture_widget(self._main_window, "app")

    def _capture_widget(self, widget, tag: str):
        if self._exp_dir is None:
            QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
            return
        shots_dir = self._exp_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
        path    = shots_dir / f"{ts_file}_{tag}.png"
        ok = widget.grab().save(str(path))
        if not ok:
            QMessageBox.warning(self, "Screenshot failed", f"Could not save to {path}")
            return

        # If there is text in the entry box, just add the image to pending so
        # the user can save text + screenshot together. Otherwise save immediately.
        if self._entry.toPlainText().strip():
            self._pending_attachments.append(str(path))
            names = [Path(p).name for p in self._pending_attachments]
            self._attach_label.setText("Pending: " + ", ".join(names))
        else:
            scan_num = self._scan_spin.value() or None
            ts_note  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            label    = {"plot": "Live Plot", "app": "App Window"}.get(tag, tag)
            note = {
                "ts":          ts_note,
                "text":        f"Screenshot: {label}",
                "scan_num":    scan_num,
                "source":      "manual",
                "attachments": [str(path)],
            }
            f = self._notes_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(note) + "\n")
            self._notes.append(note)
            self._render()
