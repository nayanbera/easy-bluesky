"""notepad.py — Floating note-taking window tied to the active experiment."""

import json
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt, QRect, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

try:
    import markdown as _md_module
    def _to_html(text: str) -> str:
        return _md_module.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
except ImportError:
    def _to_html(text: str) -> str:
        return (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>")
        )

_NOTE_CSS = (
    "body{background:#1e1e1e;color:#ddd;"
    "font-family:Menlo,Consolas,monospace;font-size:12px;padding:6px;margin:0}"
    "h1,h2,h3{color:#eee;margin:6px 0 2px 0}"
    "h1{font-size:15px}h2{font-size:13px}h3{font-size:12px}"
    "p{margin:2px 0}"
    "code{background:#2d2d2d;color:#f8c555;padding:1px 3px;border-radius:3px}"
    "pre{background:#2d2d2d;padding:6px;border-radius:4px;overflow-x:auto}"
    "pre code{background:none;padding:0}"
    "blockquote{border-left:3px solid #555;margin:4px 0;padding-left:8px;color:#aaa}"
    "a{color:#6af}"
    "table{border-collapse:collapse;margin:4px 0}"
    "th,td{border:1px solid #555;padding:3px 8px}"
    "th{background:#2a2a2a;color:#eee}"
    "ul,ol{margin:2px 0;padding-left:20px}"
    "hr{border:none;border-top:1px solid #444;margin:6px 0}"
    ".note-header{color:#888;font-size:10px;border-top:1px solid #333;"
    "padding-top:6px;margin-top:10px}"
)


# ── Region selector overlay ────────────────────────────────────────────────────

class _RegionSelector(QWidget):
    """Full-screen transparent overlay; user drags to select a rectangle."""

    region_selected = pyqtSignal(QRect)  # logical screen coords

    def __init__(self):
        super().__init__(None)
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        screen = QApplication.primaryScreen()
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._origin = None
        self._current = None

    def showEvent(self, event):
        self.grabMouse()
        self.grabKeyboard()

    def closeEvent(self, event):
        self.releaseMouse()
        self.releaseKeyboard()
        super().closeEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._origin and self._current:
            sel = QRect(self._origin, self._current).normalized()
            # Clear interior so users can see what they're selecting
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(sel, QColor(0, 0, 0, 0))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(255, 255, 255, 220), 2))
            p.drawRect(sel)
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(sel.x() + 4, sel.y() - 6,
                       f"{sel.width()} × {sel.height()}  —  ESC to cancel")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin

    def mouseMoveEvent(self, event):
        if self._origin:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._origin:
            rect = QRect(self._origin, event.position().toPoint()).normalized()
            self.close()
            if rect.width() > 5 and rect.height() > 5:
                self.region_selected.emit(rect)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()


# ── Table builder dialog ───────────────────────────────────────────────────────

class _TableDialog(QDialog):
    """Build a Markdown table interactively."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Insert Table")
        self.resize(540, 320)
        lay = QVBoxLayout(self)

        cfg = QHBoxLayout()
        cfg.addWidget(QLabel("Rows:"))
        self._rows = QSpinBox()
        self._rows.setRange(1, 30)
        self._rows.setValue(3)
        cfg.addWidget(self._rows)
        cfg.addWidget(QLabel("Columns:"))
        self._cols = QSpinBox()
        self._cols.setRange(1, 12)
        self._cols.setValue(3)
        cfg.addWidget(self._cols)
        cfg.addStretch()
        cfg.addWidget(QLabel("(Row 1 = header)"))
        lay.addLayout(cfg)

        self._table = QTableWidget(3, 3)
        self._reset_headers()
        lay.addWidget(self._table)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._rows.valueChanged.connect(self._resize)
        self._cols.valueChanged.connect(self._resize)

    def _reset_headers(self):
        cols = self._table.columnCount()
        self._table.setHorizontalHeaderLabels([f"Col {i + 1}" for i in range(cols)])

    def _resize(self):
        self._table.setRowCount(self._rows.value())
        self._table.setColumnCount(self._cols.value())
        self._reset_headers()

    def _cell(self, r: int, c: int) -> str:
        item = self._table.item(r, c)
        return item.text().strip() if item else ""

    def markdown(self) -> str:
        rows = self._rows.value()
        cols = self._cols.value()
        header = [self._cell(0, c) or f"Col {c + 1}" for c in range(cols)]
        sep = ["---"] * cols
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for r in range(1, rows):
            lines.append("| " + " | ".join(self._cell(r, c) for c in range(cols)) + " |")
        return "\n".join(lines)


# ── Attachment bar ─────────────────────────────────────────────────────────────

class _AttachBar(QWidget):
    """Horizontal strip of [📎 name] [✕] pairs for pending / edit attachments."""

    removed = pyqtSignal(int)  # index removed

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)
        self._lay.addStretch()
        self.hide()

    def set_attachments(self, paths: list):
        # Clear existing
        while self._lay.count() > 1:
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, p in enumerate(paths):
            name = Path(p).name
            lbl = QLabel(f"📎 {name}")
            lbl.setStyleSheet("color:#6af;font-size:10px;")
            lbl.setToolTip(p)
            btn = QPushButton("✕")
            btn.setFixedSize(16, 16)
            btn.setStyleSheet(
                "QPushButton{background:none;border:none;color:#f88;font-size:10px;padding:0}"
                "QPushButton:hover{color:#f44}"
            )
            idx = i
            btn.clicked.connect(lambda _, i=idx: self.removed.emit(i))
            self._lay.insertWidget(self._lay.count() - 1, lbl)
            self._lay.insertWidget(self._lay.count() - 1, btn)
        self.setVisible(bool(paths))


# ── Main window ────────────────────────────────────────────────────────────────

class NotepadWindow(QMainWindow):
    """Floating note-taking window tied to the active experiment."""

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._exp_dir: Path | None = None
        self._notes: list = []
        self._editing_idx: int | None = None
        self._pending_attachments: list = []
        self._edit_attachments: list = []
        self._region_selector: _RegionSelector | None = None
        self._setup_ui()
        self.setWindowTitle("📝 Notes")
        self.resize(540, 700)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        # Viewer
        self._viewer = QTextEdit()
        self._viewer.setReadOnly(True)
        self._viewer.setFont(QFont("Menlo, Consolas, Monaco, 'Courier New'", 11))
        lay.addWidget(self._viewer, stretch=3)

        # Note list + Edit/Delete
        list_row = QHBoxLayout()
        self._list = QListWidget()
        self._list.setFixedHeight(90)
        self._list.setFont(QFont("Menlo, Consolas, Monaco, 'Courier New'", 10))
        self._list.setStyleSheet(
            "QListWidget{background:#252525;border:1px solid #444}"
            "QListWidget::item:selected{background:#2a4a7a}"
        )
        list_row.addWidget(self._list)

        btn_col = QVBoxLayout()
        self._btn_edit   = QPushButton("✏️ Edit")
        self._btn_delete = QPushButton("🗑 Delete")
        self._btn_edit.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._btn_edit.setFixedWidth(80)
        self._btn_delete.setFixedWidth(80)
        btn_col.addWidget(self._btn_edit)
        btn_col.addWidget(self._btn_delete)
        btn_col.addStretch()
        list_row.addLayout(btn_col)
        lay.addLayout(list_row)

        # Edit-mode attachment strip (existing note's attachments)
        self._edit_attach_label = QLabel("Attachments:")
        self._edit_attach_label.setStyleSheet("color:#aaa;font-size:10px;")
        self._edit_attach_label.hide()
        lay.addWidget(self._edit_attach_label)

        self._edit_attach_bar = _AttachBar()
        lay.addWidget(self._edit_attach_bar)

        # Controls row: Scan#, screenshots, table
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Scan #:"))
        self._scan_spin = QSpinBox()
        self._scan_spin.setRange(0, 99999)
        self._scan_spin.setSpecialValueText("—")
        self._scan_spin.setFixedWidth(80)
        self._scan_spin.setToolTip("Link to a scan number (— = no link)")
        ctrl.addWidget(self._scan_spin)
        ctrl.addStretch()

        for label, tip, slot in [
            ("📷 Plot",   "Capture live plot",       self._screenshot_plot),
            ("📷 App",    "Capture app window",       self._screenshot_app),
            ("📷 Region", "Select screen region",     self._screenshot_region),
            ("📊 Table",  "Insert Markdown table",    self._insert_table),
        ]:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            ctrl.addWidget(b)
        lay.addLayout(ctrl)

        # Pending attachment bar (new note)
        self._pending_bar = _AttachBar()
        lay.addWidget(self._pending_bar)

        # Entry
        self._entry = QPlainTextEdit()
        self._entry.setPlaceholderText("Type a note… (Ctrl+Enter to save, Markdown supported)")
        self._entry.setFont(QFont("Menlo, Consolas, Monaco, 'Courier New'", 11))
        self._entry.setFixedHeight(110)
        lay.addWidget(self._entry)

        # Bottom row
        bot = QHBoxLayout()
        self._btn_cancel = QPushButton("Cancel Edit")
        self._btn_cancel.hide()
        self._btn_save = QPushButton("Save Note")
        bot.addWidget(self._btn_cancel)
        bot.addStretch()
        bot.addWidget(self._btn_save)
        lay.addLayout(bot)

        # Connections
        self._list.currentRowChanged.connect(self._on_list_row_changed)
        self._list.itemDoubleClicked.connect(lambda _: self._on_edit_clicked())
        self._btn_edit.clicked.connect(self._on_edit_clicked)
        self._btn_delete.clicked.connect(self._on_delete_clicked)
        self._btn_save.clicked.connect(self._save_note)
        self._btn_cancel.clicked.connect(self._cancel_edit)
        self._pending_bar.removed.connect(self._remove_pending)
        self._edit_attach_bar.removed.connect(self._remove_edit_attachment)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._save_note)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_experiment(self, exp_dir: str):
        self._cancel_edit()
        self._exp_dir = Path(exp_dir) if exp_dir else None
        self._pending_attachments.clear()
        self._pending_bar.set_attachments([])
        self._load_notes()
        name = self._exp_dir.name if self._exp_dir else "no experiment"
        self.setWindowTitle(f"📝 Notes — {name}")

    # ── Notes I/O ──────────────────────────────────────────────────────────────

    def _notes_file(self) -> Path | None:
        return (self._exp_dir / "notes.jsonl") if self._exp_dir else None

    def _rewrite_file(self):
        f = self._notes_file()
        if f is None:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "w", encoding="utf-8") as fp:
            for n in self._notes:
                fp.write(json.dumps(n) + "\n")

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
        self._refresh_list()
        self._render()

    def _refresh_list(self):
        self._list.blockSignals(True)
        prev_row = self._list.currentRow()
        self._list.clear()
        for n in self._notes:
            ts   = n.get("ts", "")
            text = n.get("text", "").replace("\n", " ")
            preview = text[:50] + "…" if len(text) > 50 else text
            src  = n.get("source", "manual")
            icon = {"manual": "✏️", "voice": "🎤", "ai": "🤖"}.get(src, "✏️")
            n_att = len(n.get("attachments", []))
            att_tag = f" 📎{n_att}" if n_att else ""
            self._list.addItem(f"{icon} {ts}{att_tag}  —  {preview}")
        if 0 <= prev_row < self._list.count():
            self._list.setCurrentRow(prev_row)
        self._list.blockSignals(False)
        self._on_list_row_changed(self._list.currentRow())

    def _render(self, highlight_idx: int = -1):
        if not self._notes:
            name = self._exp_dir.name if self._exp_dir else "no experiment"
            self._viewer.setHtml(
                f'<p style="color:#666">No notes yet for <b>{name}</b>.</p>'
            )
            return

        rows = []
        for i, n in enumerate(self._notes):
            ts          = n.get("ts", "")
            raw_text    = n.get("text", "")
            scan_num    = n.get("scan_num")
            source      = n.get("source", "manual")
            attachments = n.get("attachments", [])
            edited      = n.get("edited_ts")

            src_icon = {"manual": "✏️", "voice": "🎤", "ai": "🤖"}.get(source, "✏️")
            scan_tag = (
                f'&nbsp;<span style="color:#aaa;font-size:10px;">scan #{scan_num}</span>'
                if scan_num else ""
            )
            edit_tag = (
                f'&nbsp;<span style="color:#888;font-size:9px;">(edited {edited})</span>'
                if edited else ""
            )
            bg = "background:#1a2a3a;" if i == highlight_idx else ""
            header = (
                f'<div class="note-header" style="{bg}">'
                f'{src_icon} {ts}{scan_tag}{edit_tag}</div>'
            )
            body = f'<div style="margin:2px 0 4px 0">{_to_html(raw_text)}</div>'
            attach_html = "".join(
                (
                    f'<div style="margin:4px 0">'
                    f'<img src="file:///{Path(p).as_posix()}" width="460" '
                    f'style="border:1px solid #444;border-radius:4px"><br>'
                    f'<span style="color:#6af;font-size:10px">📎 {Path(p).name}</span>'
                    f'</div>'
                )
                if Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
                else f'<div style="color:#6af;font-size:10px">📎 {Path(p).name}</div>'
                for p in attachments
            )
            rows.append(header + body + attach_html)

        self._viewer.setHtml(
            f"<html><head><style>{_NOTE_CSS}</style></head><body>"
            + "".join(rows)
            + "</body></html>"
        )
        sb = self._viewer.verticalScrollBar()
        sb.setValue(sb.maximum() if highlight_idx < 0 else 0)

    # ── List selection ─────────────────────────────────────────────────────────

    def _on_list_row_changed(self, row: int):
        has = 0 <= row < len(self._notes)
        self._btn_edit.setEnabled(has and self._editing_idx is None)
        self._btn_delete.setEnabled(has and self._editing_idx is None)
        if has:
            self._render(highlight_idx=row)

    # ── Edit ───────────────────────────────────────────────────────────────────

    def _on_edit_clicked(self):
        row = self._list.currentRow()
        if not (0 <= row < len(self._notes)):
            return
        note = self._notes[row]
        self._editing_idx = row
        self._edit_attachments = list(note.get("attachments", []))

        self._entry.setPlainText(note.get("text", ""))
        self._scan_spin.setValue(note.get("scan_num") or 0)

        # Show existing attachments with ✕
        self._edit_attach_label.show()
        self._edit_attach_bar.set_attachments(self._edit_attachments)

        self._btn_save.setText("Update Note")
        self._btn_cancel.show()
        self._btn_edit.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._entry.setFocus()

    def _cancel_edit(self):
        self._editing_idx = None
        self._edit_attachments = []
        self._entry.clear()
        self._scan_spin.setValue(0)
        self._edit_attach_label.hide()
        self._edit_attach_bar.set_attachments([])
        self._btn_save.setText("Save Note")
        self._btn_cancel.hide()
        row = self._list.currentRow()
        self._btn_edit.setEnabled(0 <= row < len(self._notes))
        self._btn_delete.setEnabled(0 <= row < len(self._notes))

    def _remove_edit_attachment(self, idx: int):
        if 0 <= idx < len(self._edit_attachments):
            self._edit_attachments.pop(idx)
            self._edit_attach_bar.set_attachments(self._edit_attachments)
            if not self._edit_attachments:
                self._edit_attach_label.hide()

    # ── Delete ─────────────────────────────────────────────────────────────────

    def _on_delete_clicked(self):
        row = self._list.currentRow()
        if not (0 <= row < len(self._notes)):
            return
        note = self._notes[row]
        attachments = note.get("attachments", [])

        msg = "Delete this note?"
        if attachments:
            msg += f"\n\nIt has {len(attachments)} image file(s).\nDelete image files from disk too?"
            reply = QMessageBox.question(
                self, "Delete Note",
                msg,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            if reply == QMessageBox.StandardButton.Yes:
                for p in attachments:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
        else:
            reply = QMessageBox.question(
                self, "Delete Note", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._notes.pop(row)
        self._rewrite_file()
        self._refresh_list()
        self._render()

    # ── Save / update ──────────────────────────────────────────────────────────

    def _remove_pending(self, idx: int):
        if 0 <= idx < len(self._pending_attachments):
            self._pending_attachments.pop(idx)
            self._pending_bar.set_attachments(self._pending_attachments)

    def _save_note(self):
        text = self._entry.toPlainText().strip()
        scan_num = self._scan_spin.value() or None

        if self._editing_idx is not None:
            # In-place update
            if not text and not self._edit_attachments:
                return
            if self._notes_file() is None:
                QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
                return
            note = dict(self._notes[self._editing_idx])
            note["text"]        = text
            note["scan_num"]    = scan_num
            note["attachments"] = list(self._edit_attachments)
            note["edited_ts"]   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._notes[self._editing_idx] = note
            self._rewrite_file()
            self._refresh_list()
            self._render(highlight_idx=self._editing_idx)
            self._cancel_edit()
            return

        # New note
        if not text and not self._pending_attachments:
            return
        if self._notes_file() is None:
            QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
            return
        ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        note = {
            "ts":          ts,
            "text":        text,
            "scan_num":    scan_num,
            "source":      "manual",
            "attachments": list(self._pending_attachments),
        }
        f = self._notes_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(note) + "\n")
        self._notes.append(note)
        self._refresh_list()
        self._render()
        self._entry.clear()
        self._scan_spin.setValue(0)
        self._pending_attachments.clear()
        self._pending_bar.set_attachments([])

    # ── Table insertion ─────────────────────────────────────────────────────────

    def _insert_table(self):
        dlg = _TableDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            md = dlg.markdown()
            cursor = self._entry.textCursor()
            text = self._entry.toPlainText()
            if text and not text.endswith("\n"):
                md = "\n\n" + md
            cursor.insertText(md)
            self._entry.setFocus()

    # ── Screenshots ─────────────────────────────────────────────────────────────

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

    def _screenshot_region(self):
        if self._exp_dir is None:
            QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
            return
        sel = _RegionSelector()
        self._region_selector = sel
        sel.region_selected.connect(self._on_region_selected)
        sel.show()

    def _on_region_selected(self, rect: QRect):
        # Brief delay so the overlay is fully gone before grabbing.
        QTimer.singleShot(200, lambda: self._grab_region(rect))

    def _grab_region(self, rect: QRect):
        screen = QApplication.primaryScreen()
        pixmap = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
        self._save_pixmap(pixmap, "region")

    def _capture_widget(self, widget, tag: str):
        if self._exp_dir is None:
            QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
            return
        self._save_pixmap(widget.grab(), tag)

    def _save_pixmap(self, pixmap, tag: str):
        if self._exp_dir is None:
            return
        shots_dir = self._exp_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = shots_dir / f"{ts_file}_{tag}.png"
        if not pixmap.save(str(path)):
            QMessageBox.warning(self, "Screenshot failed", f"Could not save to {path}")
            return

        if self._entry.toPlainText().strip() or self._editing_idx is not None:
            # Accumulate as pending / edit attachment
            if self._editing_idx is not None:
                self._edit_attachments.append(str(path))
                self._edit_attach_label.show()
                self._edit_attach_bar.set_attachments(self._edit_attachments)
            else:
                self._pending_attachments.append(str(path))
                self._pending_bar.set_attachments(self._pending_attachments)
        else:
            # No text typed — save an immediate screenshot note
            scan_num = self._scan_spin.value() or None
            ts_note  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            label    = {"plot": "Live Plot", "app": "App Window",
                        "region": "Region"}.get(tag, tag)
            note = {
                "ts":          ts_note,
                "text":        f"Screenshot: {label}",
                "scan_num":    scan_num,
                "source":      "manual",
                "attachments": [str(path)],
            }
            f = self._notes_file()
            if f is None:
                QMessageBox.warning(self, "No Experiment", "Open an experiment first.")
                return
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(note) + "\n")
            self._notes.append(note)
            self._refresh_list()
            self._render()
