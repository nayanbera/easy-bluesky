"""experiments_tab.py — Experiments tab: experiment manager, queue, live plot, plan log."""

import json
import re
from datetime import datetime
from pathlib import Path

try:
    import h5py
    H5PY_AVAILABLE = True
except ImportError:
    H5PY_AVAILABLE = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QInputDialog, QFileDialog, QMessageBox,
    QAbstractItemView, QTabWidget, QComboBox, QPlainTextEdit, QDialog,
    QDialogButtonBox, QMainWindow, QLineEdit, QFormLayout, QGroupBox,
    QMenu, QFrame, QCheckBox, QSpinBox,
)
from PyQt6.QtCore import pyqtSignal, Qt, QThread, QTimer
from PyQt6.QtGui import QColor, QFont

from .config import (
    SUCCESS, DANGER, ACCENT,
    EXPERIMENTS_DIR, ACTIVE_EXPERIMENT_FILE, PLOT_COLORS,
)
from .live_viewer import LiveViewer
from .widgets import PlanDialog
from .queue_manager import RunDetailDialog

# ── Recent-experiments tracking file ──────────────────────────────────────────
# Stores experiments created or opened from ANY location (not just EXPERIMENTS_DIR).
# Per-computer — lives in ~/.easy_bluesky/ alongside connection.json.

_RECENT_FILE = Path.home() / ".easy_bluesky" / "recent_experiments.json"


def _load_recent_list() -> list:
    """Return list of {path, name, created, ...} dicts, newest first."""
    try:
        if _RECENT_FILE.exists():
            return json.loads(_RECENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_recent_list(entries: list):
    try:
        _RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RECENT_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception:
        pass


def _add_to_recent_list(path: str, info: dict):
    """Upsert an experiment into the recent list, keeping newest first."""
    entries = _load_recent_list()
    entries = [e for e in entries if e.get("path") != path]
    entries.insert(0, {"path": path, **info})
    _save_recent_list(entries[:30])

# Plans that never produce detector data — shown in neutral color in logs
_MOTION_PLANS = frozenset({
    "mv", "mvr", "abs_set", "rel_set", "move", "sleep", "rd", "set",
    "kickoff", "complete", "collect", "null",
})
_NEUTRAL_COLOR = "#aaaaaa"  # light grey for motion-only plans


def _is_motion_only(name: str, kwargs: dict) -> bool:
    return name.lower() in _MOTION_PLANS


# ── MongoDB-based HDF5 exporter (whole experiment) ────────────────────────────

class _MongoHDF5Exporter(QThread):
    """Export all runs for an experiment from MongoDB to one HDF5 file."""
    progress = pyqtSignal(int, int)   # (done, total)
    done     = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, host, port, db_name, exp_dir, entries, path, parent=None):
        super().__init__(parent)
        self._host    = host
        self._port    = port
        self._db      = db_name
        self._exp_dir = exp_dir
        self._entries = entries   # list of plans_log.jsonl dicts
        self._path    = path

    def run(self):
        try:
            import pymongo
            from .mongo_browser import _fetch_streams
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db = client[self._db]

            with h5py.File(self._path, "w") as hf:
                meta = hf.create_group("metadata")
                exp_name = Path(self._exp_dir).name if self._exp_dir else ""
                if exp_name:
                    meta.attrs["experiment_name"] = exp_name
                if self._exp_dir:
                    meta.attrs["exp_dir"] = self._exp_dir
                meta.attrs["n_scans"] = len(self._entries)

                for i, entry in enumerate(self._entries):
                    run_uids  = entry.get("run_uids", [])
                    uid       = run_uids[0] if run_uids else ""
                    scan_num  = entry.get("scan_num", i + 1)
                    name      = entry.get("name", "?")
                    kwargs    = entry.get("kwargs", {}) or {}
                    md        = kwargs.get("md", {}) or {}
                    grp_name  = f"scan_{scan_num:04d}"

                    grp = hf.create_group(grp_name)
                    grp.attrs["plan_name"]   = name
                    grp.attrs["scan_num"]    = int(scan_num)
                    grp.attrs["exit_status"] = entry.get("exit_status", "")
                    grp.attrs["timestamp"]   = entry.get("timestamp", "")
                    if entry.get("duration_s") is not None:
                        grp.attrs["duration_s"] = float(entry["duration_s"])
                    for attr in ("sample_name", "sample_description", "exp_dir"):
                        v = md.get(attr, "")
                        if v:
                            grp.attrs[attr] = str(v)
                    if uid:
                        grp.attrs["uid"] = uid

                    if uid:
                        streams = _fetch_streams(db, uid)
                        primary = streams.get("primary", {})
                        n_events = 0
                        for field, arr in primary.items():
                            if field == "data_keys":
                                continue
                            try:
                                grp.create_dataset(
                                    field, data=arr.astype(float), compression="gzip"
                                )
                                if field == "time":
                                    n_events = len(arr)
                            except Exception:
                                pass
                        if n_events:
                            grp.attrs["n_events"] = n_events

                    self.progress.emit(i + 1, len(self._entries))

            client.close()
            self.done.emit(self._path)
        except Exception as exc:
            self.error.emit(str(exc))


def _parse_jsonl_run(path) -> dict:
    """Parse a JSONL run file and return {field: numpy_array} for event data.

    Each line in the file is [doc_type, doc_body].  Collects data from
    'event' and 'event_page' documents and returns float arrays per field.
    Returns {} if no event data is found.
    """
    import numpy as _np

    fields: dict = {}   # field -> list of raw values

    try:
        with open(path) as _fh:
            for line in _fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc_type, doc = json.loads(line)
                except Exception:
                    continue

                if doc_type == "event":
                    data = doc.get("data", {})
                    for k, v in data.items():
                        fields.setdefault(k, []).append(v)

                elif doc_type == "event_page":
                    data     = doc.get("data", {})
                    seq_nums = doc.get("seq_num", [])
                    n        = len(seq_nums) if seq_nums else 0
                    for k, v_list in data.items():
                        if isinstance(v_list, list):
                            fields.setdefault(k, []).extend(v_list)
                        else:
                            fields.setdefault(k, []).extend([v_list] * n)
    except Exception:
        pass

    if not fields:
        return {}

    result = {}
    for k, vals in fields.items():
        arr = []
        for v in vals:
            try:
                arr.append(float(v))
            except (TypeError, ValueError):
                arr.append(float("nan"))
        result[k] = _np.array(arr)
    return result


class _JSONLHDFExporter(QThread):
    """Export all runs for an experiment from JSONL run files to one HDF5 file."""
    progress = pyqtSignal(int, int)   # (done, total)
    done     = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, exp_path, entries, path, parent=None):
        super().__init__(parent)
        self._exp_path = exp_path
        self._entries  = entries   # list of plans_log.jsonl dicts
        self._path     = path

    def run(self):
        try:
            with h5py.File(self._path, "w") as hf:
                meta     = hf.create_group("metadata")
                exp_name = Path(self._exp_path).name if self._exp_path else ""
                if exp_name:
                    meta.attrs["experiment_name"] = exp_name
                if self._exp_path:
                    meta.attrs["exp_dir"] = self._exp_path
                meta.attrs["n_scans"] = len(self._entries)

                for i, entry in enumerate(self._entries):
                    run_uids = entry.get("run_uids", [])
                    uid      = run_uids[0] if run_uids else ""
                    scan_num = entry.get("scan_num", i + 1)
                    name     = entry.get("name", "?")
                    kwargs   = entry.get("kwargs", {}) or {}
                    md       = kwargs.get("md", {}) or {}
                    grp_name = f"scan_{scan_num:04d}"

                    grp = hf.create_group(grp_name)
                    grp.attrs["plan_name"]   = name
                    grp.attrs["scan_num"]    = int(scan_num)
                    grp.attrs["exit_status"] = entry.get("exit_status", "")
                    grp.attrs["timestamp"]   = entry.get("timestamp", "")
                    if entry.get("duration_s") is not None:
                        grp.attrs["duration_s"] = float(entry["duration_s"])
                    for attr in ("sample_name", "sample_description", "exp_dir"):
                        v = md.get(attr, "")
                        if v:
                            grp.attrs[attr] = str(v)
                    if uid:
                        grp.attrs["uid"] = uid

                    if uid and self._exp_path:
                        jsonl_path = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                        if jsonl_path.exists():
                            data     = _parse_jsonl_run(jsonl_path)
                            n_events = 0
                            for field, arr in data.items():
                                try:
                                    grp.create_dataset(
                                        field, data=arr, compression="gzip"
                                    )
                                    if field == "time":
                                        n_events = len(arr)
                                except Exception:
                                    pass
                            if n_events:
                                grp.attrs["n_events"] = n_events
                            elif data:
                                # 'time' may not be present; count the first field
                                grp.attrs["n_events"] = len(next(iter(data.values())))

                    self.progress.emit(i + 1, len(self._entries))

            self.done.emit(self._path)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Placeholder class (kept to avoid AttributeError on legacy code paths) ─────

class _HistoryWidgetStub:
    """Minimal stub so any remaining references to history_widget don't crash."""
    def load_jsonl_file(self, *a):  pass
    def load_jsonl_files(self, *a): pass
    run_label = type("_L", (), {"setText": lambda *a: None})()


class _ESAFHealthWorker(QThread):
    """Background check of ESAF server health — non-blocking UI."""
    result = pyqtSignal(str, str)   # (status, detail)  status: ok_mongo|ok_sqlite|error|unconfigured

    def __init__(self, url: str, api_key: str, parent=None):
        super().__init__(parent)
        self._url     = url.strip()
        self._api_key = api_key.strip()

    def run(self):
        if not self._url:
            self.result.emit("unconfigured", "")
            return
        try:
            from .esaf import ESAFServerClient
            client = ESAFServerClient(self._url, self._api_key)
            data   = client.health()
            backend = data.get("backend", "unknown")
            if backend == "mongodb":
                self.result.emit("ok_mongo", self._url)
            else:
                self.result.emit("ok_sqlite", self._url)
        except Exception as exc:
            self.result.emit("error", str(exc))




# ── Startup experiment picker ──────────────────────────────────────────────────

class _StartupExperimentDialog(QDialog):
    """
    Shown once at app launch so the user explicitly picks an experiment
    rather than silently restoring the last session's folder.
    """

    def __init__(self, recent_experiments: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Experiment")
        self.setMinimumWidth(540)
        self.setMinimumHeight(340)
        self.action = None   # 'new' | 'open' | (path, info) tuple

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        lay.addWidget(QLabel("Select an experiment to work with, or create a new one:"))

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        for path, info in recent_experiments:
            name    = info.get("name", Path(path).name)
            created = info.get("created", "")[:10]
            short   = path if len(path) <= 60 else "…" + path[-59:]
            label   = f"{name}  ({created})" if created else name
            li = QListWidgetItem(label)
            li.setToolTip(path)
            li.setData(Qt.ItemDataRole.UserRole,     path)
            li.setData(Qt.ItemDataRole.UserRole + 1, info)
            dim = QLabel(short)   # we store as tooltip, display via text
            li.setData(Qt.ItemDataRole.UserRole + 2, short)
            self._list.addItem(li)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self._list, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        btn_row = QHBoxLayout()
        btn_new  = QPushButton("New Experiment…")
        btn_new.setObjectName("btn_primary")
        btn_new.clicked.connect(self._on_new)
        btn_open = QPushButton("Open Folder…")
        btn_open.clicked.connect(self._on_open)
        btn_skip = QPushButton("Skip")
        btn_skip.setToolTip("Continue without selecting an experiment")
        btn_skip.clicked.connect(self.reject)
        self._btn_load = QPushButton("Load Selected")
        self._btn_load.setEnabled(False)
        self._btn_load.clicked.connect(self._on_load)

        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_open)
        btn_row.addStretch()
        btn_row.addWidget(btn_skip)
        btn_row.addWidget(self._btn_load)
        lay.addLayout(btn_row)

        self._list.currentItemChanged.connect(
            lambda cur, _: self._btn_load.setEnabled(cur is not None)
        )
        if self._list.count():
            self._list.setCurrentRow(0)

    def _on_new(self):
        self.action = "new"
        self.accept()

    def _on_open(self):
        self.action = "open"
        self.accept()

    def _on_load(self):
        li = self._list.currentItem()
        if li:
            self.action = (li.data(Qt.ItemDataRole.UserRole),
                           li.data(Qt.ItemDataRole.UserRole + 1))
            self.accept()

    def _on_double_click(self, li: QListWidgetItem):
        self.action = (li.data(Qt.ItemDataRole.UserRole),
                       li.data(Qt.ItemDataRole.UserRole + 1))
        self.accept()


# ── New experiment dialog ─────────────────────────────────────────────────────

class _NewExperimentDialog(QDialog):
    """Two-tab dialog for opening or creating an experiment.

    Tab 1 "From ESAF" — open-or-create within the canonical ESAF folder structure:
        ``<experiments_root>/<pi_slug>/ESAF-<id>_<start_date>/<experiment_name>``
        Existing experiments are listed; selecting one opens it.
        Typing a new name creates a new experiment subfolder.
    Tab 2 "Manual" — the original free-form name + local/remote paths.

    Result attributes (set on accept):
        experiment_name    : str  — folder name (ESAF-id_date or manual name)
        local_parent_dir   : str  — parent dir; final path = parent/sanitized_name
        remote_exp_dir     : str  — full remote path (may be empty)
        esaf_info          : dict — ESAF metadata for experiment.json (may be {})
        open_existing_path : str  — if non-empty, open this existing experiment
                                    instead of creating a new one
    """

    def __init__(self, remote_data_root: str = "", local_data_root: str = "",
                 settings: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open / New Experiment")
        self.setMinimumWidth(600)
        self.setMinimumHeight(480)
        self._remote_data_root = remote_data_root.rstrip("/")
        self._local_data_root  = local_data_root.rstrip("/") or EXPERIMENTS_DIR
        self._settings         = settings or {}
        self.experiment_name    = ""
        self.local_parent_dir   = ""
        self.remote_exp_dir     = ""
        self.esaf_info          = {}
        self.open_existing_path = ""   # set when opening an existing run
        self._selected_esaf     = None
        self._build()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        lay.addWidget(self._tabs, 1)

        self._build_esaf_tab()
        self._build_manual_tab()

        self._ok_btn = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn.accepted.connect(self._on_accept)
        self._ok_btn.rejected.connect(self.reject)
        lay.addWidget(self._ok_btn)

    # ── Tab 1: From ESAF ──────────────────────────────────────────────────────

    def _build_esaf_tab(self):
        from .esaf_dialog import ESAFPickerWidget

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        esaf_grp = QGroupBox("ESAF")
        eg_lay = QVBoxLayout(esaf_grp)
        self._esaf_picker = ESAFPickerWidget(self._settings)
        self._esaf_picker.esaf_selected.connect(self._on_esaf_selected)
        eg_lay.addWidget(self._esaf_picker)
        lay.addWidget(esaf_grp)

        # Seed _selected_esaf: the picker emits esaf_selected during __init__,
        # before the signal was connected, so we read it back explicitly here.
        self._selected_esaf = self._esaf_picker.selected_esaf()

        # ── Existing experiments ───────────────────────────────────────────────
        runs_lbl = QLabel("Existing experiments under this ESAF:")
        runs_lbl.setStyleSheet("font-weight: bold;")
        lay.addWidget(runs_lbl)

        self._runs_list = QListWidget()
        self._runs_list.setMaximumHeight(130)
        self._runs_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._runs_list.itemSelectionChanged.connect(self._on_run_selected)
        self._runs_list.itemDoubleClicked.connect(self._on_accept)
        self._no_runs_lbl = QLabel("  (no experiments yet — select an ESAF above)")
        self._no_runs_lbl.setStyleSheet("color: #888; font-style: italic;")
        lay.addWidget(self._runs_list)
        lay.addWidget(self._no_runs_lbl)

        # ── Separator ──────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        lay.addWidget(sep)

        new_lbl = QLabel("— or create a new experiment —")
        new_lbl.setStyleSheet("color: #888; font-size: 10px;")
        new_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(new_lbl)

        new_form = QFormLayout()
        new_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        new_form.setHorizontalSpacing(12)
        self._esaf_run_name = QLineEdit()
        self._esaf_run_name.setPlaceholderText("experiment name (e.g. SAXS_day1)")
        self._esaf_run_name.textChanged.connect(self._on_new_run_name_changed)
        new_form.addRow("New experiment name:", self._esaf_run_name)
        lay.addLayout(new_form)

        # ── Path preview ───────────────────────────────────────────────────────
        path_grp = QGroupBox("Path")
        pg_lay = QFormLayout(path_grp)
        pg_lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        pg_lay.setHorizontalSpacing(10)
        self._esaf_local_lbl = QLabel("(select an ESAF above)")
        self._esaf_local_lbl.setWordWrap(True)
        self._esaf_local_lbl.setStyleSheet("font-size: 10px;")
        pg_lay.addRow("Local:", self._esaf_local_lbl)
        self._esaf_remote_lbl = QLabel(
            "(not configured)" if not self._remote_data_root else "(select an ESAF above)"
        )
        self._esaf_remote_lbl.setWordWrap(True)
        self._esaf_remote_lbl.setStyleSheet("font-size: 10px;")
        pg_lay.addRow("Remote:", self._esaf_remote_lbl)
        lay.addWidget(path_grp)

        self._tabs.addTab(w, "From ESAF")
        self._refresh_runs_list()
        self._update_esaf_paths()

    def _esaf_base_path(self) -> str | None:
        """Return the ESAF folder path (pi_slug/ESAF-id_date) or None if no ESAF selected."""
        rec = self._selected_esaf
        if rec is None:
            return None
        pi_slug = rec.pi_group_slug or "no_pi_group"
        esaf_folder = f"ESAF-{rec.esaf_id}"
        if rec.start_date:
            esaf_folder += f"_{rec.start_date}"
        return "/".join([self._local_data_root, pi_slug, esaf_folder])

    def _on_esaf_selected(self, record):
        self._selected_esaf = record
        self._refresh_runs_list()
        self._update_esaf_paths()

    def _refresh_runs_list(self):
        """Scan the ESAF folder on disk and populate the existing-runs list."""
        self._runs_list.clear()
        base = self._esaf_base_path()
        if base is None:
            self._runs_list.setVisible(False)
            self._no_runs_lbl.setText("  (none yet — select an ESAF above)")
            self._no_runs_lbl.setVisible(True)
            return

        base_path = Path(base)
        runs = []
        if base_path.is_dir():
            for child in sorted(base_path.iterdir()):
                if child.is_dir() and (child / "experiment.json").exists():
                    try:
                        info = json.loads((child / "experiment.json").read_text())
                        created = info.get("created", "")[:16].replace("T", "  ")
                        label = f"{child.name}   —   created {created}" if created else child.name
                    except Exception:
                        label = child.name
                    runs.append((child.name, str(child), label))

        if runs:
            for name, path, label in runs:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._runs_list.addItem(item)
            self._runs_list.setVisible(True)
            self._no_runs_lbl.setVisible(False)
        else:
            self._runs_list.setVisible(False)
            self._no_runs_lbl.setText("  (no experiments yet under this ESAF)")
            self._no_runs_lbl.setVisible(True)

    def _on_run_selected(self):
        """When an existing run is selected, clear the new-run name field."""
        if self._runs_list.currentItem():
            self._esaf_run_name.blockSignals(True)
            self._esaf_run_name.clear()
            self._esaf_run_name.blockSignals(False)
            self._update_esaf_paths()

    def _on_new_run_name_changed(self, text: str):
        """When typing a new name, deselect any existing run."""
        if text:
            self._runs_list.clearSelection()
        self._update_esaf_paths()

    def _update_esaf_paths(self):
        base = self._esaf_base_path()
        if base is None:
            self._esaf_local_lbl.setText("(select an ESAF above)")
            self._esaf_remote_lbl.setText("(select an ESAF above)")
            return

        # Determine the run name to preview
        selected_item = self._runs_list.currentItem()
        if selected_item:
            # Opening existing — show its full path
            existing_path = selected_item.data(Qt.ItemDataRole.UserRole)
            self._esaf_local_lbl.setText(f"Open: {existing_path}")
            self._esaf_remote_lbl.setText("(remote path stored in experiment.json)")
            return

        run = self._sanitize(self._esaf_run_name.text().strip())
        rec = self._selected_esaf

        pi_slug = rec.pi_group_slug or "no_pi_group"
        esaf_folder = f"ESAF-{rec.esaf_id}"
        if rec.start_date:
            esaf_folder += f"_{rec.start_date}"

        parts_local  = [self._local_data_root, pi_slug, esaf_folder]
        parts_remote = ([self._remote_data_root, pi_slug, esaf_folder]
                        if self._remote_data_root else [])
        if run:
            parts_local.append(run)
            if parts_remote:
                parts_remote.append(run)

        self._esaf_local_lbl.setText("/".join(parts_local) + ("/" if not run else ""))
        if parts_remote:
            self._esaf_remote_lbl.setText("/".join(parts_remote) + ("/" if not run else ""))
        else:
            self._esaf_remote_lbl.setText("(remote_data_root not set in profile)")

    # ── Tab 2: Manual ─────────────────────────────────────────────────────────

    def _build_manual_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        name_form = QFormLayout()
        name_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        name_form.setHorizontalSpacing(12)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. SAXS_run_2026")
        self._name_edit.textChanged.connect(self._on_name_changed)
        name_form.addRow("Experiment name:", self._name_edit)
        lay.addLayout(name_form)

        local_grp = QGroupBox("Local Path  (this computer)")
        local_lay = QVBoxLayout(local_grp)
        local_form = QFormLayout()
        local_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        local_form.setHorizontalSpacing(12)
        local_row = QHBoxLayout()
        self._local_edit = QLineEdit(self._local_data_root)
        btn_local = QPushButton("Browse…")
        btn_local.setMaximumWidth(70)
        btn_local.clicked.connect(self._browse_local)
        local_row.addWidget(self._local_edit)
        local_row.addWidget(btn_local)
        local_form.addRow("Parent folder:", local_row)
        self._local_result_lbl = QLabel("")
        self._local_result_lbl.setObjectName("dim_text")
        self._local_result_lbl.setStyleSheet("font-size: 10px;")
        local_form.addRow("Will create:", self._local_result_lbl)
        local_lay.addLayout(local_form)
        lay.addWidget(local_grp)

        remote_grp = QGroupBox("Remote Path  (RE machine — for detector files)")
        remote_lay = QVBoxLayout(remote_grp)
        remote_note = QLabel(
            "Path on the Linux beamline machine where detector data is saved.\n"
            "Injected as  <b>remote_exp_dir</b>  in every plan's metadata.\n"
            "Leave empty if not using a remote detector path."
        )
        remote_note.setTextFormat(Qt.TextFormat.RichText)
        remote_note.setWordWrap(True)
        remote_note.setObjectName("dim_text")
        remote_lay.addWidget(remote_note)
        remote_form = QFormLayout()
        remote_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        remote_form.setHorizontalSpacing(12)
        remote_row = QHBoxLayout()
        self._remote_edit = QLineEdit()
        self._remote_edit.setPlaceholderText(
            "/home/chem_epics/data/experiment_name  (optional)"
        )
        self._btn_browse_remote = QPushButton("Browse…")
        self._btn_browse_remote.setMaximumWidth(70)
        self._btn_browse_remote.clicked.connect(self._browse_remote)
        remote_row.addWidget(self._remote_edit)
        remote_row.addWidget(self._btn_browse_remote)
        remote_form.addRow("Remote path:", remote_row)
        remote_lay.addLayout(remote_form)
        lay.addWidget(remote_grp)
        lay.addStretch()

        self._tabs.addTab(w, "Manual")
        self._update_local_label()

    def _on_tab_changed(self, _):
        pass

    # ── Shared helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize(name: str) -> str:
        return re.sub(r"[^\w\-]", "_", name.strip())

    def _on_name_changed(self, text: str):
        self._update_local_label()
        if self._remote_data_root:
            sanitized = self._sanitize(text)
            self._remote_edit.setText(
                (self._remote_data_root + "/" + sanitized) if sanitized else ""
            )

    def _update_local_label(self):
        name   = self._name_edit.text().strip()
        parent = self._local_edit.text().strip()
        if name and parent:
            self._local_result_lbl.setText(f"{parent}/{self._sanitize(name)}")
        else:
            self._local_result_lbl.setText("")

    def _browse_local(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose parent folder for experiment",
            self._local_edit.text() or self._local_data_root,
        )
        if path:
            self._local_edit.setText(path)
            self._update_local_label()

    def _browse_remote(self):
        from .connection_settings import RemotePathBrowser, is_local_host
        settings = self._settings
        current  = self._remote_edit.text().strip() or self._remote_data_root or "~"
        if is_local_host(settings) or not settings.get("host", ""):
            path = QFileDialog.getExistingDirectory(
                self, "Select Remote Data Directory",
                current if current != "~" else str(Path.home()),
            )
            if path:
                self._remote_edit.setText(path)
            return
        dlg = RemotePathBrowser(settings, initial_path=current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_path:
            self._remote_edit.setText(dlg.selected_path)

    # ── Accept ─────────────────────────────────────────────────────────────────

    def _on_accept(self):
        if self._tabs.currentIndex() == 0:
            self._accept_esaf()
        else:
            self._accept_manual()

    def _accept_esaf(self):
        rec = self._selected_esaf
        if rec is None:
            QMessageBox.warning(self, "Required", "Select an ESAF or import one first.")
            return

        # ── Open existing run ──────────────────────────────────────────────────
        selected_item = self._runs_list.currentItem()
        if selected_item:
            self.open_existing_path = selected_item.data(Qt.ItemDataRole.UserRole)
            self.accept()
            return

        # ── Create new run ─────────────────────────────────────────────────────
        run_raw = self._esaf_run_name.text().strip()
        if not run_raw:
            QMessageBox.warning(self, "Required",
                                "Select an existing experiment from the list or enter a new experiment name.")
            return
        run = self._sanitize(run_raw)

        pi_slug     = rec.pi_group_slug or "no_pi_group"
        esaf_folder = f"ESAF-{rec.esaf_id}"
        if rec.start_date:
            esaf_folder += f"_{rec.start_date}"

        local_parent = "/".join([self._local_data_root, pi_slug, esaf_folder])
        remote_parts = ([self._remote_data_root, pi_slug, esaf_folder, run]
                        if self._remote_data_root else [])

        self.experiment_name    = run
        self.local_parent_dir   = local_parent
        self.remote_exp_dir     = "/".join(remote_parts) if remote_parts else ""
        self.open_existing_path = ""
        from .esaf import PIGroupRegistry
        pi_group = PIGroupRegistry.get(pi_slug)
        self.esaf_info = {
            "esaf_id":         rec.esaf_id,
            "pi_group":        pi_slug,
            "pi_name":         pi_group.pi_name         if pi_group else "",
            "pi_institution":  pi_group.pi_institution  if pi_group else "",
            "proposal_id":     rec.proposal_id,
            "esaf_start_date": rec.start_date,
            "title":           rec.title,
            "beamline":        rec.beamline,
        }
        self.accept()

    def _accept_manual(self):
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Required", "Experiment name is required.")
            return
        parent = self._local_edit.text().strip()
        if not parent:
            QMessageBox.warning(self, "Required", "Local parent folder is required.")
            return
        self.experiment_name  = name
        self.local_parent_dir = parent
        self.remote_exp_dir   = self._remote_edit.text().strip()
        self.esaf_info        = {}
        self.accept()


# ── Main experiments tab ───────────────────────────────────────────────────────

class ExperimentsTab(QWidget):
    """Three-panel layout:
      Left  — experiment info, sample fields, plan log
      Middle — compact queue + console
      Right  — Live plot tab (detachable)
    """

    experiment_changed = pyqtSignal(str)   # emits runs_dir path
    scan_completed     = pyqtSignal()      # emits when a new scan is logged
    start_requested    = pyqtSignal()
    pause_requested    = pyqtSignal()
    resume_requested   = pyqtSignal()
    abort_requested    = pyqtSignal()
    stop_requested     = pyqtSignal()
    auto_start_toggled = pyqtSignal(bool)
    loop_count_changed = pyqtSignal(int)   # 0 = ∞; -1 = loop disabled

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker            = worker
        self._plans: dict      = {}
        self._devices: dict    = {}
        self._active_exp_path  = ""
        self._remote_exp_dir: str = ""   # Linux path on RE machine for detector files
        self._esaf_info: dict  = {}      # esaf_id, pi_group, proposal_id, esaf_start_date
        self._logged_uids: set = set()
        self._shown_error_uids: set = set()
        self._exp_created_at: float = 0.0
        self._exp_end_time: float   = 0.0
        self._next_scan_num: int    = 1
        self._detached_win     = None
        self._plot_placeholder = None
        self._sample_name: str = ""
        self._sample_description: str = ""
        self._settings: dict   = {}         # connection settings for MongoDB export
        self._active_profile: str = ""      # current profile name — gates history logging
        self._suppressed_uids: set = set()  # manually deleted UIDs — never re-logged
        self._hdf5_exporter    = None
        self.history_widget    = _HistoryWidgetStub()
        self._build()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_middle())
        splitter.addWidget(self._build_right())
        splitter.setSizes([240, 260, 720])
        lay.addWidget(splitter)

    # ── Left panel: experiment info + sample + plan log ────────────────────────

    def _build_left(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 4, 8)
        vlay.setSpacing(6)

        lbl_active = QLabel("ACTIVE EXPERIMENT")
        lbl_active.setObjectName("section_title")
        vlay.addWidget(lbl_active)

        self.exp_name_label = QLabel("—")
        self.exp_name_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        vlay.addWidget(self.exp_name_label)

        self.exp_path_label = QLabel("")
        self.exp_path_label.setObjectName("dim_text")
        self.exp_path_label.setStyleSheet("font-size: 10px;")
        self.exp_path_label.setWordWrap(True)
        vlay.addWidget(self.exp_path_label)

        self.exp_remote_label = QLabel("")
        self.exp_remote_label.setObjectName("dim_text")
        self.exp_remote_label.setStyleSheet("font-size: 10px;")
        self.exp_remote_label.setWordWrap(True)
        vlay.addWidget(self.exp_remote_label)

        self.exp_date_label = QLabel("")
        self.exp_date_label.setObjectName("dim_text")
        self.exp_date_label.setStyleSheet("font-size: 10px;")
        vlay.addWidget(self.exp_date_label)

        # ── ESAF server status ─────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #444;")
        vlay.addWidget(sep)

        esaf_row = QHBoxLayout()
        esaf_row.setSpacing(4)
        self._esaf_dot = QLabel("●")
        self._esaf_dot.setStyleSheet("font-size: 12px; color: #888888;")
        self._esaf_status = QLabel("ESAF server not configured")
        self._esaf_status.setStyleSheet("font-size: 10px; color: #888888;")
        self._esaf_status.setWordWrap(True)
        self._btn_check_esaf = QPushButton("Check")
        self._btn_check_esaf.setFixedWidth(46)
        self._btn_check_esaf.setFixedHeight(20)
        self._btn_check_esaf.setStyleSheet("font-size: 10px;")
        self._btn_check_esaf.clicked.connect(self._check_esaf_server)
        esaf_row.addWidget(self._esaf_dot)
        esaf_row.addWidget(self._esaf_status, 1)
        esaf_row.addWidget(self._btn_check_esaf)
        vlay.addLayout(esaf_row)

        btn_row = QHBoxLayout()
        btn_new  = QPushButton("New Experiment")
        btn_new.setObjectName("btn_primary")
        btn_new.clicked.connect(self.new_experiment)
        btn_open = QPushButton("Open…")
        btn_open.clicked.connect(self.open_experiment)
        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_open)
        vlay.addLayout(btn_row)

        # ── Sample fields ──────────────────────────────────────────────────────
        sample_grp = QGroupBox("Sample")
        sample_lay = QFormLayout(sample_grp)
        sample_lay.setSpacing(4)

        self.sample_name_edit = QLineEdit()
        self.sample_name_edit.setPlaceholderText("e.g. Si_wafer_01")
        self.sample_name_edit.editingFinished.connect(self._on_sample_name_commit)
        sample_lay.addRow("Name:", self.sample_name_edit)

        self.sample_desc_edit = QLineEdit()
        self.sample_desc_edit.setPlaceholderText("optional description")
        self.sample_desc_edit.editingFinished.connect(self._on_sample_desc_commit)
        sample_lay.addRow("Desc:", self.sample_desc_edit)

        self.sample_dir_label = QLabel("—")
        self.sample_dir_label.setObjectName("dim_text")
        self.sample_dir_label.setStyleSheet("font-size: 10px;")
        self.sample_dir_label.setWordWrap(True)
        sample_lay.addRow("Folder:", self.sample_dir_label)

        vlay.addWidget(sample_grp)

        lbl_log = QLabel("PLAN LOG")
        lbl_log.setObjectName("section_title")
        vlay.addWidget(lbl_log)

        self._plan_log_search = QLineEdit()
        self._plan_log_search.setPlaceholderText("🔍  Search plan log…")
        self._plan_log_search.setClearButtonEnabled(True)
        self._plan_log_search.textChanged.connect(self._filter_plan_log)
        vlay.addWidget(self._plan_log_search)

        self.plan_log_list = QListWidget()
        self.plan_log_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.plan_log_list.itemSelectionChanged.connect(self._on_plan_log_selection_changed)
        self.plan_log_list.itemDoubleClicked.connect(self._on_plan_log_double_clicked)
        self.plan_log_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plan_log_list.customContextMenuRequested.connect(self._plan_log_context_menu)
        vlay.addWidget(self.plan_log_list, 1)

        self._btn_requeue = QPushButton("↑  Add Selected to Queue")
        self._btn_requeue.setEnabled(False)
        self._btn_requeue.setToolTip(
            "Add all selected plan log entries to the queue using their saved settings.\n"
            "Double-click a single entry to edit it before queuing."
        )
        self._btn_requeue.clicked.connect(self._requeue_selected)
        vlay.addWidget(self._btn_requeue)

        self._btn_export_h5 = QPushButton("Export HDF5…")
        self._btn_export_h5.setObjectName("btn_primary")
        self._btn_export_h5.setToolTip("Save all scan data to a single HDF5 file")
        self._btn_export_h5.clicked.connect(self._export_hdf5)
        if not H5PY_AVAILABLE:
            self._btn_export_h5.setEnabled(False)
            self._btn_export_h5.setToolTip("pip install h5py to enable HDF5 export")
        vlay.addWidget(self._btn_export_h5)

        return w

    # ── Middle panel: queue + buttons + console ────────────────────────────────

    def _build_middle(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 8, 4, 8)
        vlay.setSpacing(4)

        # Queue execution buttons
        exec_row = QHBoxLayout()
        exec_row.setSpacing(4)
        self.btn_q_start  = QPushButton("▶ Start")
        self.btn_q_start.setObjectName("btn_primary")
        self.btn_q_pause  = QPushButton("⏸ Pause")
        self.btn_q_resume = QPushButton("▶▶ Resume")
        self.btn_q_resume.setObjectName("btn_success")
        self.btn_q_abort  = QPushButton("✕ Abort")
        self.btn_q_abort.setObjectName("btn_danger")
        self.btn_q_stop   = QPushButton("⬛ Stop")
        for btn in (self.btn_q_start, self.btn_q_pause, self.btn_q_resume,
                    self.btn_q_abort, self.btn_q_stop):
            btn.setEnabled(False)
            exec_row.addWidget(btn)
        exec_row.addStretch()
        vlay.addLayout(exec_row)

        # Wire execution buttons
        self.btn_q_start.clicked.connect(self.start_requested)
        self.btn_q_pause.clicked.connect(self.pause_requested)
        self.btn_q_resume.clicked.connect(self.resume_requested)
        self.btn_q_abort.clicked.connect(self.abort_requested)
        self.btn_q_stop.clicked.connect(self.stop_requested)

        # Auto-start + Loop row
        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)

        self.chk_auto_start = QCheckBox("Auto-start")
        self.chk_auto_start.setToolTip(
            "Automatically start the queue when the first plan is added\n"
            "and the RE is idle.")
        opt_row.addWidget(self.chk_auto_start)

        vsep1 = QFrame()
        vsep1.setFrameShape(QFrame.Shape.VLine)
        vsep1.setFrameShadow(QFrame.Shadow.Sunken)
        vsep1.setFixedWidth(1)
        opt_row.addWidget(vsep1)

        self.chk_loop = QCheckBox("Loop")
        self.chk_loop.setToolTip("Re-run the queue repeatedly after it finishes.")
        opt_row.addWidget(self.chk_loop)

        self.spin_loop = QSpinBox()
        self.spin_loop.setRange(0, 9999)
        self.spin_loop.setValue(0)
        self.spin_loop.setSpecialValueText("∞")
        self.spin_loop.setToolTip("0 = loop forever; N = repeat N more times after the first run.")
        self.spin_loop.setFixedWidth(64)
        self.spin_loop.setEnabled(False)
        opt_row.addWidget(self.spin_loop)

        lbl_times = QLabel("times")
        lbl_times.setStyleSheet("font-size: 11px;")
        opt_row.addWidget(lbl_times)

        self._loop_iter_lbl = QLabel("")
        self._loop_iter_lbl.setStyleSheet("font-size: 11px; color: #e8a44a; font-style: italic;")
        opt_row.addWidget(self._loop_iter_lbl)

        opt_row.addStretch()
        vlay.addLayout(opt_row)

        # Wire auto-start + loop
        self.chk_auto_start.toggled.connect(self.auto_start_toggled)
        self.chk_loop.toggled.connect(self._on_loop_checkbox)
        self.spin_loop.valueChanged.connect(self.loop_count_changed)

        q_hdr = QHBoxLayout()
        lbl_q = QLabel("QUEUE")
        lbl_q.setObjectName("section_title")
        q_hdr.addWidget(lbl_q)
        q_hdr.addStretch()
        self.queue_count_label = QLabel("0 items")
        self.queue_count_label.setObjectName("dim_text")
        q_hdr.addWidget(self.queue_count_label)
        vlay.addLayout(q_hdr)

        self.queue_compact = QListWidget()
        self.queue_compact.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_compact.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.queue_compact.setToolTip("Double-click to edit plan; Ctrl/Shift-click to select multiple; drag to reorder")
        self.queue_compact.itemDoubleClicked.connect(self._on_queue_item_clicked)
        self.queue_compact.model().rowsMoved.connect(self._on_compact_queue_reorder)
        vlay.addWidget(self.queue_compact, 1)

        q_btns = QHBoxLayout()
        q_btns.setSpacing(4)
        btn_add = QPushButton("＋ Add")
        btn_add.setObjectName("btn_primary")
        btn_add.clicked.connect(self._add_plan)
        btn_rem = QPushButton("Remove")
        btn_rem.clicked.connect(self._remove_plan)
        btn_save = QPushButton("💾 Save…")
        btn_save.setToolTip("Save the current queue to a .queue file")
        btn_save.clicked.connect(self._save_queue)
        btn_load = QPushButton("📂 Load…")
        btn_load.setToolTip("Load a .queue file and append its plans to the current queue")
        btn_load.clicked.connect(self._load_queue)
        btn_clr = QPushButton("Clear")
        btn_clr.setObjectName("btn_danger")
        btn_clr.clicked.connect(self._clear_queue)
        q_btns.addWidget(btn_add)
        q_btns.addWidget(btn_rem)
        q_btns.addWidget(btn_save)
        q_btns.addWidget(btn_load)
        q_btns.addStretch()
        q_btns.addWidget(btn_clr)
        vlay.addLayout(q_btns)

        lbl_con = QLabel("CONSOLE")
        lbl_con.setObjectName("section_title")
        vlay.addWidget(lbl_con)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Courier New", 10))
        self.console.setPlaceholderText("RE manager output…")
        self.console.setMaximumHeight(160)
        vlay.addWidget(self.console)

        return w

    # ── Right panel: detachable plot tabs ──────────────────────────────────────

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 8, 8)
        lay.setSpacing(0)

        self._plot_container = QWidget()
        self._plot_container_lay = QVBoxLayout(self._plot_container)
        self._plot_container_lay.setContentsMargins(0, 0, 0, 0)
        self._plot_container_lay.setSpacing(0)

        self.plot_tabs   = QTabWidget()
        self.live_viewer = LiveViewer(worker=self.worker)
        self.plot_tabs.addTab(self.live_viewer, "📡  Live")

        self._detach_btn = QPushButton("⊔  Detach")
        self._detach_btn.setFixedHeight(22)
        self._detach_btn.setStyleSheet("font-size: 11px; padding: 0 8px; margin: 1px;")
        self._detach_btn.setToolTip("Detach plots into a floating window")
        self._detach_btn.clicked.connect(self._toggle_detach)
        self.plot_tabs.setCornerWidget(self._detach_btn, Qt.Corner.TopRightCorner)

        self._plot_container_lay.addWidget(self.plot_tabs)
        lay.addWidget(self._plot_container, 1)
        return w

    # ── Plot detach / reattach ─────────────────────────────────────────────────

    def _toggle_detach(self):
        if self._detached_win is None:
            self._do_detach()
        else:
            self._do_reattach()

    def _do_detach(self):
        self._plot_container_lay.removeWidget(self.plot_tabs)
        self._plot_placeholder = QLabel(
            "Plots are in a floating window.\nClose it to re-attach.")
        self._plot_placeholder.setObjectName("dim_text")
        self._plot_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plot_placeholder.setStyleSheet("font-size: 14px;")
        self._plot_container_lay.addWidget(self._plot_placeholder)

        win = QMainWindow()
        win.setWindowTitle("EasyBluesky — Plots")
        win.setMinimumSize(900, 600)
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        cw  = QWidget()
        cl  = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        self.plot_tabs.setParent(cw)
        cl.addWidget(self.plot_tabs)
        win.setCentralWidget(cw)

        def _close(event):
            self._do_reattach()
            event.accept()
        win.closeEvent = _close

        self._detached_win = win
        win.show()
        self._detach_btn.setText("⊓  Re-attach")

    def _do_reattach(self):
        if not self._detached_win:
            return
        if self._plot_placeholder:
            self._plot_container_lay.removeWidget(self._plot_placeholder)
            self._plot_placeholder.deleteLater()
            self._plot_placeholder = None
        self.plot_tabs.setParent(self._plot_container)
        self._plot_container_lay.addWidget(self.plot_tabs)
        self._detached_win.hide()
        self._detached_win = None
        self._detach_btn.setText("⊔  Detach")

    # ── Settings (for MongoDB-backed HDF5 export) ─────────────────────────────

    def update_settings(self, settings: dict):
        self._settings = settings
        self._check_esaf_server()

    def _check_esaf_server(self):
        """Spawn a background thread to ping the ESAF server health endpoint."""
        url = (self._settings.get("esaf_server_url") or "").strip()
        key = (self._settings.get("esaf_api_key") or "").strip()
        if not url:
            self._on_esaf_health_result("unconfigured", "")
            return
        self._esaf_dot.setStyleSheet("font-size: 12px; color: #888888;")
        self._esaf_status.setText("Checking…")
        self._esaf_status.setStyleSheet("font-size: 10px; color: #888888;")
        self._btn_check_esaf.setEnabled(False)
        worker = _ESAFHealthWorker(url, key, self)
        worker.result.connect(self._on_esaf_health_result)
        worker.finished.connect(lambda: self._btn_check_esaf.setEnabled(True))
        worker.start()

    def _on_esaf_health_result(self, status: str, detail: str):
        self._btn_check_esaf.setEnabled(True)
        if status == "unconfigured":
            self._esaf_dot.setStyleSheet("font-size: 12px; color: #888888;")
            self._esaf_status.setText("ESAF server not configured")
            self._esaf_status.setStyleSheet("font-size: 10px; color: #888888;")
        elif status == "ok_mongo":
            self._esaf_dot.setStyleSheet("font-size: 12px; color: #33aa44;")
            self._esaf_status.setText("Connected · MongoDB")
            self._esaf_status.setStyleSheet("font-size: 10px; color: #33aa44;")
        elif status == "ok_sqlite":
            self._esaf_dot.setStyleSheet("font-size: 12px; color: #cc8800;")
            self._esaf_status.setText("Connected · SQLite")
            self._esaf_status.setStyleSheet("font-size: 10px; color: #cc8800;")
        else:
            self._esaf_dot.setStyleSheet("font-size: 12px; color: #cc3333;")
            host = detail.split("/")[2] if "//" in detail else detail
            self._esaf_status.setText(f"Unreachable ({host})" if host else "Unreachable")
            self._esaf_status.setStyleSheet("font-size: 10px; color: #cc3333;")

    def set_profile(self, profile_name: str):
        """Switch the active profile — clears the current experiment and loads the
        one saved for the new profile, if any.  Called by main.py on profile change."""
        if profile_name == self._active_profile:
            return
        self._active_profile = profile_name
        # Clear current state so history from the old profile is not mixed in.
        self._active_exp_path  = ""
        self._remote_exp_dir   = ""
        self._esaf_info        = {}
        self._logged_uids      = set()
        self._suppressed_uids  = set()
        self._shown_error_uids = set()
        self._exp_created_at   = 0.0
        self._exp_end_time     = 0.0
        self._next_scan_num    = 1
        self._clear_sample()
        # Reset display labels — _set_active_experiment will re-populate them
        # if a saved experiment exists for this profile.
        self.exp_name_label.setText("—")
        self.exp_path_label.setText("")
        self.exp_remote_label.setText("")
        self.exp_date_label.setText("")
        self.plan_log_list.clear()
        self._load_active_experiment()
        # Notify the rest of the app (mongo browser, RE startup md, etc.)
        if self._active_exp_path:
            runs_dir = str(Path(self._active_exp_path) / "runs")
            self.experiment_changed.emit(runs_dir)
        self._check_esaf_server()

    # ── Queue operations ───────────────────────────────────────────────────────

    def _build_metadata(self) -> dict:
        """Build md dict injected automatically into every submitted plan."""
        md: dict = {}
        if self._active_exp_path:
            md["exp_dir"] = self._active_exp_path
        if self._remote_exp_dir:
            md["remote_exp_dir"] = self._remote_exp_dir
        if self._sample_name:
            md["sample_name"] = self._sample_name
        if self._sample_description:
            md["sample_description"] = self._sample_description
        # ESAF metadata — injected when an experiment was created from an ESAF
        esaf = getattr(self, "_esaf_info", {})
        for key in ("esaf_id", "pi_name", "pi_institution", "proposal_id",
                    "esaf_start_date", "pi_group"):
            if esaf.get(key):
                md[key] = esaf[key]
        # Backfill pi_name/pi_institution from PIGroupRegistry for older experiments
        # that only stored the pi_group slug in experiment.json.
        if md.get("pi_group") and not (md.get("pi_name") and md.get("pi_institution")):
            from .esaf import PIGroupRegistry
            g = PIGroupRegistry.get(md["pi_group"])
            if g:
                md.setdefault("pi_name", g.pi_name)
                md.setdefault("pi_institution", g.pi_institution)
        return md

    def _inject_metadata(self, result_item: dict):
        """Inject experiment/sample metadata into a plan item's md key."""
        auto_md = self._build_metadata()
        if not auto_md:
            return result_item
        # Only inject md if the plan actually accepts it — plan stubs like
        # sleep/mv/mvr do not, and the queue server will reject the item.
        plan_name = result_item.get("name", "")
        plan_info = self._plans.get(plan_name, {})
        params = plan_info.get("parameters", []) if plan_info else []
        if not any(p.get("name") == "md" for p in params):
            return result_item
        existing_md = result_item.get("kwargs", {}).get("md", {}) or {}
        merged = {**auto_md, **existing_md}   # user-supplied md wins
        result_item.setdefault("kwargs", {})["md"] = merged
        return result_item

    def _add_plan(self):
        if not self.worker:
            return
        dlg = PlanDialog(self._plans, self._devices, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            item = self._inject_metadata(dlg.result_item)
            ok, msg = self.worker.add_item(item)
            self._log(f"{'✓' if ok else '✗'} Add plan: {msg}")

    def _on_compact_queue_reorder(self, parent, start, end, dest, row):
        QTimer.singleShot(100, self._sync_compact_queue_order)

    def _sync_compact_queue_order(self):
        if not self.worker:
            return
        for i in range(self.queue_compact.count()):
            uid = self.queue_compact.item(i).data(Qt.ItemDataRole.UserRole)
            if uid:
                self.worker.move_item(uid, i)

    def _remove_plan(self):
        if not self.worker:
            return
        selected = self.queue_compact.selectedItems()
        if not selected:
            return
        removed = 0
        for li in selected:
            uid = li.data(Qt.ItemDataRole.UserRole)
            if uid:
                ok, msg = self.worker.remove_item(uid)
                if ok:
                    removed += 1
                else:
                    self._log(f"✗ Remove: {msg}")
        if removed:
            self._log(f"✓ Removed {removed} plan(s) from queue")

    def _clear_queue(self):
        if not self.worker:
            return
        r = QMessageBox.question(self, "Clear Queue",
                                 "Remove all items from the queue?")
        if r == QMessageBox.StandardButton.Yes:
            ok, msg = self.worker.clear_queue()
            self._log(f"{'✓' if ok else '✗'} Clear queue: {msg}")

    def _save_queue(self):
        items = []
        for i in range(self.queue_compact.count()):
            raw = self.queue_compact.item(i).data(Qt.ItemDataRole.UserRole + 1)
            if raw:
                clean = {k: v for k, v in raw.items()
                         if k not in ("item_uid", "status", "result", "msg")}
                items.append(clean)
        if not items:
            QMessageBox.information(self, "Empty Queue", "Queue is empty — nothing to save.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Queue", "", "Queue Files (*.queue);;All Files (*)"
        )
        if not path:
            return
        if not path.endswith(".queue"):
            path += ".queue"
        try:
            Path(path).write_text(json.dumps(items, indent=2), encoding="utf-8")
            self._log(f"✓ Saved {len(items)} plan(s) → {Path(path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", str(e))

    def _load_queue(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Queue", "", "Queue Files (*.queue);;All Files (*)"
        )
        if not path:
            return
        try:
            items = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "Load Failed", f"Could not read queue file:\n{e}")
            return
        if not isinstance(items, list):
            QMessageBox.critical(self, "Invalid Format", "Queue file must contain a JSON array.")
            return
        added = failed = 0
        for item in items:
            if not isinstance(item, dict) or "name" not in item:
                failed += 1
                continue
            item.setdefault("item_type", "plan")
            ok, _ = self.worker.add_item(item)
            if ok:
                added += 1
            else:
                failed += 1
        self._log(f"{'✓' if not failed else '⚠'} Loaded queue: {added} added"
                  + (f", {failed} failed" if failed else ""))

    def _on_queue_item_clicked(self, li: QListWidgetItem):
        if not self.worker:
            return
        item = li.data(Qt.ItemDataRole.UserRole + 1)
        if not item:
            return
        dlg = PlanDialog(self._plans, self._devices, item=item, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            updated = self._inject_metadata(dlg.result_item)
            ok, msg = self.worker.update_item(updated)
            self._log(f"{'✓' if ok else '✗'} Update plan: {msg}")

    # ── Console ────────────────────────────────────────────────────────────────

    def append_console(self, text: str):
        self.console.appendPlainText(text)
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_console(f"[{ts}] {msg}")

    # ── RE-status-driven button enable/disable ─────────────────────────────────

    def update_re_status(self, status: dict) -> None:
        """Enable/disable execution buttons based on RE state."""
        env_state = status.get("worker_environment_state", "")
        if not env_state:
            env_state = "idle" if status.get("worker_environment_exists") else "closed"
        re_state = status.get("re_state", "")
        env_open = env_state not in ("", "closed")
        running  = re_state == "running"
        paused   = re_state == "paused"
        idle     = re_state in ("", "idle") and env_open
        self.btn_q_start.setEnabled(idle)
        self.btn_q_pause.setEnabled(running)
        self.btn_q_resume.setEnabled(paused)
        self.btn_q_abort.setEnabled(running or paused)
        self.btn_q_stop.setEnabled(running or paused)

    def on_disconnected(self) -> None:
        for b in (self.btn_q_start, self.btn_q_pause, self.btn_q_resume,
                  self.btn_q_abort, self.btn_q_stop):
            b.setEnabled(False)

    def _on_loop_checkbox(self, checked: bool) -> None:
        self.spin_loop.setEnabled(checked)
        if not checked:
            self._loop_iter_lbl.setText("")
        self.loop_count_changed.emit(self.spin_loop.value() if checked else -1)

    def set_loop_iteration(self, current: int, total: int) -> None:
        if total == 0:
            self._loop_iter_lbl.setText(f"(iteration {current}, ∞)")
        elif total > 0:
            self._loop_iter_lbl.setText(f"(iteration {current} of {total + current - 1})")
        else:
            self._loop_iter_lbl.setText("")

    def clear_loop_iteration(self) -> None:
        self._loop_iter_lbl.setText("")

    # ── Public setters ─────────────────────────────────────────────────────────

    def is_ready_to_run(self) -> tuple:
        """Return (ok: bool, reason: str). ok is False if prerequisites are missing."""
        if not self._active_exp_path:
            return False, "No active experiment — create or open one first."
        if not self._sample_name:
            return False, "Sample name is required before starting the queue.\n\nEnter a sample name in the Experiments tab."
        return True, ""

    def set_plans(self, plans: dict):
        self._plans = plans

    def set_devices(self, devices: dict):
        self._devices = devices

    # ── Sample management ──────────────────────────────────────────────────────

    def _on_sample_name_commit(self):
        name = self.sample_name_edit.text().strip()
        if not name or name == self._sample_name:
            return
        if not self._active_exp_path:
            QMessageBox.warning(self, "No Experiment",
                                "Open or create an experiment first.")
            self.sample_name_edit.setText(self._sample_name)
            return
        safe = re.sub(r"[^\w\-]", "_", name)
        sample_dir = Path(self._active_exp_path) / "samples" / safe
        if sample_dir.exists():
            r = QMessageBox.question(
                self, "Sample Exists",
                f"Sample folder '{safe}' already exists.\nUse it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                self.sample_name_edit.setText(self._sample_name)
                return
        else:
            try:
                sample_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(self, "Error",
                                     f"Could not create sample folder:\n{e}")
                self.sample_name_edit.setText(self._sample_name)
                return
        self._sample_name = name
        display = str(sample_dir) if len(str(sample_dir)) <= 55 else "…" + str(sample_dir)[-54:]
        self.sample_dir_label.setText(display)
        self._log(f"✓ Sample: {safe}")

    def _on_sample_desc_commit(self):
        self._sample_description = self.sample_desc_edit.text().strip()

    def _clear_sample(self):
        self._sample_name = ""
        self._sample_description = ""
        self.sample_name_edit.clear()
        self.sample_desc_edit.clear()
        self.sample_dir_label.setText("—")

    # ── Experiment management ──────────────────────────────────────────────────

    def new_experiment(self):
        from .connection_settings import get_active_profile
        profile          = get_active_profile(self._settings)
        remote_data_root = profile.get("remote_data_root", "").strip()
        local_data_root  = profile.get("local_data_root", "").strip()

        dlg = _NewExperimentDialog(
            remote_data_root=remote_data_root,
            local_data_root=local_data_root,
            settings=self._settings,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # ── Open existing run (From ESAF → selected from list) ────────────────
        if dlg.open_existing_path:
            self._open_experiment_at(dlg.open_existing_path)
            return

        # ── Create new run ─────────────────────────────────────────────────────
        name           = dlg.experiment_name
        parent_dir     = dlg.local_parent_dir
        remote_exp_dir = dlg.remote_exp_dir
        esaf_info      = dlg.esaf_info

        ts        = datetime.now()
        sanitized = re.sub(r"[^\w\-]", "_", name)
        exp_dir   = Path(parent_dir) / sanitized
        if exp_dir.exists():
            suffix = 2
            while (Path(parent_dir) / f"{sanitized}_{suffix}").exists():
                suffix += 1
            exp_dir = Path(parent_dir) / f"{sanitized}_{suffix}"
        runs_dir = exp_dir / "runs"
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Could not create experiment folder:\n{e}")
            return

        exp_info = {
            "name":           name,
            "created":        ts.isoformat(),
            "description":    "",
            "remote_exp_dir": remote_exp_dir,
        }
        if esaf_info:
            exp_info["esaf"] = esaf_info
        (exp_dir / "experiment.json").write_text(json.dumps(exp_info, indent=2))

        active_info = {
            "name":           name,
            "path":           str(exp_dir),
            "created":        ts.isoformat(),
            "remote_exp_dir": remote_exp_dir,
        }
        if esaf_info:
            active_info["esaf"] = esaf_info
        self._write_active_experiment(active_info)
        self._set_active_experiment(str(exp_dir), active_info)
        self._clear_sample()
        self.experiment_changed.emit(str(runs_dir))
        self._exp_end_time = self._compute_exp_end_time()

    def open_experiment(self):
        path = QFileDialog.getExistingDirectory(self, "Open Experiment Folder")
        if not path:
            return
        self._open_experiment_at(path)

    def _open_experiment_at(self, path: str):
        """Open an experiment folder by absolute path (shared by open_experiment and new_experiment)."""
        exp_json = Path(path) / "experiment.json"
        if not exp_json.exists():
            QMessageBox.warning(
                self, "Invalid Folder",
                "Selected folder does not contain experiment.json.")
            return
        try:
            info = json.loads(exp_json.read_text())
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read experiment.json:\n{e}")
            return

        active_info = {
            "name":           info.get("name", Path(path).name),
            "path":           path,
            "created":        info.get("created", ""),
            "remote_exp_dir": info.get("remote_exp_dir", ""),
        }
        if info.get("esaf"):
            active_info["esaf"] = info["esaf"]
        self._write_active_experiment(active_info)
        self._set_active_experiment(path, active_info)
        self._clear_sample()
        runs_dir = Path(path) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_changed.emit(str(runs_dir))

    def prompt_experiment_on_startup(self):
        """Show the startup experiment picker.  Called once after the main window shows."""
        recent = self.get_recent_experiments(12)
        dlg = _StartupExperimentDialog(recent, parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.action is None:
            return
        if dlg.action == "new":
            self.new_experiment()
        elif dlg.action == "open":
            self.open_experiment()
        elif isinstance(dlg.action, tuple):
            path, info = dlg.action
            self.load_experiment(path, info)

    def load_experiment(self, path: str, info: dict = None):
        """Public entry point called from File → Recent Experiments menu."""
        if not Path(path).exists():
            QMessageBox.warning(self, "Not Found",
                                f"Experiment folder not found:\n{path}")
            return
        if info is None:
            exp_json = Path(path) / "experiment.json"
            try:
                info = json.loads(exp_json.read_text())
            except Exception:
                info = {"name": Path(path).name, "path": path, "created": ""}

        active_info = {
            "name":           info.get("name", Path(path).name),
            "path":           path,
            "created":        info.get("created", ""),
            "remote_exp_dir": info.get("remote_exp_dir", ""),
        }
        self._write_active_experiment(active_info)
        self._set_active_experiment(path, active_info)
        self._clear_sample()
        runs_dir = Path(path) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_changed.emit(str(runs_dir))

    def get_recent_experiments(self, limit: int = 12) -> list:
        """Return list of (path, info) tuples, most-recent first.

        Merges the explicit tracking file (all created/opened experiments,
        any location) with a scan of EXPERIMENTS_DIR (backward compat for
        experiments that existed before tracking was introduced).
        """
        seen: set  = set()
        merged: list = []   # (sort_key, path, info)

        # Tracked list — experiments created/opened from anywhere on this computer
        for entry in _load_recent_list():
            path = entry.get("path", "")
            if not path or not Path(path).exists():
                continue
            info = {k: v for k, v in entry.items() if k != "path"}
            seen.add(path)
            merged.append((entry.get("created", ""), path, info))

        # Scan EXPERIMENTS_DIR — picks up experiments created before tracking
        exps_dir = Path(EXPERIMENTS_DIR)
        if exps_dir.exists():
            for d in exps_dir.iterdir():
                if not d.is_dir() or str(d) in seen:
                    continue
                exp_json = d / "experiment.json"
                if not exp_json.exists():
                    continue
                try:
                    info = json.loads(exp_json.read_text())
                    merged.append((info.get("created", ""), str(d), info))
                    seen.add(str(d))
                except Exception:
                    pass

        merged.sort(key=lambda x: x[0], reverse=True)
        return [(path, info) for _, path, info in merged[:limit]]

    # ── HDF5 export ────────────────────────────────────────────────────────────

    def _export_hdf5(self):
        if not H5PY_AVAILABLE:
            QMessageBox.warning(self, "h5py Missing",
                                "Install h5py first:\n  pip install h5py")
            return
        if not self._active_exp_path:
            QMessageBox.warning(self, "No Experiment",
                                "Open or create an experiment first.")
            return

        from .connection_settings import get_active_profile
        profile = get_active_profile(self._settings)
        db      = profile.get("mongo_db", "").strip()
        host    = profile.get("mongo_host", "") or "localhost"
        port    = int(profile.get("mongo_port", 27017))

        log_file = Path(self._active_exp_path) / "plans_log.jsonl"
        if not log_file.exists():
            QMessageBox.warning(self, "No Data",
                                "No plan log found for this experiment.")
            return

        entries: list = []
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as e:
            QMessageBox.critical(self, "Read Error", str(e))
            return

        next_n = 1
        for e in entries:
            if e.get("scan_num") is None:
                e["scan_num"] = next_n
            next_n = max(next_n, e.get("scan_num", 0)) + 1

        exp_name     = self.exp_name_label.text()
        default_path = str(Path(self._active_exp_path) / f"{exp_name}.h5")
        path, _      = QFileDialog.getSaveFileName(
            self, "Export Experiment to HDF5", default_path,
            "HDF5 Files (*.h5 *.hdf5)"
        )
        if not path:
            return

        if self._hdf5_exporter and self._hdf5_exporter.isRunning():
            QMessageBox.warning(self, "Busy", "An export is already in progress.")
            return

        self._btn_export_h5.setEnabled(False)

        if db:
            self._log(f"Exporting {len(entries)} scans to HDF5 via MongoDB…")
            self._hdf5_exporter = _MongoHDF5Exporter(
                host, port, db, self._active_exp_path, entries, path, parent=self
            )
            self._hdf5_exporter.progress.connect(
                lambda done, total: self._log(f"  Exporting… {done}/{total}")
            )
            self._hdf5_exporter.done.connect(self._on_hdf5_done)
            self._hdf5_exporter.error.connect(self._on_hdf5_error)
            self._hdf5_exporter.start()
        else:
            runs_dir   = Path(self._active_exp_path) / "runs"
            jsonl_files = list(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
            if not jsonl_files:
                self._btn_export_h5.setEnabled(True)
                QMessageBox.information(
                    self, "No Run Files",
                    f"No MongoDB configured and no JSONL run files found in\n"
                    f"{runs_dir}.\n\nRun at least one scan first."
                )
                return
            self._log(f"Exporting {len(entries)} scans to HDF5 via JSONL files…")
            self._hdf5_exporter = _JSONLHDFExporter(
                self._active_exp_path, entries, path, parent=self
            )
            self._hdf5_exporter.progress.connect(
                lambda done, total: self._log(f"  Exporting… {done}/{total}")
            )
            self._hdf5_exporter.done.connect(self._on_hdf5_done)
            self._hdf5_exporter.error.connect(self._on_hdf5_error)
            self._hdf5_exporter.start()

    def _on_hdf5_done(self, path: str):
        self._btn_export_h5.setEnabled(True)
        n = len(self.plan_log_list)
        self._log(f"✓ Exported → {Path(path).name}")
        QMessageBox.information(self, "Export Complete", f"Saved to:\n{path}")

    def _on_hdf5_error(self, msg: str):
        self._btn_export_h5.setEnabled(True)
        self._log(f"✗ HDF5 export failed: {msg}")
        QMessageBox.critical(self, "Export Failed", msg)

    def _write_active_experiment(self, info: dict):
        active_file = Path(ACTIVE_EXPERIMENT_FILE)
        active_file.parent.mkdir(parents=True, exist_ok=True)
        # Store per-profile so switching profiles restores the correct experiment.
        try:
            existing = json.loads(active_file.read_text()) if active_file.exists() else {}
        except Exception:
            existing = {}
        # Migrate legacy flat format (single dict with "path" key) on first write.
        if "path" in existing:
            existing = {}
        profile_key = self._active_profile or "__default__"
        existing[profile_key] = info
        active_file.write_text(json.dumps(existing, indent=2))

    def _compute_exp_end_time(self) -> float:
        if not self._exp_created_at:
            return 0.0
        exps_dir = Path(EXPERIMENTS_DIR)
        if not exps_dir.exists():
            return 0.0
        next_t = float("inf")
        for d in exps_dir.iterdir():
            if not d.is_dir() or str(d) == self._active_exp_path:
                continue
            exp_json = d / "experiment.json"
            if not exp_json.exists():
                continue
            try:
                info = json.loads(exp_json.read_text())
                ct = datetime.fromisoformat(info.get("created", "")).timestamp()
                if ct > self._exp_created_at and ct < next_t:
                    next_t = ct
            except Exception:
                pass
        return next_t if next_t != float("inf") else 0.0

    def _set_active_experiment(self, path: str, info: dict):
        _add_to_recent_list(path, info)
        self._active_exp_path = path
        self._remote_exp_dir  = info.get("remote_exp_dir", "")
        self._esaf_info       = info.get("esaf", {})
        if self.worker and hasattr(self.worker, "set_doc_writer_exp_dir"):
            self.worker.set_doc_writer_exp_dir(path)
        self._logged_uids     = set()
        self._suppressed_uids = set()   # suppressions are per-experiment
        created = info.get("created", "")
        try:
            self._exp_created_at = datetime.fromisoformat(created).timestamp()
        except Exception:
            self._exp_created_at = 0.0
        self._exp_end_time = self._compute_exp_end_time()

        name = info.get("name", Path(path).name)
        display_path = path if len(path) <= 60 else "…" + path[-59:]
        self.exp_name_label.setText(name)
        self.exp_path_label.setText(display_path)
        if self._remote_exp_dir:
            remote_display = (self._remote_exp_dir if len(self._remote_exp_dir) <= 55
                              else "…" + self._remote_exp_dir[-54:])
            self.exp_remote_label.setText(f"Remote: {remote_display}")
        else:
            self.exp_remote_label.setText("")
        self.exp_date_label.setText(f"Created: {created[:10]}" if created else "")
        self._load_plan_log(path)

    def _load_active_experiment(self):
        active_file = Path(ACTIVE_EXPERIMENT_FILE)
        if not active_file.exists():
            return
        try:
            data = json.loads(active_file.read_text())
            # Legacy flat format (single dict with "path" key) — treat as __default__
            if "path" in data:
                info = data
            else:
                profile_key = self._active_profile or "__default__"
                info = data.get(profile_key) or {}
            path = info.get("path", "")
            if path and Path(path).exists():
                self._set_active_experiment(path, info)
        except Exception:
            pass

    # ── Plan log ───────────────────────────────────────────────────────────────

    @staticmethod
    def _plan_summary(name: str, kwargs: dict, args: list = None) -> str:
        args    = list(args or [])
        parts   = []
        name_lc = name.lower()

        # ── Readable (detectors) ──────────────────────────────────────────────
        dets = kwargs.get("detectors") or kwargs.get("detector_list", [])
        if isinstance(dets, str):
            dets = [dets]
        # Fallback: first positional arg is often the detectors list
        if not dets and args and isinstance(args[0], list):
            dets = args[0]
        if dets:
            parts.append("det: [" + ", ".join(str(d) for d in dets) + "]")

        # ── Movable (motor) ───────────────────────────────────────────────────
        motor  = kwargs.get("motor")
        motors = kwargs.get("motors")
        if not motor and isinstance(motors, list) and motors:
            motor = motors[0]
        if not motor and name_lc in _MOTION_PLANS and args:
            motor = args[0]
        # scan-style: [dets_list, motor, start, stop, ...] in positional args
        if not motor and name_lc not in _MOTION_PLANS:
            if len(args) >= 2 and isinstance(args[0], list) and not isinstance(args[1], (int, float)):
                motor = args[1]

        if motor:
            start = kwargs.get("start")
            stop  = kwargs.get("stop")
            num   = kwargs.get("num")
            # args-based layout: [dets_list, motor, start, stop, ...]
            if start is None and len(args) >= 4 and isinstance(args[0], list):
                try:
                    start, stop = float(args[2]), float(args[3])
                except (TypeError, ValueError):
                    pass
            s = f"mot: {motor}"
            if start is not None and stop is not None:
                s += f" [{start} → {stop}"
                if num is not None:
                    s += f", {num} pts"
                s += "]"
            elif name_lc in ("mv", "mvr") and len(args) >= 2:
                try:
                    s += f" → {float(args[1]):.4g}"
                except (TypeError, ValueError):
                    s += f" → {args[1]}"
            parts.insert(0, s)

        # ── num pts / delay (when not already shown inside the motor range) ───
        num_shown = any("pts" in p for p in parts)
        num = kwargs.get("num")
        if num is not None and not num_shown:
            parts.append(f"{num} pts")
        delay = kwargs.get("delay")
        if delay is not None:
            parts.append(f"delay={delay:.4g} s")

        return "  " + "  |  ".join(parts) if parts else ""

    def _entry_belongs_here(self, entry: dict) -> bool:
        """True when this entry's timestamp falls within the experiment's lifetime."""
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
            if self._exp_created_at and ts < self._exp_created_at:
                return False
            if self._exp_end_time and ts >= self._exp_end_time:
                return False
            return True
        except Exception:
            return True

    def _filter_plan_log(self, text: str):
        q = text.strip().lower()
        for i in range(self.plan_log_list.count()):
            item = self.plan_log_list.item(i)
            item.setHidden(bool(q and q not in item.text().lower()))

    def _suppressed_file(self, exp_path: str) -> Path:
        return Path(exp_path) / "suppressed_uids.json"

    def _load_suppressed_uids(self, exp_path: str) -> None:
        try:
            data = json.loads(self._suppressed_file(exp_path).read_text())
            self._suppressed_uids = set(data) if isinstance(data, list) else set()
        except Exception:
            self._suppressed_uids = set()

    def _save_suppressed_uids(self, exp_path: str) -> None:
        try:
            self._suppressed_file(exp_path).write_text(
                json.dumps(sorted(self._suppressed_uids), indent=2)
            )
        except Exception:
            pass

    def _load_plan_log(self, exp_path: str, auto_select_newest: bool = False):
        log_file = Path(exp_path) / "plans_log.jsonl"
        self.plan_log_list.clear()
        self._logged_uids = set()
        self._load_suppressed_uids(exp_path)   # restore persisted deletions

        # Collect UIDs from all experiments so we don't double-log after switching.
        exps_dir = Path(EXPERIMENTS_DIR)
        if exps_dir.exists():
            for d in exps_dir.iterdir():
                other_log = d / "plans_log.jsonl"
                if other_log.exists():
                    try:
                        with open(other_log) as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                uid = json.loads(line).get("uid", "")
                                if uid:
                                    self._logged_uids.add(uid)
                    except Exception:
                        pass

        if not log_file.exists():
            return
        try:
            # Read ALL raw entries without timestamp filtering.
            all_entries = []
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        all_entries.append(entry)
                        uid = entry.get("uid", "")
                        if uid:
                            self._logged_uids.add(uid)
                    except Exception:
                        pass

            # Sort chronologically by timestamp so display and numbering are
            # consistent even when entries were appended out of order.
            def _ts_key(e):
                try:
                    return datetime.fromisoformat(e.get("timestamp", "")).timestamp()
                except Exception:
                    return 0.0
            all_entries.sort(key=_ts_key)

            # Renumber in time order: entries with non-empty run_uids are actual
            # scans and get sequential numbers 1, 2, 3…; motion-only plans (empty
            # run_uids) get scan_num = None.  The MongoDB browser column shows the
            # same time-ordered position, so the numbers match across both tables.
            scan_counter = 1
            file_changed = True   # always write back after sort
            for e in all_entries:
                new_num = scan_counter if e.get("run_uids") else None
                if e.get("scan_num") != new_num:
                    e["scan_num"] = new_num
                    file_changed = True
                if e.get("run_uids"):
                    scan_counter += 1
            self._next_scan_num = scan_counter  # next unused number

            if file_changed:
                try:
                    with open(log_file, "w") as f:
                        for e in all_entries:
                            f.write(json.dumps(e) + "\n")
                except Exception:
                    pass

            # Show all entries in the file — no time-window cap.
            entries = all_entries

            for entry in reversed(entries):
                name    = entry.get("name", "?")
                args    = entry.get("args", []) or []
                kwargs  = entry.get("kwargs", {}) or {}
                status  = entry.get("exit_status", "")
                ok      = status in ("completed", "success")
                motion  = _is_motion_only(name, kwargs)
                icon    = "✓" if ok else ("✗" if status else "?")
                if motion:
                    color = _NEUTRAL_COLOR
                else:
                    color = SUCCESS if ok else DANGER
                ts       = entry.get("timestamp", "")
                t_str    = ts[11:19] if len(ts) >= 19 else ts[:19]
                dur      = entry.get("duration_s")
                scan_num = entry.get("scan_num")
                summary  = self._plan_summary(name, kwargs, args)
                dur_str  = f"  ({dur:.1f}s)" if dur is not None else ""
                prefix   = f"#{scan_num:<3} " if scan_num is not None else "     "
                li = QListWidgetItem(
                    f"{prefix}{icon}  {t_str}  {name}{summary}{dur_str}")
                li.setForeground(QColor(color))
                li.setData(Qt.ItemDataRole.UserRole, entry)
                self.plan_log_list.addItem(li)
        except Exception:
            pass
        # Always re-apply manually suppressed UIDs so they survive repeated reloads
        self._logged_uids |= self._suppressed_uids
        self._filter_plan_log(self._plan_log_search.text())

        if auto_select_newest and self.plan_log_list.count() > 0:
            self.plan_log_list.scrollToTop()

    # ── Public update slots ────────────────────────────────────────────────────

    def update_history(self, items: list):
        if not self._active_exp_path:
            return
        log_file = Path(self._active_exp_path) / "plans_log.jsonl"
        changed  = False

        for item in items:
            uid = item.get("item_uid", "")
            if not uid or uid in self._logged_uids:
                continue
            result      = item.get("result") or {}
            exit_status = result.get("exit_status", "")
            if not exit_status:
                continue
            t_stop   = result.get("time_stop",  0)
            t_start  = result.get("time_start", 0)
            run_uids = result.get("run_uids", [])

            if t_stop and self._exp_created_at and t_stop < self._exp_created_at:
                self._logged_uids.add(uid)
                continue
            if t_stop and self._exp_end_time and t_stop >= self._exp_end_time:
                self._logged_uids.add(uid)
                continue

            # Scans (non-empty run_uids) get the next sequential number;
            # motion-only plans (mv etc.) get None — they don't appear in
            # MongoDB browser so shouldn't consume a scan slot.
            scan_num = self._next_scan_num if run_uids else None

            timestamp = (
                datetime.fromtimestamp(t_stop).isoformat()
                if t_stop else datetime.now().isoformat()
            )
            dur = (t_stop - t_start) if (t_stop and t_start) else None
            entry = {
                "timestamp":   timestamp,
                "uid":         uid,
                "run_uids":    run_uids,
                "name":        item.get("name", ""),
                "args":        item.get("args", []) or [],
                "kwargs":      item.get("kwargs", {}) or {},
                "exit_status": exit_status,
                "duration_s":  round(dur, 2) if dur else None,
                "scan_num":    scan_num,
            }
            try:
                with open(log_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                self._logged_uids.add(uid)
                # Keep _next_scan_num ahead of the highest assigned number so
                # motion plans (no run_uids / no MongoDB) never collide.
                if run_uids:
                    self._next_scan_num += 1
                changed = True
            except Exception:
                pass

            # Show error dialog for newly failed plans
            if exit_status == "failed" and uid not in self._shown_error_uids:
                self._shown_error_uids.add(uid)
                err_msg = result.get("msg", "") or result.get("traceback", "") or "(no details)"
                QMessageBox.warning(
                    self, f"Plan Failed — {item.get('name', '?')}",
                    f"Plan  '{item.get('name', '?')}'  failed.\n\n{err_msg[:1000]}",
                )

        if changed:
            self._load_plan_log(self._active_exp_path, auto_select_newest=True)
            self.scan_completed.emit()

    def update_compact_queue(self, items: list):
        selected_uids = {
            self.queue_compact.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.queue_compact.count())
            if self.queue_compact.item(i).isSelected()
        }
        self.queue_compact.clear()
        for i, item in enumerate(items):
            name    = item.get("name", "unknown")
            args    = item.get("args", []) or []
            kwargs  = item.get("kwargs", {}) or {}
            uid     = item.get("item_uid", "")
            summary = self._plan_summary(name, kwargs, args)
            li = QListWidgetItem(f"{i + 1}.  {name}{summary}")
            li.setData(Qt.ItemDataRole.UserRole,     uid)
            li.setData(Qt.ItemDataRole.UserRole + 1, item)
            self.queue_compact.addItem(li)
            if uid and uid in selected_uids:
                li.setSelected(True)
        n = len(items)
        self.queue_count_label.setText(f"{n} item{'s' if n != 1 else ''}")

    # ── Internal slots ─────────────────────────────────────────────────────────

    def _on_plan_log_selection_changed(self):
        self._btn_requeue.setEnabled(bool(self.plan_log_list.selectedItems()))

    def _requeue_selected(self):
        """Add all selected plan log entries to the queue using their saved settings."""
        selected = self.plan_log_list.selectedItems()
        if not selected or not self.worker:
            return
        added = 0
        for li in selected:
            entry = li.data(Qt.ItemDataRole.UserRole)
            if not entry:
                continue
            item = {
                "name":      entry.get("name", ""),
                "args":      entry.get("args", []) or [],
                "kwargs":    {k: v for k, v in (entry.get("kwargs", {}) or {}).items()
                              if k != "md"},
                "item_type": "plan",
            }
            item = self._inject_metadata(item)
            ok, msg = self.worker.add_item(item)
            if ok:
                added += 1
            else:
                self._log(f"✗ Re-queue '{item['name']}': {msg}")
        if added:
            self._log(f"✓ Added {added} plan(s) to queue")

    def _on_plan_log_double_clicked(self, li: QListWidgetItem):
        """Double-click: open PlanDialog pre-populated so the user can edit & re-queue."""
        entry = li.data(Qt.ItemDataRole.UserRole)
        if not entry or not self.worker:
            return
        base = {
            "name":      entry.get("name", ""),
            "args":      entry.get("args", []) or [],
            "kwargs":    {k: v for k, v in (entry.get("kwargs", {}) or {}).items()
                         if k != "md"},
            "item_type": "plan",
        }
        dlg = PlanDialog(self._plans, self._devices, item=base, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            item = self._inject_metadata(dlg.result_item)
            ok, msg = self.worker.add_item(item)
            self._log(f"{'✓' if ok else '✗'} Re-queue '{base['name']}': {msg}")

    def _plan_log_context_menu(self, pos):
        li = self.plan_log_list.itemAt(pos)
        if not li:
            return
        entry = li.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        # If the right-clicked item is not already selected, select it alone.
        if not li.isSelected():
            self.plan_log_list.clearSelection()
            li.setSelected(True)
        selected = self.plan_log_list.selectedItems()
        menu = QMenu(self)
        menu.addAction("Edit & Re-queue", lambda: self._on_plan_log_double_clicked(li))
        menu.addAction("View Details",    lambda: self._view_plan_detail(entry))
        menu.addSeparator()
        n = len(selected)
        lbl = f"Remove {n} entr{'ies' if n != 1 else 'y'} from log"
        menu.addAction(lbl, lambda: self._remove_from_plan_log(selected))
        menu.exec(self.plan_log_list.viewport().mapToGlobal(pos))

    def _remove_from_plan_log(self, items: list):
        if not items or not self._active_exp_path:
            return
        n = len(items)
        if n > 1:
            r = QMessageBox.question(
                self, "Remove Entries",
                f"Remove {n} entries from the plan log?\n"
                "This edits plans_log.jsonl on disk and cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        # Collect UIDs to remove
        uids_to_remove = set()
        for item in items:
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry:
                uids_to_remove.add(entry.get("uid", ""))
        uids_to_remove.discard("")

        # Rewrite plans_log.jsonl keeping only entries not in the removal set
        log_file = Path(self._active_exp_path) / "plans_log.jsonl"
        try:
            lines = log_file.read_text().splitlines() if log_file.exists() else []
            kept = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("uid", "") not in uids_to_remove:
                        kept.append(line)
                except Exception:
                    kept.append(line)   # keep malformed lines intact
            log_file.write_text("\n".join(kept) + ("\n" if kept else ""))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not update plans_log.jsonl:\n{e}")
            return

        # Persist removed UIDs — survives both repeated reloads and app restarts
        self._suppressed_uids |= uids_to_remove
        self._save_suppressed_uids(self._active_exp_path)
        # Reload the plan log display (_load_plan_log re-reads suppressed_uids.json)
        self._load_plan_log(self._active_exp_path)

    def _view_plan_detail(self, entry: dict):
        ts_str = entry.get("timestamp", "")
        dur    = entry.get("duration_s")
        try:
            t_stop = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            t_stop = 0.0
        t_start = (t_stop - dur) if (t_stop and dur) else t_stop
        item = {
            "name":      entry.get("name", "?"),
            "args":      entry.get("args", []) or [],
            "kwargs":    entry.get("kwargs", {}) or {},
            "_run_file": entry.get("run_file", ""),
            "_scan_num": entry.get("scan_num"),
            "result": {
                "exit_status": entry.get("exit_status", "?"),
                "time_start":  t_start,
                "time_stop":   t_stop,
                "run_uids":    entry.get("run_uids", []),
            },
        }
        dlg = RunDetailDialog(item, worker=self.worker,
                              plans=self._plans, devices=self._devices, parent=self)
        dlg.exec()
