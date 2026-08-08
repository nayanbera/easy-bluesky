"""esaf_dialog.py — PyQt6 dialogs for ESAF management.

All dialogs are self-contained; the only easy_bluesky import is .esaf.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .esaf import (
    ESAFRecord,
    ESAFServerClient,
    ESAFServerError,
    ESAFUser,
    PIGroup,
    PIGroupRegistry,
    delete_cached,
    fetch_esaf,
    list_cached,
    load_cached,
    make_pi_slug,
    parse_esaf_pdf,
    save_cached,
)

# ── Colour constants (replicated locally to avoid cross-module import) ─────────
_SUCCESS = "#2ca02c"
_WARNING = "#ff7f0e"
_DANGER  = "#d62728"
_ACCENT  = "#1f77b4"
_DIM     = "#888888"

# Confidence thresholds
_CONF_HIGH   = 0.7
_CONF_MEDIUM = 0.4


def _conf_color(conf: float) -> str:
    """Map a confidence value to a CSS colour string."""
    if conf >= _CONF_HIGH:
        return _SUCCESS
    if conf >= _CONF_MEDIUM:
        return _WARNING
    return _DANGER


# ── Background workers ─────────────────────────────────────────────────────────

class _FetchWorker(QThread):
    """Fetch an ESAFRecord from the server or cache in a background thread."""
    done  = pyqtSignal(object, dict)   # (ESAFRecord, confidence)
    error = pyqtSignal(str)

    def __init__(self, esaf_id: str, settings: dict, parent=None):
        super().__init__(parent)
        self._esaf_id = esaf_id
        self._settings = settings

    def run(self):
        try:
            record = fetch_esaf(self._esaf_id, self._settings)
            if record is None:
                self.error.emit(f"ESAF {self._esaf_id!r} not found on server or in local cache.")
            else:
                self.done.emit(record, {})
        except Exception as exc:
            self.error.emit(str(exc))


class _ParsePDFWorker(QThread):
    """Parse an ESAF PDF in a background thread (local or server)."""
    done  = pyqtSignal(object, dict)   # (ESAFRecord, confidence)
    error = pyqtSignal(str)

    def __init__(self, pdf_bytes: bytes, use_server: bool, settings: dict, parent=None):
        super().__init__(parent)
        self._pdf_bytes = pdf_bytes
        self._use_server = use_server
        self._settings = settings

    def run(self):
        try:
            if self._use_server:
                url = (self._settings.get("esaf_server_url") or "").strip()
                key = (self._settings.get("esaf_api_key") or "").strip()
                client = ESAFServerClient(url, key)
                record, conf = client.parse_pdf(self._pdf_bytes)
            else:
                record, conf = parse_esaf_pdf(self._pdf_bytes)
            self.done.emit(record, conf)
        except ImportError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            self.error.emit(str(exc))


class _ServerSyncWorker(QThread):
    """Fetch all ESAFs from the shared server and merge into local cache."""
    done  = pyqtSignal(int)   # number of records synced
    error = pyqtSignal(str)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings

    def run(self):
        try:
            url = (self._settings.get("esaf_server_url") or "").strip()
            key = (self._settings.get("esaf_api_key") or "").strip()
            client = ESAFServerClient(url, key)
            records = client.list_esafs()
            for r in records:
                save_cached(r)
            self.done.emit(len(records))
        except Exception as exc:
            self.error.emit(str(exc))


# ── PIGroupPickerWidget ────────────────────────────────────────────────────────

class PIGroupPickerWidget(QWidget):
    """Composite widget for selecting a PI group from the local registry.

    Contains a QComboBox showing all known groups plus "＋ New group…",
    an auto-match label, and a Manage… button that opens PIGroupManagerDialog.

    Signal ``group_selected`` emits the selected PIGroup (or None when the
    placeholder/new-group entry is active).
    """

    group_selected = pyqtSignal(object)   # PIGroup or None

    _NEW_LABEL = "＋ New group…"
    _NONE_LABEL = "(none)"

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        from PyQt6.QtWidgets import QComboBox
        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_index_changed)

        self._match_label = QLabel("")
        self._match_label.setWordWrap(True)
        self._match_label.setStyleSheet(f"color: {_ACCENT}; font-style: italic; font-size: 11px;")
        self._match_label.setVisible(False)

        btn_manage = QPushButton("Manage…")
        btn_manage.setMaximumWidth(80)
        btn_manage.setToolTip("Add, edit, or delete PI groups")
        btn_manage.clicked.connect(self._on_manage)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._combo, 1)
        row.addWidget(btn_manage)
        lay.addLayout(row)
        lay.addWidget(self._match_label)

        self._groups: list[PIGroup] = []
        self._populating = False
        self.refresh()

    # ── public interface ───────────────────────────────────────────────────────

    def refresh(self):
        """Reload groups from PIGroupRegistry and repopulate the combo."""
        self._populating = True
        current_slug = self._current_slug()
        self._groups = PIGroupRegistry.load()
        self._combo.clear()
        self._combo.addItem(self._NONE_LABEL)
        for g in self._groups:
            self._combo.addItem(g.display_name)
        self._combo.addItem(self._NEW_LABEL)
        # Restore selection
        if current_slug:
            for i, g in enumerate(self._groups):
                if g.slug == current_slug:
                    self._combo.setCurrentIndex(i + 1)  # +1 for (none)
                    break
        self._populating = False

    def set_auto_match(self, groups: list[PIGroup], matched_via: str = ""):
        """Pre-select the best match and show an auto-match info label."""
        if not groups:
            self._match_label.setVisible(False)
            return
        best = groups[0]
        # Select it in the combo
        for i, g in enumerate(self._groups):
            if g.slug == best.slug:
                self._combo.setCurrentIndex(i + 1)
                break
        via = f" via: {matched_via}" if matched_via else ""
        self._match_label.setText(f"Auto-matched{via} → {best.display_name}")
        self._match_label.setVisible(True)

    def selected_group(self) -> PIGroup | None:
        """Return the currently selected PIGroup, or None."""
        idx = self._combo.currentIndex()
        if idx <= 0:
            return None
        # Last item is "＋ New group…"
        group_idx = idx - 1
        if group_idx < len(self._groups):
            return self._groups[group_idx]
        return None

    # ── internal ───────────────────────────────────────────────────────────────

    def _current_slug(self) -> str:
        g = self.selected_group()
        return g.slug if g else ""

    def _on_index_changed(self, idx: int):
        if self._populating:
            return
        text = self._combo.currentText()
        if text == self._NEW_LABEL:
            # Open create dialog
            dlg = PIGroupDialog(parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_group:
                self.refresh()
                # Select the newly created group
                for i, g in enumerate(self._groups):
                    if g.slug == dlg.result_group.slug:
                        self._combo.setCurrentIndex(i + 1)
                        break
                else:
                    self._combo.setCurrentIndex(0)
            else:
                self._combo.setCurrentIndex(0)
        self.group_selected.emit(self.selected_group())

    def _on_manage(self):
        dlg = PIGroupManagerDialog(parent=self)
        dlg.exec()
        current_slug = self._current_slug()
        self.refresh()
        # Restore previous selection if still present
        for i, g in enumerate(self._groups):
            if g.slug == current_slug:
                self._combo.setCurrentIndex(i + 1)
                return
        self._combo.setCurrentIndex(0)


# ── PIGroupDialog ──────────────────────────────────────────────────────────────

class PIGroupDialog(QDialog):
    """Create or edit a PIGroup.

    Pass group=None to create a new group; pass an existing PIGroup to edit it.
    After accept(), ``result_group`` holds the saved group.
    """

    def __init__(self, group: PIGroup = None, parent=None):
        super().__init__(parent)
        self._edit_mode = group is not None
        self._original_slug = group.slug if group else ""
        self.result_group: PIGroup | None = None

        self.setWindowTitle("Edit PI Group" if self._edit_mode else "New PI Group")
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)

        self._univ = QLineEdit(group.univ_short_name if group else "")
        self._univ.setPlaceholderText("e.g. uchicago, anl, mit")
        self._univ.textChanged.connect(self._update_slug_hint)
        form.addRow("Univ. short name:", self._univ)

        self._first = QLineEdit(group.pi_first_name if group else "")
        self._first.setPlaceholderText("PI first name")
        self._first.textChanged.connect(self._update_slug_hint)
        form.addRow("PI first name:", self._first)

        self._last = QLineEdit(group.pi_last_name if group else "")
        self._last.setPlaceholderText("PI last name")
        self._last.textChanged.connect(self._update_slug_hint)
        form.addRow("PI last name:", self._last)

        self._inst = QLineEdit(group.pi_institution if group else "")
        self._inst.setPlaceholderText("e.g. University of Chicago")
        form.addRow("PI institution:", self._inst)

        self._slug = QLineEdit(group.slug if group else "")
        self._slug.setPlaceholderText("auto-generated — editable")
        self._slug.setStyleSheet(f"color: {_DIM};")
        form.addRow("Slug:", self._slug)

        lay.addLayout(form)

        lay.addWidget(QLabel("Known members (one per line):"))
        self._members = QPlainTextEdit()
        self._members.setMaximumHeight(120)
        if group:
            self._members.setPlainText("\n".join(group.known_members))
        self._members.setPlaceholderText("Smith, Alice\nJones, Bob")
        lay.addWidget(self._members)

        self._err_label = QLabel("")
        self._err_label.setStyleSheet(f"color: {_DANGER};")
        self._err_label.setWordWrap(True)
        lay.addWidget(self._err_label)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._update_slug_hint()

    def _update_slug_hint(self):
        """Auto-generate slug from name fields, but don't overwrite user edits."""
        slug = make_pi_slug(
            self._univ.text(), self._first.text(), self._last.text()
        )
        self._slug.setText(slug)

    def _on_accept(self):
        slug = self._slug.text().strip()
        first = self._first.text().strip()
        last = self._last.text().strip()
        univ = self._univ.text().strip()
        inst = self._inst.text().strip()

        if not slug:
            self._err_label.setText("Slug is required.")
            return
        if not (first or last):
            self._err_label.setText("PI first or last name is required.")
            return

        members = [
            line.strip()
            for line in self._members.toPlainText().splitlines()
            if line.strip()
        ]

        # Check for slug collision (only in create mode or if slug changed)
        if not self._edit_mode or slug != self._original_slug:
            existing = PIGroupRegistry.get(slug)
            if existing:
                self._err_label.setText(
                    f"Slug '{slug}' is already in use by '{existing.pi_name}'. "
                    "Edit the slug to make it unique."
                )
                return

        from datetime import datetime, timezone
        group = PIGroup(
            slug=slug,
            pi_first_name=first,
            pi_last_name=last,
            pi_institution=inst,
            univ_short_name=univ,
            known_members=members,
        )
        # Delete old slug if renamed
        if self._edit_mode and slug != self._original_slug:
            PIGroupRegistry.delete(self._original_slug)

        PIGroupRegistry.add_or_update(group)
        self.result_group = group
        self.accept()


# ── PIGroupManagerDialog ───────────────────────────────────────────────────────

class PIGroupManagerDialog(QDialog):
    """Manage the full list of PI groups: view, add, edit, delete."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PI Group Manager")
        self.setMinimumSize(480, 360)

        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        lbl = QLabel(
            "PI groups are used to associate an ESAF with a PI's research group. "
            "Known members are matched against ESAF user lists."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        lay.addWidget(lbl)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(self._on_edit)
        lay.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("＋ Add")
        btn_add.clicked.connect(self._on_add)
        self._btn_edit = QPushButton("Edit…")
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(self._btn_edit)
        btn_row.addWidget(self._btn_delete)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        lay.addWidget(btn_close, 0, Qt.AlignmentFlag.AlignRight)

        self._refresh()

    def _refresh(self):
        self._list.clear()
        for g in PIGroupRegistry.load():
            n = len(g.known_members)
            member_str = f"{n} member{'s' if n != 1 else ''}"
            item = QListWidgetItem(f"{g.display_name}   [{member_str}]")
            item.setData(Qt.ItemDataRole.UserRole, g.slug)
            self._list.addItem(item)

    def _selected_slug(self) -> str:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _on_add(self):
        dlg = PIGroupDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_edit(self):
        slug = self._selected_slug()
        if not slug:
            return
        group = PIGroupRegistry.get(slug)
        if group is None:
            return
        dlg = PIGroupDialog(group=group, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_delete(self):
        slug = self._selected_slug()
        if not slug:
            return
        group = PIGroupRegistry.get(slug)
        if group is None:
            return
        ans = QMessageBox.question(
            self,
            "Delete PI Group",
            f"Delete group '{group.display_name}'?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            PIGroupRegistry.delete(slug)
            self._refresh()


# ── ESAFReviewWidget ───────────────────────────────────────────────────────────

class ESAFReviewWidget(QWidget):
    """Editable display of an ESAFRecord's fields with confidence colour-coding.

    Confidence: 1.0 → green label, 0.4-0.7 → orange, <0.4 or missing → red.
    Users are displayed in a QTableWidget with Add/Remove row buttons.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Main fields form
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)

        self._esaf_id   = QLineEdit(); self._esaf_id.setPlaceholderText("ESAF number")
        self._title     = QLineEdit(); self._title.setPlaceholderText("Experiment title")
        self._start     = QLineEdit(); self._start.setPlaceholderText("YYYY-MM-DD")
        self._end       = QLineEdit(); self._end.setPlaceholderText("YYYY-MM-DD")
        self._beamline  = QLineEdit(); self._beamline.setPlaceholderText("e.g. 12-ID-B")
        self._prop_id   = QLineEdit(); self._prop_id.setPlaceholderText("GUP-XXXXX")

        self._lbl_esaf_id  = QLabel("ESAF ID:")
        self._lbl_title    = QLabel("Title:")
        self._lbl_start    = QLabel("Start date:")
        self._lbl_end      = QLabel("End date:")
        self._lbl_beamline = QLabel("Beamline:")
        self._lbl_prop_id  = QLabel("Proposal ID:")

        form.addRow(self._lbl_esaf_id,  self._esaf_id)
        form.addRow(self._lbl_title,    self._title)
        form.addRow(self._lbl_start,    self._start)
        form.addRow(self._lbl_end,      self._end)
        form.addRow(self._lbl_beamline, self._beamline)
        form.addRow(self._lbl_prop_id,  self._prop_id)

        lay.addLayout(form)

        # Users table
        users_label = QLabel("Users:")
        users_label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        lay.addWidget(users_label)

        self._users_table = QTableWidget(0, 4)
        self._users_table.setHorizontalHeaderLabels(["Name", "Institution", "Role", "Email"])
        self._users_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._users_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._users_table.setMinimumHeight(120)
        self._users_table.setMaximumHeight(200)
        lay.addWidget(self._users_table)

        user_btn_row = QHBoxLayout()
        btn_add_user = QPushButton("＋ Add user")
        btn_add_user.clicked.connect(self._add_user_row)
        self._btn_remove_user = QPushButton("Remove selected")
        self._btn_remove_user.clicked.connect(self._remove_user_row)
        user_btn_row.addWidget(btn_add_user)
        user_btn_row.addWidget(self._btn_remove_user)
        user_btn_row.addStretch()
        lay.addLayout(user_btn_row)

    # ── public interface ───────────────────────────────────────────────────────

    def load(self, record: ESAFRecord, confidence: dict = None):
        """Populate all fields from record; colour-code labels by confidence."""
        if confidence is None:
            confidence = {}

        self._esaf_id.setText(record.esaf_id or "")
        self._title.setText(record.title or "")
        self._start.setText(record.start_date or "")
        self._end.setText(record.end_date or "")
        self._beamline.setText(record.beamline or "")
        self._prop_id.setText(record.proposal_id or "")

        # Colour-code labels
        mapping = [
            (self._lbl_esaf_id,  "esaf_id"),
            (self._lbl_title,    "title"),
            (self._lbl_start,    "start_date"),
            (self._lbl_end,      "end_date"),
            (self._lbl_beamline, "beamline"),
            (self._lbl_prop_id,  "proposal_id"),
        ]
        for lbl, key in mapping:
            conf = confidence.get(key, -1.0)
            if conf < 0:
                lbl.setStyleSheet("")   # no confidence info
            else:
                color = _conf_color(conf)
                lbl.setStyleSheet(f"color: {color}; font-weight: bold;")

        # Users table
        self._users_table.setRowCount(0)
        for u in record.users:
            self._append_user(u.name, u.institution, u.role, u.email)

    def get_record(self) -> ESAFRecord:
        """Read back all edited fields into a new ESAFRecord."""
        users = []
        for row in range(self._users_table.rowCount()):
            def _cell(c) -> str:
                item = self._users_table.item(row, c)
                return item.text().strip() if item else ""
            users.append(ESAFUser(
                name=_cell(0),
                institution=_cell(1),
                role=_cell(2),
                email=_cell(3),
            ))
        return ESAFRecord(
            esaf_id=self._esaf_id.text().strip(),
            title=self._title.text().strip(),
            start_date=self._start.text().strip(),
            end_date=self._end.text().strip(),
            beamline=self._beamline.text().strip(),
            proposal_id=self._prop_id.text().strip(),
            users=users,
        )

    # ── internal ───────────────────────────────────────────────────────────────

    def _append_user(self, name: str = "", institution: str = "",
                     role: str = "", email: str = ""):
        row = self._users_table.rowCount()
        self._users_table.insertRow(row)
        for col, val in enumerate([name, institution, role, email]):
            self._users_table.setItem(row, col, QTableWidgetItem(val))

    def _add_user_row(self):
        self._append_user()
        self._users_table.scrollToBottom()
        # Start editing the new name cell
        row = self._users_table.rowCount() - 1
        self._users_table.editItem(self._users_table.item(row, 0))

    def _remove_user_row(self):
        rows = sorted(
            set(idx.row() for idx in self._users_table.selectedIndexes()),
            reverse=True,
        )
        for row in rows:
            self._users_table.removeRow(row)


# ── ESAFImportDialog ───────────────────────────────────────────────────────────

class ESAFImportDialog(QDialog):
    """Three-tab dialog for importing an ESAF record.

    Tabs:
        1. Source — choose where to get the ESAF data from
        2. Review — inspect and correct parsed/fetched fields
        3. PI Group — assign the ESAF to a PI group

    On OK: saves to local cache; sets ``result_record`` with the final ESAFRecord
    including ``pi_group_slug``.

    If a server URL is configured and the user requests server upload, the
    record is also pushed to the server.
    """

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.result_record: ESAFRecord | None = None
        self._current_record: ESAFRecord | None = None
        self._current_confidence: dict = {}
        self._pdf_bytes: bytes | None = None

        server_url = (settings.get("esaf_server_url") or "").strip()
        self._has_server = bool(server_url)

        self.setWindowTitle("Import ESAF")
        self.setMinimumSize(580, 500)

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs, 1)

        self._build_source_tab()
        self._build_review_tab()
        self._build_pi_group_tab()

        # Dialog buttons
        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.accepted.connect(self._on_accept)
        self._btn_box.rejected.connect(self.reject)
        self._btn_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        outer.addWidget(self._btn_box)

    # ── Tab 1: Source ──────────────────────────────────────────────────────────

    def _build_source_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)
        lay.setContentsMargins(12, 12, 12, 12)

        title = QLabel("Select data source:")
        title.setStyleSheet("font-weight: bold;")
        lay.addWidget(title)

        self._rb_local_pdf   = QRadioButton("Upload PDF — parse locally")
        self._rb_server_pdf  = QRadioButton("Upload PDF — parse on server")
        self._rb_fetch_id    = QRadioButton("Fetch from server by ESAF ID")
        self._rb_manual      = QRadioButton("Enter manually")
        self._rb_local_pdf.setChecked(True)

        if not self._has_server:
            self._rb_server_pdf.setEnabled(False)
            self._rb_server_pdf.setToolTip("No ESAF server URL configured in settings.")
            self._rb_fetch_id.setEnabled(False)
            self._rb_fetch_id.setToolTip("No ESAF server URL configured in settings.")

        for rb in (self._rb_local_pdf, self._rb_server_pdf, self._rb_fetch_id, self._rb_manual):
            lay.addWidget(rb)

        # PDF file row
        self._pdf_row = QWidget()
        pdf_h = QHBoxLayout(self._pdf_row)
        pdf_h.setContentsMargins(0, 0, 0, 0)
        self._pdf_path_edit = QLineEdit()
        self._pdf_path_edit.setPlaceholderText("Select a PDF file…")
        self._pdf_path_edit.setReadOnly(True)
        btn_browse_pdf = QPushButton("Browse…")
        btn_browse_pdf.setMaximumWidth(80)
        btn_browse_pdf.clicked.connect(self._browse_pdf)
        pdf_h.addWidget(self._pdf_path_edit)
        pdf_h.addWidget(btn_browse_pdf)
        lay.addWidget(self._pdf_row)

        # ESAF ID row (for fetch)
        self._id_row = QWidget()
        id_h = QHBoxLayout(self._id_row)
        id_h.setContentsMargins(0, 0, 0, 0)
        id_h.addWidget(QLabel("ESAF ID:"))
        self._id_edit = QLineEdit()
        self._id_edit.setPlaceholderText("e.g. 12345")
        id_h.addWidget(self._id_edit)
        self._id_row.setVisible(False)
        lay.addWidget(self._id_row)

        # Load/parse button
        self._btn_load = QPushButton("Load / Parse")
        self._btn_load.setMinimumWidth(120)
        self._btn_load.clicked.connect(self._on_load)
        load_row = QHBoxLayout()
        load_row.addWidget(self._btn_load)
        load_row.addStretch()
        lay.addLayout(load_row)

        self._source_status = QLabel("")
        self._source_status.setWordWrap(True)
        lay.addWidget(self._source_status)
        lay.addStretch()

        # Wire radio buttons to update visible rows
        for rb in (self._rb_local_pdf, self._rb_server_pdf):
            rb.toggled.connect(self._update_source_rows)
        self._rb_fetch_id.toggled.connect(self._update_source_rows)
        self._rb_manual.toggled.connect(self._update_source_rows)

        self._tabs.addTab(w, "1. Source")

    def _update_source_rows(self):
        is_pdf    = self._rb_local_pdf.isChecked() or self._rb_server_pdf.isChecked()
        is_fetch  = self._rb_fetch_id.isChecked()
        is_manual = self._rb_manual.isChecked()
        self._pdf_row.setVisible(is_pdf)
        self._id_row.setVisible(is_fetch)
        self._btn_load.setVisible(not is_manual)
        if is_manual:
            # Jump straight to Review with a blank record
            self._current_record = ESAFRecord(esaf_id="")
            self._current_confidence = {}
            self._review_widget.load(self._current_record, {})
            self._tabs.setCurrentIndex(1)
            self._btn_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def _browse_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ESAF PDF", "", "PDF Files (*.pdf);;All Files (*)"
        )
        if path:
            self._pdf_path_edit.setText(path)

    def _on_load(self):
        self._source_status.setText("Loading…")
        self._source_status.setStyleSheet(f"color: {_DIM};")
        self._btn_load.setEnabled(False)

        if self._rb_fetch_id.isChecked():
            esaf_id = self._id_edit.text().strip()
            if not esaf_id:
                self._source_status.setText("Enter an ESAF ID first.")
                self._source_status.setStyleSheet(f"color: {_DANGER};")
                self._btn_load.setEnabled(True)
                return
            self._worker = _FetchWorker(esaf_id, self._settings, parent=self)
            self._worker.done.connect(self._on_loaded)
            self._worker.error.connect(self._on_load_error)
            self._worker.start()

        else:  # PDF (local or server)
            path = self._pdf_path_edit.text().strip()
            if not path:
                self._source_status.setText("Select a PDF file first.")
                self._source_status.setStyleSheet(f"color: {_DANGER};")
                self._btn_load.setEnabled(True)
                return
            try:
                self._pdf_bytes = Path(path).read_bytes()
            except Exception as exc:
                self._source_status.setText(f"Cannot read file: {exc}")
                self._source_status.setStyleSheet(f"color: {_DANGER};")
                self._btn_load.setEnabled(True)
                return

            use_server = self._rb_server_pdf.isChecked()
            self._worker = _ParsePDFWorker(
                self._pdf_bytes, use_server, self._settings, parent=self
            )
            self._worker.done.connect(self._on_loaded)
            self._worker.error.connect(self._on_load_error)
            self._worker.start()

    def _on_loaded(self, record: ESAFRecord, confidence: dict):
        self._current_record = record
        self._current_confidence = confidence
        self._review_widget.load(record, confidence)
        self._source_status.setText(
            f"Loaded ESAF {record.esaf_id or '(unknown)'}.  "
            "Review the fields on the next tab."
        )
        self._source_status.setStyleSheet(f"color: {_SUCCESS};")
        self._btn_load.setEnabled(True)
        self._btn_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        # Auto-match PI group from users
        self._try_auto_match_pi(record)
        self._tabs.setCurrentIndex(1)

    def _on_load_error(self, msg: str):
        self._source_status.setText(f"Error: {msg}")
        self._source_status.setStyleSheet(f"color: {_DANGER};")
        self._btn_load.setEnabled(True)

    def _try_auto_match_pi(self, record: ESAFRecord):
        """Try to auto-match a PI group from the ESAF user list."""
        matched: list[PIGroup] = []
        matched_via = ""
        for user in record.users:
            if user.name:
                groups = PIGroupRegistry.find_by_member(user.name)
                if groups:
                    matched = groups
                    matched_via = user.name
                    break
        if matched:
            self._pi_picker.set_auto_match(matched, matched_via)

    # ── Tab 2: Review ──────────────────────────────────────────────────────────

    def _build_review_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        hint = QLabel(
            "Review and correct the extracted fields below.\n"
            "Label colour: green = high confidence, orange = uncertain, red = not found."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        lay.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._review_widget = ESAFReviewWidget()
        scroll.setWidget(self._review_widget)
        lay.addWidget(scroll, 1)

        self._tabs.addTab(w, "2. Review")

    # ── Tab 3: PI Group ────────────────────────────────────────────────────────

    def _build_pi_group_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        lbl = QLabel(
            "Assign this ESAF to a PI group.  "
            "The group is used to organise experiments and link them to a research group."
        )
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        grp_box = QGroupBox("PI Group")
        gb_lay = QVBoxLayout(grp_box)
        self._pi_picker = PIGroupPickerWidget()
        gb_lay.addWidget(self._pi_picker)
        lay.addWidget(grp_box)

        # ── Shared server sync ─────────────────────────────────────────────────
        from PyQt6.QtWidgets import QCheckBox
        if self._has_server:
            server_url = (self._settings.get("esaf_server_url") or "").strip()
            host = server_url.split("//")[-1].split("/")[0]
            srv_grp = QGroupBox("Shared MongoDB Server")
            srv_lay = QVBoxLayout(srv_grp)

            srv_note = QLabel(
                f"Server: <b>{server_url}</b><br>"
                "Saving to the server makes this ESAF visible to all connected apps "
                "on the same network."
            )
            srv_note.setTextFormat(Qt.TextFormat.RichText)
            srv_note.setWordWrap(True)
            srv_note.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
            srv_lay.addWidget(srv_note)

            self._cb_upload = QCheckBox(
                f"Save this ESAF to the shared server ({host})"
            )
            self._cb_upload.setChecked(True)   # default ON when server is configured
            srv_lay.addWidget(self._cb_upload)

            self._upload_status = QLabel("")
            self._upload_status.setWordWrap(True)
            self._upload_status.setStyleSheet("font-size: 10px;")
            srv_lay.addWidget(self._upload_status)

            lay.addWidget(srv_grp)
        else:
            self._cb_upload = None
            self._upload_status = QLabel("")

            no_srv = QLabel(
                "No ESAF server configured — record will be saved to the local cache only.\n"
                "To share ESAFs across machines, add a server URL in "
                "File → Connection Settings → ESAF Server."
            )
            no_srv.setWordWrap(True)
            no_srv.setStyleSheet(f"color: {_WARNING}; font-size: 11px;")
            lay.addWidget(no_srv)

        lay.addStretch()

        self._tabs.addTab(w, "3. PI Group")

    # ── Accept ─────────────────────────────────────────────────────────────────

    def _on_accept(self):
        # Read back from review widget
        record = self._review_widget.get_record()
        if not record.esaf_id:
            QMessageBox.warning(self, "ESAF ID required", "Please enter an ESAF ID.")
            self._tabs.setCurrentIndex(1)
            return

        # Merge source metadata from original parsed record
        if self._current_record:
            record.source = self._current_record.source
            record.raw_fields = self._current_record.raw_fields
            record.pi_group_slug = self._current_record.pi_group_slug

        # Assign PI group
        group = self._pi_picker.selected_group()
        if group:
            record.pi_group_slug = group.slug

        # Attach PDF availability flag
        if self._pdf_bytes is not None:
            record.pdf_available = True

        # Save to local cache
        save_cached(record)

        # Optionally upload to server
        if self._cb_upload is not None and self._cb_upload.isChecked():
            try:
                server_url = (self._settings.get("esaf_server_url") or "").strip()
                api_key = (self._settings.get("esaf_api_key") or "").strip()
                client = ESAFServerClient(server_url, api_key)
                # Try update first, then create
                try:
                    client.update_esaf(record)
                except ESAFServerError:
                    client.create_esaf(record)
                # Upload PDF if available
                if self._pdf_bytes is not None:
                    client.upload_pdf(record.esaf_id, self._pdf_bytes)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Server upload failed",
                    f"ESAF saved locally but server upload failed:\n{exc}"
                )

        self.result_record = record
        self.accept()


# ── _ExtraFieldsDialog ────────────────────────────────────────────────────────

class _ExtraFieldsDialog(QDialog):
    """Edit user-defined extra_fields on a cached ESAFRecord.

    Changes are written to the local cache on OK.  If the settings contain an
    ESAF server URL the dialog offers to push the changes there too.
    """

    def __init__(self, record: ESAFRecord, settings: dict, parent=None):
        super().__init__(parent)
        self._record   = record
        self._settings = settings
        self.setWindowTitle(f"Extra Fields — ESAF {record.esaf_id}")
        self.resize(500, 380)

        lay = QVBoxLayout(self)

        info = QLabel(
            "Add any custom key-value fields to this ESAF.  Use these to record "
            "beamline-specific metadata, approval notes, or any other information "
            "not captured by the standard fields.  Older ESAF entries leave these "
            "empty by default."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        lay.addWidget(info)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Field Name", "Value"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lay.addWidget(self._table)

        for k, v in (record.extra_fields or {}).items():
            self._add_row(k, str(v) if v is not None else "")

        btn_row = QHBoxLayout()
        btn_add = QPushButton("＋ Add field")
        btn_add.clicked.connect(self._add_empty_row)
        btn_remove = QPushButton("Remove selected")
        btn_remove.clicked.connect(self._remove_row)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_remove)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        server_url = (settings.get("esaf_server_url") or "").strip()
        if server_url:
            from PyQt6.QtWidgets import QCheckBox
            self._cb_push = QCheckBox(f"Push to server  ({server_url})")
            self._cb_push.setChecked(True)
            lay.addWidget(self._cb_push)
        else:
            self._cb_push = None

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add_row(self, key: str = "", value: str = ""):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(key))
        self._table.setItem(row, 1, QTableWidgetItem(value))

    def _add_empty_row(self):
        self._add_row()
        row = self._table.rowCount() - 1
        self._table.scrollToBottom()
        self._table.editItem(self._table.item(row, 0))

    def _remove_row(self):
        rows = sorted(
            set(idx.row() for idx in self._table.selectedIndexes()),
            reverse=True,
        )
        for row in rows:
            self._table.removeRow(row)

    def _read_table(self) -> dict:
        fields = {}
        for row in range(self._table.rowCount()):
            k_item = self._table.item(row, 0)
            v_item = self._table.item(row, 1)
            k = k_item.text().strip() if k_item else ""
            v = v_item.text().strip() if v_item else ""
            if k:
                fields[k] = v
        return fields

    def _on_ok(self):
        new_fields = self._read_table()
        old_fields = dict(self._record.extra_fields or {})

        # Persist to local cache
        cached = load_cached(self._record.esaf_id) or self._record
        cached.extra_fields = new_fields
        save_cached(cached)
        self._record = cached

        # Push to server (merge: None-values delete keys server-side)
        if self._cb_push and self._cb_push.isChecked():
            url = (self._settings.get("esaf_server_url") or "").strip()
            key = (self._settings.get("esaf_api_key") or "").strip()
            try:
                merge = dict(new_fields)
                for k in old_fields:
                    if k not in merge:
                        merge[k] = None
                ESAFServerClient(url, key).patch_extra_fields(
                    self._record.esaf_id, merge
                )
            except Exception as exc:
                QMessageBox.warning(
                    self, "Server push failed",
                    f"Extra fields saved locally, but server push failed:\n{exc}",
                )

        self.accept()


# ── ESAFPickerWidget ───────────────────────────────────────────────────────────

class ESAFPickerWidget(QWidget):
    """Compact widget for selecting an existing ESAF from the local cache.

    Shows a dropdown of cached ESAFs with a summary panel.  An "Import New ESAF…"
    button opens ESAFImportDialog.  A "Refresh from server" button re-syncs
    with the optional server.

    Signal ``esaf_selected`` emits the chosen ESAFRecord (or None).
    """

    esaf_selected = pyqtSignal(object)   # ESAFRecord or None

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._records: list[ESAFRecord] = []
        self._sync_worker = None   # _ServerSyncWorker | None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Top row: combo + buttons
        top_row = QHBoxLayout()

        from PyQt6.QtWidgets import QComboBox
        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_index_changed)
        top_row.addWidget(self._combo, 1)

        btn_import = QPushButton("Import New ESAF…")
        btn_import.clicked.connect(self._on_import)
        top_row.addWidget(btn_import)

        server_url = (settings.get("esaf_server_url") or "").strip()
        self._has_server = bool(server_url)
        if self._has_server:
            self._btn_refresh = QPushButton("⟳ Sync server")
            self._btn_refresh.setToolTip(f"Sync from {server_url}")
            self._btn_refresh.clicked.connect(self._on_refresh_server)
            top_row.addWidget(self._btn_refresh)

        lay.addLayout(top_row)

        # Summary panel
        self._summary_frame = QFrame()
        self._summary_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._summary_frame.setFrameShadow(QFrame.Shadow.Sunken)
        sf_lay = QFormLayout(self._summary_frame)
        sf_lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sf_lay.setHorizontalSpacing(10)
        sf_lay.setVerticalSpacing(3)

        self._sum_title     = QLabel("")
        self._sum_dates     = QLabel("")
        self._sum_beamline  = QLabel("")
        self._sum_pi_group  = QLabel("")
        self._sum_source    = QLabel("")
        self._sum_extra     = QLabel("")

        for lbl in (self._sum_title, self._sum_dates, self._sum_beamline,
                    self._sum_pi_group, self._sum_source, self._sum_extra):
            lbl.setWordWrap(True)

        sf_lay.addRow("Title:",        self._sum_title)
        sf_lay.addRow("Dates:",        self._sum_dates)
        sf_lay.addRow("Beamline:",     self._sum_beamline)
        sf_lay.addRow("PI group:",     self._sum_pi_group)
        sf_lay.addRow("Source:",       self._sum_source)
        sf_lay.addRow("Extra fields:", self._sum_extra)

        lay.addWidget(self._summary_frame)

        self._btn_edit_extra = QPushButton("Edit Extra Fields…")
        self._btn_edit_extra.setEnabled(False)
        self._btn_edit_extra.clicked.connect(self._on_edit_extra_fields)
        lay.addWidget(self._btn_edit_extra)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        lay.addWidget(self._status_lbl)

        # Load from local cache first, then kick off a background server sync
        self.refresh()
        if self._has_server:
            self._start_sync(auto=True)

    # ── public interface ───────────────────────────────────────────────────────

    def selected_esaf(self) -> ESAFRecord | None:
        """Return the currently selected ESAFRecord, or None."""
        idx = self._combo.currentIndex()
        if 0 <= idx < len(self._records):
            return self._records[idx]
        return None

    def refresh(self):
        """Reload from local cache (and server if available)."""
        self._records = list_cached()
        # Sort newest first by updated_at / created_at
        self._records.sort(
            key=lambda r: r.updated_at or r.created_at or "",
            reverse=True,
        )
        self._populate_combo()

    # ── internal ───────────────────────────────────────────────────────────────

    def _populate_combo(self):
        self._combo.blockSignals(True)
        self._combo.clear()
        if not self._records:
            self._combo.addItem("(no ESAFs in cache)")
        for r in self._records:
            date_str = r.start_date or r.updated_at[:10] if r.updated_at else ""
            label = f"{r.esaf_id}  —  {r.title or '(no title)'}  [{date_str}]"
            self._combo.addItem(label)
        self._combo.blockSignals(False)
        self._on_index_changed(self._combo.currentIndex())

    def _on_index_changed(self, idx: int):
        record = self.selected_esaf()
        if record is None:
            self._clear_summary()
            self._btn_edit_extra.setEnabled(False)
            self.esaf_selected.emit(None)
            return
        self._update_summary(record)
        self._btn_edit_extra.setEnabled(True)
        self.esaf_selected.emit(record)

    def _clear_summary(self):
        for lbl in (self._sum_title, self._sum_dates, self._sum_beamline,
                    self._sum_pi_group, self._sum_source, self._sum_extra):
            lbl.setText("")

    def _update_summary(self, record: ESAFRecord):
        self._sum_title.setText(record.title or "(no title)")
        dates = ""
        if record.start_date or record.end_date:
            dates = f"{record.start_date or '?'}  to  {record.end_date or '?'}"
        self._sum_dates.setText(dates)
        self._sum_beamline.setText(record.beamline or "")
        if record.pi_group_slug:
            g = PIGroupRegistry.get(record.pi_group_slug)
            self._sum_pi_group.setText(g.display_name if g else record.pi_group_slug)
        else:
            self._sum_pi_group.setText("(none)")
        self._sum_source.setText(record.source or "")
        n = len(record.extra_fields or {})
        self._sum_extra.setText(f"{n} field(s)" if n else "(none)")

    def _on_edit_extra_fields(self):
        record = self.selected_esaf()
        if record is None:
            return
        dlg = _ExtraFieldsDialog(record, self._settings, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            # Re-select the same ESAF after the list is rebuilt
            for i, r in enumerate(self._records):
                if r.esaf_id == record.esaf_id:
                    self._combo.setCurrentIndex(i)
                    break

    def _on_import(self):
        dlg = ESAFImportDialog(self._settings, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_record:
            self.refresh()
            # Select the newly imported record
            for i, r in enumerate(self._records):
                if r.esaf_id == dlg.result_record.esaf_id:
                    self._combo.setCurrentIndex(i)
                    break

    def _on_refresh_server(self):
        self._start_sync(auto=False)

    def _start_sync(self, auto: bool = False):
        """Start a background server sync. auto=True suppresses errors if unreachable."""
        if self._sync_worker and self._sync_worker.isRunning():
            return
        prefix = "Syncing" if auto else "Refreshing"
        self._status_lbl.setText(f"{prefix} from server…")
        self._status_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        if self._has_server:
            self._btn_refresh.setEnabled(False)
        self._sync_worker = _ServerSyncWorker(self._settings, parent=self)
        self._sync_worker.done.connect(lambda n: self._on_sync_done(n))
        self._sync_worker.error.connect(lambda e: self._on_sync_error(e, silent=auto))
        self._sync_worker.start()

    def _on_sync_done(self, count: int):
        server_url = (self._settings.get("esaf_server_url") or "").strip()
        host = server_url.split("//")[-1].split("/")[0]
        self._status_lbl.setText(f"✓  {count} ESAF(s) synced from {host}")
        self._status_lbl.setStyleSheet(f"color: {_SUCCESS}; font-size: 11px;")
        if self._has_server:
            self._btn_refresh.setEnabled(True)
        self.refresh()

    def _on_sync_error(self, msg: str, silent: bool = False):
        if silent:
            self._status_lbl.setText("Server unreachable — showing local cache.")
            self._status_lbl.setStyleSheet(f"color: {_WARNING}; font-size: 11px;")
        else:
            self._status_lbl.setText(f"✗  {msg}")
            self._status_lbl.setStyleSheet(f"color: {_DANGER}; font-size: 11px;")
        if self._has_server:
            self._btn_refresh.setEnabled(True)
