"""Local Data Browser — read experiment JSONL run files without MongoDB/RE Manager.

Users at home institutions can open an experiment folder (received from the beamline)
and get the same 1D plot + curve-fit experience as the MongoDB Browser.
"""

import json
from pathlib import Path

import numpy as np

try:
    import pandas as pd
    _PANDAS_OK = True
except ImportError:
    _PANDAS_OK = False

try:
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:
    _PG_OK = False

import datetime as _dt

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QSizePolicy,
    QSplitter, QStackedWidget, QTabBar, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from . import peak_fit as _pf
from .curve_fit_dialog import FitParamsDialog
from .config import PLOT_COLORS
from .plot_tools import setup_crosshair, smart_legend_position, TwoDMapWidget


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def _mongo_fetch_primary(profile: dict, uid: str) -> dict:
    """Fetch the primary event stream for one UID from MongoDB.

    Uses the same host/port/mongo_db fields as MongoDataBrowserTab.
    Returns {field: np.array} or {} if MongoDB is unavailable or has no data.
    """
    host    = profile.get("mongo_host", "localhost")
    port    = int(profile.get("mongo_port", 27017))
    db_name = profile.get("mongo_db", "")
    if not db_name or not uid:
        return {}
    try:
        import pymongo
        client = pymongo.MongoClient(host, port, serverSelectionTimeoutMS=4000)
        db     = client[db_name]
        descs  = list(db["event_descriptor"].find({"run_start": uid}))
        for desc in descs:
            if desc.get("name", "primary") != "primary":
                continue
            desc_uid  = desc["uid"]
            data_keys = desc.get("data_keys", {})
            if not data_keys:
                continue
            times      = []
            field_data = {k: [] for k in data_keys}
            pages = list(db["event_page"].find({"descriptor": desc_uid}))
            if pages:
                pages.sort(key=lambda p: (p.get("seq_num") or [0])[0])
                for page in pages:
                    times.extend(page.get("time", []))
                    pdata = page.get("data", {})
                    for field in data_keys:
                        field_data[field].extend(pdata.get(field, []))
            else:
                for ev in db["event"].find(
                        {"descriptor": desc_uid}).sort("seq_num", 1):
                    times.append(ev.get("time", 0))
                    edata = ev.get("data", {})
                    for field in data_keys:
                        field_data[field].append(edata.get(field))
            client.close()
            if not times:
                return {}
            result = {"time": np.array(times, dtype=float)}
            for field, vals in field_data.items():
                try:
                    result[field] = np.array(vals, dtype=float)
                except (TypeError, ValueError):
                    result[field] = np.array(
                        [float(v) if v is not None else float("nan") for v in vals],
                        dtype=float,
                    )
            return result
        client.close()
    except Exception:
        pass
    return {}


def _mongo_fetch_stop(profile: dict, uid: str) -> dict:
    """Fetch the run_stop document for one UID from MongoDB."""
    host    = profile.get("mongo_host", "localhost")
    port    = int(profile.get("mongo_port", 27017))
    db_name = profile.get("mongo_db", "")
    if not db_name or not uid:
        return {}
    try:
        import pymongo
        client = pymongo.MongoClient(host, port, serverSelectionTimeoutMS=4000)
        doc = client[db_name]["run_stop"].find_one(
            {"run_start": uid}, {"_id": 0}
        ) or {}
        client.close()
        return dict(doc)
    except Exception:
        return {}


def _repair_jsonl(path: str, primary_data: dict, stop_doc: dict | None = None):
    """Rewrite a JSONL file to include an event_page from MongoDB-fetched data.

    Reads the existing start/descriptor/stop docs, inserts a synthesised
    event_page immediately after the descriptor, then rewrites the file.
    After this call _parse_jsonl_run will find event data and MongoDB is no
    longer needed for this scan.

    inf/nan values are replaced with null so json.dumps never raises.
    """
    import math

    def _fix(obj):
        if isinstance(obj, float):
            return None if (math.isnan(obj) or math.isinf(obj)) else obj
        if isinstance(obj, dict):
            return {k: _fix(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_fix(v) for v in obj]
        try:
            import numpy as _np
            if isinstance(obj, _np.floating):
                v = float(obj)
                return None if (math.isnan(v) or math.isinf(v)) else v
            if isinstance(obj, _np.integer):
                return int(obj)
        except ImportError:
            pass
        return obj

    # Read all existing docs
    raw_docs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        raw_docs.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        return

    # Find descriptor uid
    desc_uid = ""
    for doc_type, doc in raw_docs:
        if doc_type == "descriptor":
            desc_uid = doc.get("uid", "")
            break

    time_arr = primary_data.get("time", np.array([]))
    n = len(time_arr)
    if n == 0:
        return

    event_page: dict = {
        "descriptor": desc_uid,
        "seq_num":    list(range(1, n + 1)),
        "time":       _fix(time_arr.tolist()),
        "data":       {},
        "timestamps": {},
    }
    for field, arr in primary_data.items():
        if field == "time":
            continue
        event_page["data"][field]       = _fix(arr.tolist())
        event_page["timestamps"][field] = _fix(time_arr.tolist())

    # Find existing stop doc in file (may be absent for truncated runs)
    existing_stop = next((doc for dt, doc in raw_docs if dt == "stop"), None)
    final_stop    = existing_stop or stop_doc  # MongoDB stop preferred as fallback

    # Rebuild: start → descriptor → event_page → stop
    new_docs = []
    for doc_type, doc in raw_docs:
        if doc_type == "stop":
            continue  # added at end after event_page
        new_docs.append((doc_type, doc))
        if doc_type == "descriptor":
            new_docs.append(("event_page", event_page))
    if final_stop:
        new_docs.append(("stop", final_stop))

    try:
        with open(path, "w", encoding="utf-8") as fh:
            for doc_type, doc in new_docs:
                fh.write(json.dumps([doc_type, doc]) + "\n")
    except Exception:
        pass


def _read_jsonl_start_stop(path) -> tuple:
    """Read start and stop documents from a JSONL run file.

    Returns (start_doc, stop_doc) — either may be {} if not found.
    Reads the whole file but stops accumulating once both are found.
    """
    start_doc: dict = {}
    stop_doc:  dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc_type, doc = json.loads(line)
                except Exception:
                    continue
                if doc_type == "start" and not start_doc:
                    start_doc = dict(doc)
                elif doc_type == "stop" and not stop_doc:
                    stop_doc = dict(doc)
                if start_doc and stop_doc:
                    break
    except Exception:
        pass
    return start_doc, stop_doc


def _parse_jsonl_run(path) -> dict:
    """Parse a bluesky suitcase JSONL run file → {field: numpy_array}.

    Each line is [doc_type, doc_body].  Collects values from 'event' and
    'event_page' documents.  Returns {} when no event data is present.
    """
    fields: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
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
        result[k] = np.array(arr)
    return result


def _poisson_sigma(y_raw, norm_raw=None):
    sigma = np.sqrt(np.abs(y_raw))
    if norm_raw is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            sigma = sigma / np.where(norm_raw != 0, norm_raw, np.nan)
    return sigma


# ── Background loader ─────────────────────────────────────────────────────────

class _RunLoader(QThread):
    """Load one or more JSONL run files in a background thread.

    Falls back to MongoDB (if mongo_profile is set) when a JSONL file has no
    event data.  Tasks are (jsonl_path, uid, label) tuples.
    """
    # (dfs, n_from_mongo, n_no_events, mongo_labels)
    done  = pyqtSignal(list, int, int, list)
    error = pyqtSignal(str)

    def __init__(self, tasks, mongo_profile=None, parent=None):
        """tasks: list of (jsonl_path, uid, label)"""
        super().__init__(parent)
        self._tasks = tasks
        self._mongo = mongo_profile  # profile dict or None

    def run(self):
        try:
            result       = []
            n_from_mongo = 0
            n_no_events  = 0
            mongo_labels = []
            for path, uid, label in self._tasks:
                data = _parse_jsonl_run(path)
                if not data and self._mongo and uid:
                    data = _mongo_fetch_primary(self._mongo, uid)
                    if data:
                        n_from_mongo += 1
                        mongo_labels.append(label)
                        stop_doc = _mongo_fetch_stop(self._mongo, uid)
                        _repair_jsonl(path, data, stop_doc)  # self-heals JSONL permanently
                if data and _PANDAS_OK:
                    df = pd.DataFrame(data)
                    if not df.empty:
                        result.append((df, label))
                        continue
                n_no_events += 1
            self.done.emit(result, n_from_mongo, n_no_events, mongo_labels)
        except Exception as exc:
            self.error.emit(str(exc))


class _MetaLoader(QThread):
    """Read start/stop docs from JSONL files in background → populate Points/Detectors."""
    row_ready = pyqtSignal(int, str, str)  # (table_row, n_points_str, detectors_str)

    def __init__(self, tasks, parent=None):
        """tasks: list of (row_index, jsonl_path)"""
        super().__init__(parent)
        self._tasks = tasks

    def run(self):
        for row_idx, path in self._tasks:
            start_doc, stop_doc = _read_jsonl_start_stop(path)

            dets = start_doc.get("detectors", [])
            dets_str = ", ".join(dets) if dets else "—"

            n_pts = stop_doc.get("num_events", None)
            if isinstance(n_pts, dict):
                total = sum(n_pts.values())
                n_pts_str = str(total) if total else "—"
            elif isinstance(n_pts, int):
                n_pts_str = str(n_pts) if n_pts else "—"
            else:
                n_pts_str = "—"

            self.row_ready.emit(row_idx, n_pts_str, dets_str)


class _LocalRunDetailDialog(QDialog):
    """Tabbed run-detail dialog matching MongoDB Browser: Metadata / Data / Start Doc (raw)."""

    def __init__(self, entry: dict, start_doc: dict, stop_doc: dict,
                 df=None, parent=None):
        super().__init__(parent)
        sn   = entry.get("scan_num", start_doc.get("scan_num", "?"))
        plan = entry.get("name") or start_doc.get("plan_name", "?")
        uid  = start_doc.get("uid") or (entry.get("run_uids") or [""])[0]
        self.setWindowTitle(
            f"Run #{sn}  —  {plan}  [{uid[:8]}…]" if uid else f"Run #{sn}  —  {plan}"
        )
        self.setMinimumSize(860, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)

        mono = QFont("Courier", 10)

        root = QVBoxLayout(self)
        root.setSpacing(6)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # ── Tab 1: Metadata ───────────────────────────────────────────────────
        meta_w = QWidget()
        QVBoxLayout(meta_w)
        self._meta_txt = QTextEdit()
        self._meta_txt.setReadOnly(True)
        self._meta_txt.setFont(mono)
        self._meta_txt.setPlainText(self._format_metadata(start_doc, stop_doc, sn))
        meta_w.layout().addWidget(self._meta_txt)
        tabs.addTab(meta_w, "Metadata")

        # ── Tab 2: Data table ─────────────────────────────────────────────────
        data_w = QWidget()
        data_l = QVBoxLayout(data_w)
        if df is not None and not df.empty:
            tbl = QTableWidget()
            tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            tbl.setAlternatingRowColors(True)
            tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            tbl.verticalHeader().setVisible(False)
            cols = list(df.columns)
            tbl.setColumnCount(len(cols))
            tbl.setHorizontalHeaderLabels(cols)
            tbl.setRowCount(len(df))
            hh = tbl.horizontalHeader()
            for i in range(len(cols)):
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
            time_col = "time" in df.columns
            for i, (_, row_ser) in enumerate(df.iterrows()):
                for j, col in enumerate(cols):
                    v = row_ser[col]
                    if col == "time":
                        try:
                            txt = _dt.datetime.fromtimestamp(float(v)).strftime("%H:%M:%S.%f")[:-3]
                        except Exception:
                            txt = str(v)
                    else:
                        try:
                            txt = f"{float(v):.6g}"
                        except (TypeError, ValueError):
                            txt = str(v)
                    item = QTableWidgetItem(txt)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    tbl.setItem(i, j, item)
            data_l.addWidget(tbl, 1)
        elif df is not None:
            data_l.addWidget(QLabel(
                "No event data in this JSONL file.\n\n"
                "This scan was recorded before the JSONL event writer was fully active.\n"
                "Use MongoDB Browser to view its data."
            ))
        else:
            data_l.addWidget(QLabel(
                "Data not loaded — select the scan in the table first, then double-click."
            ))
        tabs.addTab(data_w, "Data")

        # ── Tab 3: Start Doc (raw) ────────────────────────────────────────────
        raw_w = QWidget()
        QVBoxLayout(raw_w)
        raw_txt = QTextEdit()
        raw_txt.setReadOnly(True)
        raw_txt.setFont(QFont("Courier", 9))
        raw_txt.setPlainText(json.dumps(start_doc, indent=2, default=str))
        raw_w.layout().addWidget(raw_txt)
        tabs.addTab(raw_w, "Start Doc (raw)")

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row  = QHBoxLayout()
        btn_copy = QPushButton("Copy metadata")
        btn_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self._meta_txt.toPlainText()))
        btn_row.addWidget(btn_copy)
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    @staticmethod
    def _format_metadata(start: dict, stop: dict, seq_num) -> str:
        ts_start = start.get("time", 0)
        ts_stop  = stop.get("time",  0) if stop else 0
        dur = f"{ts_stop - ts_start:.2f} s" if ts_start and ts_stop else "—"
        try:
            s_str = _dt.datetime.fromtimestamp(ts_start).strftime("%Y-%m-%d %H:%M:%S") if ts_start else "—"
        except Exception:
            s_str = str(ts_start)
        try:
            e_str = _dt.datetime.fromtimestamp(ts_stop).strftime("%Y-%m-%d %H:%M:%S") if ts_stop else "—"
        except Exception:
            e_str = str(ts_stop)
        n_ev = stop.get("num_events", {}) if stop else {}
        if isinstance(n_ev, dict):
            n_pts = sum(n_ev.values()) or "—"
        elif isinstance(n_ev, int):
            n_pts = n_ev or "—"
        else:
            n_pts = "—"
        lines = [
            f"Scan #        : {seq_num}",
            f"Plan          : {start.get('plan_name', '—')}",
            f"UID           : {start.get('uid', '—')}",
            f"Scan ID       : {start.get('scan_id', '—')}",
            f"Status        : {(stop or {}).get('exit_status', 'running')}",
            f"Start         : {s_str}",
            f"Stop          : {e_str}",
            f"Duration      : {dur}",
            f"Num events    : {n_pts}",
            f"Motors        : {', '.join(start.get('motors', [])) or '—'}",
            f"Detectors     : {', '.join(start.get('detectors', [])) or '—'}",
        ]
        for key in ("sample_name", "exp_dir"):
            val = start.get(key, "")
            if val:
                label = "Sample" if key == "sample_name" else "Exp dir"
                lines.append(f"{label:<14}: {val}")
        skip = {"uid", "time", "plan_name", "scan_id", "motors", "detectors",
                "sample_name", "exp_dir", "hints", "plan_args", "plan_pattern",
                "plan_type", "peak_stats", "scan_num"}
        extras = {k: v for k, v in start.items() if k not in skip}
        if extras:
            lines += ["", "─── Extra metadata ───"]
            for k, v in extras.items():
                lines.append(f"{k:<14}: {v}")
        ps = start.get("peak_stats")
        if ps:
            def _fmt(v):
                try:
                    return f"{float(v):.4g}"
                except (TypeError, ValueError):
                    return "—"
            lines += ["", "─── Peak stats ───"]
            for sig, st in ps.items():
                lines.append(
                    f"{sig:<14}: cen={_fmt(st.get('cen'))}  FWHM={_fmt(st.get('fwhm'))}"
                    f"  COM={_fmt(st.get('com'))}  max={_fmt(st.get('max_val'))}"
                    f"@{_fmt(st.get('max_pos'))}"
                )
        plan_args = start.get("plan_args", {})
        if plan_args:
            lines += ["", "─── Plan arguments ───"]
            for k, v in plan_args.items():
                lines.append(f"{k:<14}: {v}")
        return "\n".join(lines)


# ── Main widget ───────────────────────────────────────────────────────────────

class LocalDataBrowserTab(QWidget):
    """Offline experiment data browser — reads plans_log.jsonl + runs/*.jsonl."""

    COLORS = PLOT_COLORS
    sync_requested = pyqtSignal(list, str)   # (uid_list, runs_dir_path)

    def set_sync_message(self, msg: str):
        """Called by main.py to show SSH progress in this tab's status bar."""
        self._status_label.setText(msg)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exp_path   = ""
        self._entries    = []   # plans_log.jsonl rows (newest-first in table)
        self._dfs        = []   # [(pd.DataFrame, label), ...] currently shown
        self._curves:     dict = {}
        self._error_items:dict = {}
        self._fit_curves: dict = {}
        self._fit_texts:  list = []
        self._fit_preview_curve = None
        self._fit_dlg           = None
        self._saved_fit_state   = None
        self._loader            = None
        self._meta_loader       = None
        self._crosshair_cleanup = None
        self._map_mode          = False
        self._2d_map_data       = None
        self._mongo_profile: dict | None = None  # active profile for MongoDB fallback
        self._build()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setSizes([320, 1080])
        lay.addWidget(splitter)

    def _build_left(self) -> QWidget:
        w    = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 4, 8)
        vlay.setSpacing(6)

        lbl = QLabel("LOCAL DATA BROWSER")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

        self._btn_open = QPushButton("Open Experiment Folder…")
        self._btn_open.setObjectName("btn_primary")
        self._btn_open.clicked.connect(self._open_folder)
        vlay.addWidget(self._btn_open)

        self._btn_sync = QPushButton("⟳ Fetch JSONL from Beamline")
        self._btn_sync.setToolTip(
            "Copy missing run JSONL files from the beamline computer\n"
            "via SSH — requires active connection to RE Manager"
        )
        self._btn_sync.clicked.connect(self._on_sync_clicked)
        vlay.addWidget(self._btn_sync)

        self._exp_label = QLabel("No folder open")
        self._exp_label.setObjectName("dim_text")
        self._exp_label.setStyleSheet("font-size: 11px; font-weight: bold;")
        self._exp_label.setWordWrap(True)
        vlay.addWidget(self._exp_label)

        hint = QLabel("SCANS  (click to plot · Shift/Ctrl to overlay)")
        hint.setObjectName("section_title")
        vlay.addWidget(hint)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search scans…")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._filter_table)
        vlay.addWidget(self._search)

        self._scan_table = QTableWidget()
        self._scan_table.setColumnCount(6)
        self._scan_table.setHorizontalHeaderLabels(
            ["#", "Plan", "Date / Time", "Status", "Points", "Detectors"])
        hh = self._scan_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._scan_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._scan_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._scan_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._scan_table.verticalHeader().setVisible(False)
        self._scan_table.setSortingEnabled(False)
        self._scan_table.setAlternatingRowColors(True)
        self._scan_table.itemSelectionChanged.connect(
            self._on_scan_selection_changed)
        self._scan_table.cellDoubleClicked.connect(self._on_run_double_clicked)
        vlay.addWidget(self._scan_table, 1)

        self._status_label = QLabel("Open an experiment folder to begin")
        self._status_label.setObjectName("dim_text")
        self._status_label.setStyleSheet("font-size: 10px;")
        vlay.addWidget(self._status_label)

        return w

    def _build_right(self) -> QWidget:
        w    = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 8, 8, 8)
        vlay.setSpacing(6)

        # ── Top bar: X combo + run label + screenshot ─────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(2, 2, 2, 2)
        top_bar.setSpacing(4)
        top_bar.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(120)
        self.x_combo.setMaximumWidth(240)
        self.x_combo.setFixedHeight(26)
        self.x_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.x_combo.currentTextChanged.connect(self._replot)
        top_bar.addWidget(self.x_combo)
        top_bar.addSpacing(6)
        self.run_label = QLabel("")
        self.run_label.setObjectName("dim_text")
        self.run_label.setStyleSheet("font-size: 12px; padding: 0 4px;")
        top_bar.addWidget(self.run_label)
        top_bar.addStretch()
        btn_ss = QPushButton("📷")
        btn_ss.setFixedSize(28, 26)
        btn_ss.setToolTip("Copy plot to clipboard")
        btn_ss.clicked.connect(self._copy_screenshot)
        top_bar.addWidget(btn_ss)
        vlay.addLayout(top_bar)

        # ── Mode tabs: 1D Plot / 2D Map ────────────────────────────────────────
        self._mode_tabs = QTabBar()
        self._mode_tabs.setDocumentMode(True)

        # 1D controls bar (Norm, ±Errors, Fit)
        _tab_1d = QWidget()
        _1d_bar = QHBoxLayout(_tab_1d)
        _1d_bar.setContentsMargins(4, 2, 4, 2)
        _1d_bar.setSpacing(4)

        _1d_bar.addWidget(QLabel("Norm:"))
        self.norm_combo = QComboBox()
        self.norm_combo.setMinimumWidth(100)
        self.norm_combo.setMaximumWidth(220)
        self.norm_combo.setFixedHeight(26)
        self.norm_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.norm_combo.addItem("None", userData=None)
        self.norm_combo.currentIndexChanged.connect(self._replot)
        _1d_bar.addWidget(self.norm_combo)

        _1d_bar.addSpacing(6)
        self._err_cb = QCheckBox("± Errors")
        self._err_cb.setToolTip("Overlay Poisson √N error bars")
        self._err_cb.stateChanged.connect(self._replot)
        _1d_bar.addWidget(self._err_cb)

        _1d_bar.addSpacing(10)
        _1d_bar.addWidget(QLabel("Fit:"))

        self._fit_model_combo = QComboBox()
        self._fit_model_combo.setFixedHeight(26)
        self._fit_model_combo.setMinimumWidth(110)
        self._fit_model_combo.addItem("None")
        self._fit_model_combo.insertSeparator(self._fit_model_combo.count())
        for m in _pf.PEAK_MODELS:
            self._fit_model_combo.addItem(m)
        self._fit_model_combo.insertSeparator(self._fit_model_combo.count())
        for m in _pf.STEP_MODELS:
            self._fit_model_combo.addItem(m)
        self._fit_model_combo.setCurrentText(_pf.PEAK_MODELS[0])
        _1d_bar.addWidget(self._fit_model_combo)

        bg_lbl = QLabel("+ BG:")
        bg_lbl.setStyleSheet("font-size: 11px;")
        _1d_bar.addWidget(bg_lbl)
        self._fit_bg_combo = QComboBox()
        self._fit_bg_combo.setFixedHeight(26)
        self._fit_bg_combo.setMinimumWidth(80)
        self._fit_bg_combo.setMaximumWidth(110)
        self._fit_bg_combo.addItems(_pf.BACKGROUND_MODELS)
        _1d_bar.addWidget(self._fit_bg_combo)

        btn_fit = QPushButton("Fit…")
        btn_fit.setObjectName("btn_primary")
        btn_fit.setFixedHeight(26)
        btn_fit.clicked.connect(self._open_fit_dialog)
        _1d_bar.addWidget(btn_fit)

        btn_clear = QPushButton("✕")
        btn_clear.setFixedSize(28, 26)
        btn_clear.setToolTip("Clear fit overlays")
        btn_clear.clicked.connect(self._clear_fit_overlays)
        _1d_bar.addWidget(btn_clear)
        _1d_bar.addStretch()

        self._mode_tabs.addTab("1D Plot")
        self._mode_tabs.addTab("2D Map")
        self._mode_tabs.currentChanged.connect(self._on_mode_tab_changed)
        vlay.addWidget(self._mode_tabs)
        self._1d_controls = _tab_1d
        vlay.addWidget(self._1d_controls)

        # ── Y signal list on right side of plot ───────────────────────────────
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.y_list.setMinimumWidth(100)
        self.y_list.itemSelectionChanged.connect(self._replot)

        y_lbl = QLabel("Y signals")
        y_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        y_lbl.setObjectName("dim_text")
        y_container = QWidget()
        y_layout = QVBoxLayout(y_container)
        y_layout.setSpacing(2)
        y_layout.setContentsMargins(4, 0, 0, 0)
        y_layout.addWidget(y_lbl)
        y_layout.addWidget(self.y_list, 1)

        # coord label created before crosshair setup
        self._coord_label = QLabel("")
        self._coord_label.setObjectName("dim_text")
        self._coord_label.setStyleSheet(
            "font-size: 11px; padding: 4px; font-family: Menlo, Consolas, Monaco, 'Courier New';")

        if _PG_OK:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            plot_area = self.plot_widget
        else:
            self.plot_widget = QLabel(
                "pyqtgraph not installed — pip install pyqtgraph")
            self.plot_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            plot_area = self.plot_widget

        plot_splitter = QSplitter(Qt.Orientation.Horizontal)
        plot_splitter.addWidget(plot_area)
        plot_splitter.addWidget(y_container)
        plot_splitter.setSizes([880, 180])
        plot_splitter.setStretchFactor(0, 1)
        plot_splitter.setStretchFactor(1, 0)

        self._2d_widget = TwoDMapWidget(parent=w)
        self._2d_widget.selection_changed.connect(self._update_2d_plot)

        self._btn_save_2d = QPushButton("Save 2D…")
        self._btn_save_2d.setVisible(False)
        self._btn_save_2d.setToolTip("Save multi-scan 2D map as CSV")
        self._btn_save_2d.clicked.connect(self._save_2d_map)
        self._2d_widget.add_to_ctrl_row(self._btn_save_2d)

        self._plot_stack = QStackedWidget()
        self._plot_stack.addWidget(plot_splitter)
        self._plot_stack.addWidget(self._2d_widget)
        vlay.addWidget(self._plot_stack, 1)

        # ── Bottom bar: coord + stats + Log Y + Deriv ─────────────────────────
        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(6)
        bot.addWidget(self._coord_label, 1)

        self._log_y_cb = QCheckBox("Log Y")
        self._log_y_cb.stateChanged.connect(self._replot)
        bot.addWidget(self._log_y_cb)

        bot.addWidget(QLabel("Deriv:"))
        self._deriv_combo = QComboBox()
        self._deriv_combo.addItems(["—", "dy/dx", "d²y/dx²"])
        self._deriv_combo.setFixedHeight(22)
        self._deriv_combo.setFixedWidth(90)
        self._deriv_combo.currentIndexChanged.connect(self._replot)
        bot.addWidget(self._deriv_combo)

        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim_text")
        self.stats_label.setStyleSheet("font-size: 10px;")
        bot.addWidget(self.stats_label)
        vlay.addLayout(bot)

        if _PG_OK:
            self._crosshair_cleanup = setup_crosshair(
                self.plot_widget, self._coord_label,
                get_curves_fn=lambda: self._curves,
            )

        return w

    # ── Folder opening ────────────────────────────────────────────────────────

    def _open_folder(self):
        start = self._exp_path or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Open Experiment Folder", start)
        if folder:
            self._load_folder(folder)

    def update_settings(self, settings: dict):
        """Called by main.py when connection settings change.

        Extracts the active profile's MongoDB credentials so the browser can
        fall back to MongoDB when a JSONL file has no event data.
        """
        from .connection_settings import get_active_profile
        profile = get_active_profile(settings) or {}
        if profile.get("mongo_db", "").strip():
            self._mongo_profile = profile
        else:
            self._mongo_profile = None

    def open_folder(self, folder: str):
        """Public entry point — called from main.py when experiment changes."""
        if folder and Path(folder).is_dir():
            self._load_folder(folder)

    def _missing_uids(self) -> list:
        """Return UIDs from plans_log that have no local JSONL file."""
        missing = []
        for entry in self._entries:
            uids = entry.get("run_uids", [])
            uid  = uids[0] if uids else ""
            if uid:
                p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                if not p.exists():
                    missing.append(uid)
        return missing

    def _auto_sync(self):
        """Emit sync_requested for any UIDs with no local JSONL file."""
        if not self._exp_path:
            return
        missing = self._missing_uids()
        n_all   = sum(1 for e in self._entries if (e.get("run_uids") or [""])[0])
        n_local = n_all - len(missing)
        self._status_label.setText(
            f"{len(self._entries)} scans  ·  "
            f"{n_local}/{n_all} run files local"
            + (f"  ·  fetching {len(missing)} from beamline…" if missing else "")
        )
        if missing:
            runs_dir = str(Path(self._exp_path) / "runs")
            self.sync_requested.emit(missing, runs_dir)

    def _on_sync_clicked(self):
        """Manual re-fetch button — re-checks for missing files and emits sync_requested."""
        if not self._exp_path:
            self._status_label.setText("Open an experiment folder first")
            return
        missing = self._missing_uids()
        if not missing:
            self._status_label.setText("All run files already present locally — nothing to fetch")
            return
        runs_dir = str(Path(self._exp_path) / "runs")
        self._status_label.setText(f"Fetching {len(missing)} JSONL file(s) from beamline…")
        self.sync_requested.emit(missing, runs_dir)

    def on_sync_done(self, n_copied: int, n_total: int):
        """Called by main.py after SSH fetch completes."""
        if n_copied > 0:
            self._status_label.setText(
                f"Fetched {n_copied}/{n_total} JSONL file(s) from beamline"
            )
            # Reload table entries (plans_log unchanged, but files now exist locally)
            if self._exp_path:
                self._reload_after_sync()
        else:
            missing_still = self._missing_uids()
            if missing_still:
                self._status_label.setText(
                    f"Could not fetch JSONL files — "
                    f"check RE Console for details  "
                    f"({n_total} file(s) not found in beamline fallback directory)"
                )
            else:
                self._status_label.setText(
                    f"{len(self._entries)} scans loaded  ·  all run files present locally"
                )

    def _reload_after_sync(self):
        """Reload plan table and re-trigger any current scan selection."""
        # Remember which scan rows are currently selected
        selected_rows = sorted({idx.row() for idx in self._scan_table.selectedIndexes()})
        # Reload so file-existence checks reflect newly copied files
        log_file = Path(self._exp_path) / "plans_log.jsonl"
        if not log_file.exists():
            return
        entries = []
        try:
            with open(log_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            return
        self._entries = entries
        self._populate_table(entries)
        # Restore selection
        if selected_rows:
            self._scan_table.blockSignals(True)
            for row in selected_rows:
                if row < self._scan_table.rowCount():
                    self._scan_table.selectRow(row)
            self._scan_table.blockSignals(False)
            self._on_scan_selection_changed()

    def _load_folder(self, folder: str):
        self._exp_path = folder
        self._exp_label.setText(Path(folder).name)

        log_file = Path(folder) / "plans_log.jsonl"
        if not log_file.exists():
            QMessageBox.warning(
                self, "No Plan Log",
                f"plans_log.jsonl not found in:\n{folder}\n\n"
                "Make sure this is a valid EasyBluesky experiment folder.",
            )
            self._entries = []
            self._scan_table.setRowCount(0)
            self._status_label.setText("No plan log found")
            return

        entries = []
        try:
            with open(log_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as exc:
            QMessageBox.critical(self, "Read Error", str(exc))
            return

        self._entries = entries
        self._populate_table(entries)
        self._dfs = []
        self._clear_plot()
        self._auto_sync()

    # ── Scan table ────────────────────────────────────────────────────────────

    def _populate_table(self, entries):
        self._scan_table.setRowCount(0)

        def _ro(text, align=Qt.AlignmentFlag.AlignLeft):
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            it.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            return it

        STATUS_COLOR = {
            "success":  "#2ca02c",
            "fail":     "#d62728",
            "abort":    "#e88a00",
            "running":  "#1f77b4",
        }

        meta_tasks = []   # (row_index, jsonl_path) for background meta loading

        for entry in reversed(entries):   # newest first
            sn     = entry.get("scan_num", "")
            name   = entry.get("name", "?")
            ts     = entry.get("timestamp", "")
            if ts and "T" in ts:
                ts = ts[:16].replace("T", "  ")
            status = entry.get("exit_status", "")

            row = self._scan_table.rowCount()
            self._scan_table.insertRow(row)

            sn_it = _ro(str(sn) if sn != "" else "?",
                        Qt.AlignmentFlag.AlignRight)
            sn_it.setData(Qt.ItemDataRole.UserRole, entry)
            self._scan_table.setItem(row, 0, sn_it)
            self._scan_table.setItem(row, 1, _ro(name))
            self._scan_table.setItem(row, 2, _ro(ts))

            st_it = _ro(status)
            if status in STATUS_COLOR:
                st_it.setForeground(QColor(STATUS_COLOR[status]))
            self._scan_table.setItem(row, 3, st_it)

            # Points and Detectors — placeholder until _MetaLoader fills them
            self._scan_table.setItem(row, 4, _ro("—", Qt.AlignmentFlag.AlignRight))
            self._scan_table.setItem(row, 5, _ro("—"))

            uids = entry.get("run_uids", [])
            uid  = uids[0] if uids else ""
            if uid and self._exp_path:
                p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                if p.exists():
                    meta_tasks.append((row, str(p)))

        self._scan_table.resizeColumnsToContents()

        # Launch background thread to fill Points / Detectors columns
        if meta_tasks:
            if self._meta_loader and self._meta_loader.isRunning():
                self._meta_loader.terminate()
                self._meta_loader.wait(200)
            self._meta_loader = _MetaLoader(meta_tasks, parent=self)
            self._meta_loader.row_ready.connect(self._update_meta_row)
            self._meta_loader.start()

    def _update_meta_row(self, row: int, n_pts_str: str, dets_str: str):
        """Slot called by _MetaLoader — fill Points and Detectors cells."""
        if row >= self._scan_table.rowCount():
            return
        def _ro_r(text, align=Qt.AlignmentFlag.AlignLeft):
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            it.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            return it
        self._scan_table.setItem(row, 4, _ro_r(n_pts_str, Qt.AlignmentFlag.AlignRight))
        self._scan_table.setItem(row, 5, _ro_r(dets_str))

    def _on_run_double_clicked(self, row: int, _col: int):
        """Double-click a scan row → show tabbed run detail dialog."""
        if row >= self._scan_table.rowCount():
            return
        entry = self._scan_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        sn   = entry.get("scan_num", "?")
        uids = entry.get("run_uids", [])
        uid  = uids[0] if uids else ""
        start_doc: dict = {}
        stop_doc:  dict = {}
        df = None
        if uid and self._exp_path:
            p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
            if p.exists():
                start_doc, stop_doc = _read_jsonl_start_stop(str(p))
                # Re-use already-loaded DataFrame if this scan is currently selected
                label = f"#{sn}"
                for loaded_df, loaded_lbl in self._dfs:
                    if loaded_lbl == label:
                        df = loaded_df
                        break
                # Otherwise parse fresh (event data only, no blocking issue — dialog is modal-less)
                if df is None and _PANDAS_OK:
                    raw = _parse_jsonl_run(str(p))
                    if not raw and self._mongo_profile and uid:
                        raw = _mongo_fetch_primary(self._mongo_profile, uid)
                        if raw:
                            stop_from_mongo = _mongo_fetch_stop(self._mongo_profile, uid)
                            _repair_jsonl(str(p), raw, stop_from_mongo)
                            if not stop_doc and stop_from_mongo:
                                stop_doc = stop_from_mongo  # use in this dialog immediately
                    df = pd.DataFrame(raw) if raw else pd.DataFrame()
        dlg = _LocalRunDetailDialog(entry, start_doc, stop_doc, df=df, parent=self)
        dlg.show()

    def _filter_table(self, text: str):
        text = text.lower()
        for row in range(self._scan_table.rowCount()):
            visible = (
                not text
                or any(
                    text in (self._scan_table.item(row, c).text() or "").lower()
                    for c in range(6)
                )
            )
            self._scan_table.setRowHidden(row, not visible)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _on_scan_selection_changed(self):
        rows = sorted({idx.row() for idx in self._scan_table.selectedIndexes()})
        if not rows:
            return

        tasks        = []
        labels       = []
        missing_files = []
        no_uid_count  = 0
        for row in rows:
            if self._scan_table.isRowHidden(row):
                continue
            entry = self._scan_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if entry is None:
                continue
            uids = entry.get("run_uids", [])
            uid  = uids[0] if uids else ""
            sn   = entry.get("scan_num", "?")
            label = f"#{sn}"
            if not uid:
                no_uid_count += 1
                continue
            if self._exp_path:
                p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                if p.exists():
                    tasks.append((str(p), uid, label))
                    labels.append(label)
                else:
                    missing_files.append(p.name)

        if not tasks:
            if missing_files:
                self._status_label.setText(
                    f"Run file(s) not found in {Path(self._exp_path).name}/runs/  "
                    f"— data may be on the beamline computer"
                )
            elif no_uid_count:
                self._status_label.setText("Selected scan(s) have no bluesky run data (motion-only plans)")
            else:
                self._status_label.setText("No run data found for selected scan(s)")
            return

        # Cancel any running loader
        if self._loader and self._loader.isRunning():
            self._loader.done.disconnect()
            self._loader.error.disconnect()

        loading_msg = f"Loading {len(tasks)} scan(s)…"
        if self._mongo_profile:
            loading_msg += "  (will fall back to MongoDB if JSONL has no events)"
        self._status_label.setText(loading_msg)
        self._loader = _RunLoader(tasks, mongo_profile=self._mongo_profile, parent=self)
        self._loader.done.connect(self._on_load_done)
        self._loader.error.connect(self._on_load_error)
        self._loader.start()

    def _refresh_meta_for_labels(self, labels: set):
        """Re-run _MetaLoader for table rows whose label is in `labels`.

        Called after MongoDB repair so Points/Status cells update without
        requiring a full folder reload.
        """
        if not labels or not self._exp_path:
            return
        tasks = []
        for row in range(self._scan_table.rowCount()):
            item = self._scan_table.item(row, 0)
            if item is None:
                continue
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry is None:
                continue
            sn    = entry.get("scan_num", "?")
            label = f"#{sn}"
            if label not in labels:
                continue
            uids = entry.get("run_uids", [])
            uid  = uids[0] if uids else ""
            if uid:
                p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                if p.exists():
                    tasks.append((row, str(p)))
        if tasks:
            loader = _MetaLoader(tasks, parent=self)
            loader.row_ready.connect(self._update_meta_row)
            loader.start()

    def _on_load_done(self, dfs, n_from_mongo: int, n_no_events: int, mongo_labels: list):
        n = len(dfs)
        self._dfs = dfs
        if mongo_labels:
            self._refresh_meta_for_labels(set(mongo_labels))
        if n == 0:
            if n_no_events and not self._mongo_profile:
                self._status_label.setText(
                    "JSONL file(s) have no event data — "
                    "configure MongoDB in Settings to auto-load, "
                    "or view in MongoDB Browser"
                )
            elif n_no_events:
                self._status_label.setText(
                    "No event data found in JSONL or MongoDB for selected scan(s)"
                )
            else:
                self._status_label.setText("No plottable data in selected scan(s)")
            self._clear_plot()
            return
        mongo_note = f"  [{n_from_mongo} via MongoDB]" if n_from_mongo else ""
        self._status_label.setText(
            f"{n} scan(s) loaded{mongo_note} — "
            f"{', '.join(lbl for _, lbl in dfs)}"
        )
        if dfs:
            self._update_field_combos(dfs[0][0])
        if self._map_mode:
            self._update_2d_plot()
        else:
            self._replot()

    def _on_load_error(self, msg: str):
        self._status_label.setText(f"Load error: {msg}")

    # ── Mode tab / 2D map ─────────────────────────────────────────────────────

    def _on_mode_tab_changed(self, idx: int):
        self._1d_controls.setVisible(idx == 0)
        self._toggle_map_mode(idx == 1)

    def _toggle_map_mode(self, checked: bool):
        self._map_mode = checked
        self._plot_stack.setCurrentIndex(1 if checked else 0)
        if checked:
            self._update_2d_plot()
        else:
            self._btn_save_2d.setVisible(False)
            self._2d_map_data = None
            self._replot()

    def _update_2d_plot(self):
        """Build 2D map from all selected scans — 1 scan or N scans, same path."""
        if not self._dfs:
            return
        x_col = self.x_combo.currentText()
        y_col = self._2d_widget.get_y_signal()
        z_col = self._2d_widget.get_z_signal()
        if not x_col or not z_col:
            return

        xs_list, ys_list, zs_list = [], [], []
        for i, (df, _lbl) in enumerate(self._dfs):
            if x_col not in df.columns or z_col not in df.columns:
                continue
            x_arr = df[x_col].values.astype(float)
            z_arr = df[z_col].values.astype(float)
            y_arr = (df[y_col].values.astype(float)
                     if y_col and y_col in df.columns
                     else np.full(len(x_arr), float(i)))
            n = min(len(x_arr), len(y_arr), len(z_arr))
            xs_list.append(x_arr[:n])
            ys_list.append(y_arr[:n])
            zs_list.append(z_arr[:n])

        if not xs_list:
            self._status_label.setText(
                f"No scans contain both '{x_col}' and '{z_col}'"
            )
            return

        xs = np.concatenate(xs_list)
        ys = np.concatenate(ys_list)
        zs = np.concatenate(zs_list)

        y_label = y_col or "scan index"
        self._2d_map_data = (xs, ys, zs, x_col, y_label, z_col,
                             [lbl for _, lbl in self._dfs])
        self._2d_widget.replot(xs, ys, zs,
                               x_label=x_col, y_label=y_label, z_label=z_col)
        self._btn_save_2d.setVisible(True)

    def _save_2d_map(self):
        if not self._2d_map_data:
            return
        xs, ys, zs, x_col, y_col, z_col, _labels = self._2d_map_data
        path, _ = QFileDialog.getSaveFileName(
            self, "Save 2D Map", "", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w") as fh:
                fh.write(f"{x_col},{y_col},{z_col}\n")
                for x, y, z in zip(xs, ys, zs):
                    fh.write(f"{x},{y},{z}\n")
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", str(exc))

    # ── Field combos ─────────────────────────────────────────────────────────

    def _update_field_combos(self, df):
        cols = sorted(df.columns.tolist())

        prev_x    = self.x_combo.currentText()
        prev_ys   = {self.y_list.item(i).text()
                     for i in range(self.y_list.count())
                     if self.y_list.item(i).isSelected()}
        prev_norm = self.norm_combo.currentData()

        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.addItems(cols)
        if prev_x in cols:
            self.x_combo.setCurrentText(prev_x)
        else:
            for pref in ("time", "motor", "energy", "x"):
                match = next((c for c in cols if pref in c.lower()), None)
                if match:
                    self.x_combo.setCurrentText(match)
                    break
        self.x_combo.blockSignals(False)

        self.y_list.blockSignals(True)
        self.y_list.clear()
        for c in cols:
            self.y_list.addItem(c)
        for i in range(self.y_list.count()):
            if self.y_list.item(i).text() in prev_ys:
                self.y_list.item(i).setSelected(True)
        if not any(self.y_list.item(i).isSelected()
                   for i in range(self.y_list.count())):
            x_name = self.x_combo.currentText()
            for i in range(self.y_list.count()):
                name = self.y_list.item(i).text()
                if name != x_name and not name.endswith(("_setpoint", "_user_setpoint")):
                    self.y_list.item(i).setSelected(True)
                    break
        self.y_list.blockSignals(False)

        self.norm_combo.blockSignals(True)
        self.norm_combo.clear()
        self.norm_combo.addItem("None", userData=None)
        for c in cols:
            self.norm_combo.addItem(c, userData=c)
        if prev_norm and prev_norm in cols:
            self.norm_combo.setCurrentText(prev_norm)
        self.norm_combo.blockSignals(False)

        x_col = self.x_combo.currentText()
        self._2d_widget.set_columns(cols, x_col, [], cols)

    # ── Plotting ──────────────────────────────────────────────────────────────

    def _clear_plot(self):
        if not _PG_OK:
            return
        self._clear_fit_overlays()
        self._clear_fit_preview()
        for item in list(self._error_items.values()) + list(self._curves.values()):
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        pi = self.plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves      = {}
        self._error_items = {}
        self.stats_label.setText("")
        self.run_label.setText("")

    def _replot(self):
        if not _PG_OK or not self._dfs:
            return

        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return

        self._clear_plot()

        norm_col  = self.norm_combo.currentData()
        show_err  = self._err_cb.isChecked()
        color_idx = 0
        stats     = []

        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x        = df[xc].values.astype(float)
            norm_raw = (df[norm_col].values.astype(float)
                        if norm_col and norm_col in df.columns else None)

            for yc in ycs:
                if yc not in df.columns:
                    continue
                y_raw = df[yc].values.astype(float)

                y = y_raw.copy()
                if norm_raw is not None:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(norm_raw != 0, y / norm_raw, np.nan)

                sigma = _poisson_sigma(y_raw, norm_raw)

                deriv_mode = self._deriv_combo.currentIndex()
                if deriv_mode > 0:
                    _, y, sigma = self._apply_deriv(x, y, sigma, order=deriv_mode)
                if self._log_y_cb.isChecked():
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.log10(np.where(y > 0, y, np.nan))

                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_, s_ = x[mask], y[mask], sigma[mask]
                if not len(x_):
                    continue

                color      = self.COLORS[color_idx % len(self.COLORS)]
                pen        = pg.mkPen(color=color, width=2)
                base_name  = yc if not norm_col else f"{yc}/{norm_col}"
                curve_name = (base_name if len(self._dfs) == 1
                              else f"{base_name}  [{df_label}]")
                curve = self.plot_widget.plot(
                    x_, y_, pen=pen, name=curve_name,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
                self._curves[curve_name] = curve

                if show_err and np.any(np.isfinite(s_)):
                    err_item = pg.ErrorBarItem(
                        x=x_, y=y_, height=2 * s_,
                        beam=0.0, pen=pg.mkPen(color=color, width=1),
                    )
                    self.plot_widget.addItem(err_item)
                    self._error_items[curve_name] = err_item

                color_idx += 1
                stats.append(
                    f"{curve_name}: min={y_.min():.4g}  max={y_.max():.4g}")

        self.plot_widget.setLabel("bottom", xc)
        y_label = ", ".join(ycs)
        if norm_col:
            y_label += f"  /  {norm_col}"
        deriv_mode = self._deriv_combo.currentIndex()
        if deriv_mode == 1:
            y_label += "  [dy/dx]"
        elif deriv_mode == 2:
            y_label += "  [d²y/dx²]"
        if self._log_y_cb.isChecked():
            y_label = f"log₁₀({y_label})"
        self.plot_widget.setLabel("left", y_label)
        self.stats_label.setText("   ".join(stats))
        if self._dfs:
            self.run_label.setText(", ".join(lbl for _, lbl in self._dfs))
        smart_legend_position(self.plot_widget)

    @staticmethod
    def _apply_deriv(x, y, sigma=None, order=1):
        """Central-difference derivative, same implementation as HDF5Viewer."""
        finite = np.isfinite(x) & np.isfinite(y)
        if finite.sum() < 3:
            return x, y, (sigma if sigma is not None else np.zeros_like(y))
        for _ in range(order):
            dy = np.gradient(y, x)
            if sigma is not None:
                n = len(x)
                s = np.zeros(n)
                for i in range(n):
                    il = max(0, i - 1)
                    ir = min(n - 1, i + 1)
                    dx = x[ir] - x[il]
                    if dx != 0:
                        s[i] = np.sqrt(sigma[il]**2 + sigma[ir]**2) / abs(dx)
                sigma = s
            y = dy
        return x, y, (sigma if sigma is not None else np.zeros_like(y))

    # ── Fit overlays ──────────────────────────────────────────────────────────

    def _clear_fit_overlays(self):
        if not _PG_OK:
            return
        for item in self._fit_texts:
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        for curve in self._fit_curves.values():
            try:
                self.plot_widget.removeItem(curve)
            except Exception:
                pass
        self._fit_texts  = []
        self._fit_curves = {}

    def _clear_fit_preview(self):
        if self._fit_preview_curve is not None:
            try:
                self.plot_widget.removeItem(self._fit_preview_curve)
            except Exception:
                pass
            self._fit_preview_curve = None

    def _on_fit_preview(self, x_fit, y_fit):
        if not _PG_OK:
            return
        try:
            pen = pg.mkPen("#ffcc44", width=2, style=Qt.PenStyle.DotLine)
            if self._fit_preview_curve is None:
                self._fit_preview_curve = self.plot_widget.plot(
                    x_fit, y_fit, pen=pen)
            else:
                self._fit_preview_curve.setData(x_fit, y_fit)
        except Exception:
            pass

    def _on_fit_applied(self, fit_items):
        self._clear_fit_preview()
        self._clear_fit_overlays()
        fit_colors = ["#ff6688", "#66ffaa", "#ffaa33", "#33aaff", "#cc88ff"]
        for idx, item in enumerate(fit_items):
            color = fit_colors[idx % len(fit_colors)]
            x_fit = item.get("x_fit")
            y_fit = item.get("y_fit")
            info  = item.get("info", {})
            label = item.get("label", "fit")
            if x_fit is None or y_fit is None:
                continue
            pen  = pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine)
            key  = f"fit:{label}"
            self._fit_curves[key] = self.plot_widget.plot(
                x_fit, y_fit, pen=pen, name=key)
            x0 = info.get("x0", float("nan"))
            if np.isfinite(x0):
                vline = pg.InfiniteLine(
                    pos=float(x0), angle=90,
                    pen=pg.mkPen(color=color, width=1,
                                 style=Qt.PenStyle.DashLine),
                )
                self.plot_widget.addItem(vline)
                self._fit_texts.append(vline)
        if fit_items:
            info0 = fit_items[0].get("info", {})
            result_obj = info0.get("result")
            self._saved_fit_state = {
                "model_name": fit_items[0].get("model_name", ""),
                "bg_name":    fit_items[0].get("bg_name", "None"),
                "params":     result_obj.params if result_obj else None,
            }

    def _on_fit_cancelled(self):
        self._clear_fit_preview()

    def _open_fit_dialog(self):
        if not _pf.LMFIT_AVAILABLE:
            QMessageBox.warning(self, "lmfit not installed",
                                "pip install lmfit")
            return
        if not self._dfs:
            QMessageBox.warning(self, "No data",
                                "Select at least one scan first.")
            return

        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            QMessageBox.warning(self, "No fields selected",
                                "Select X and at least one Y field.")
            return

        if self._fit_dlg is not None:
            try:
                self._fit_dlg.close()
            except Exception:
                pass

        initial_model   = self._fit_model_combo.currentText()
        initial_bg_name = self._fit_bg_combo.currentText()
        initial_params  = None
        if self._saved_fit_state:
            initial_model   = self._saved_fit_state.get("model_name", initial_model)
            initial_bg_name = self._saved_fit_state.get("bg_name", initial_bg_name)
            initial_params  = self._saved_fit_state.get("params")

        norm_col   = self.norm_combo.currentData()
        deriv_mode = self._deriv_combo.currentIndex()
        log_y      = self._log_y_cb.isChecked()

        datasets = []
        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x        = df[xc].values.astype(float)
            norm_raw = (df[norm_col].values.astype(float)
                        if norm_col and norm_col in df.columns else None)
            for yc in ycs:
                if yc not in df.columns:
                    continue
                y_raw = df[yc].values.astype(float)
                y = y_raw.copy()
                if norm_raw is not None:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(norm_raw != 0, y / norm_raw, np.nan)
                if deriv_mode > 0:
                    sigma = _poisson_sigma(y_raw, norm_raw)
                    _, y, _ = self._apply_deriv(x, y, sigma, order=deriv_mode)
                if log_y:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.log10(np.where(y > 0, y, np.nan))
                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_ = x[mask], y[mask]
                if len(x_) < 4:
                    continue
                lbl = yc if not norm_col else f"{yc}/{norm_col}"
                if len(self._dfs) > 1:
                    lbl = f"{lbl}  [{df_label}]"
                datasets.append((x_, y_, lbl))

        if not datasets:
            QMessageBox.warning(self, "No data",
                                "No plottable data for the selected fields.")
            return

        self._fit_dlg = FitParamsDialog(
            datasets, initial_model, initial_bg_name, initial_params,
            parent=self,
        )
        self._fit_dlg.preview_changed.connect(self._on_fit_preview)
        self._fit_dlg.fit_applied.connect(self._on_fit_applied)
        self._fit_dlg.show()

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _copy_screenshot(self):
        if _PG_OK:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setPixmap(self.plot_widget.grab())

    def closeEvent(self, event):
        if self._crosshair_cleanup:
            self._crosshair_cleanup()
        super().closeEvent(event)
