"""mongo_browser.py — Browse and plot bluesky runs stored in per-profile MongoDB."""

from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import pyqtgraph as pg
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

try:
    import h5py
    H5PY_AVAILABLE = True
except ImportError:
    H5PY_AVAILABLE = False

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSizePolicy, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)


from .config import PLOT_COLORS
from .plot_tools import setup_crosshair, smart_legend_position
from . import peak_fit as _peak_fit
from .curve_fit_dialog import FitParamsDialog as _FitParamsDialog


# ── Module-level helpers ───────────────────────────────────────────────────────

def _poisson_sigma(y_raw, norm_raw=None):
    """Poisson √N error with propagation through y/norm normalization.

    Raw:        σ = √|y|
    Normalized: σ = √(y/n² + y²/n³)  where n = norm_raw
                  = √|y|/n · √(1 + y/n)
    """
    y = np.abs(y_raw)
    if norm_raw is None:
        return np.sqrt(y)
    n = np.abs(norm_raw)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n > 0, np.sqrt(y / n ** 2 + y ** 2 / n ** 3), np.nan)


def _fetch_streams(db, uid: str) -> dict:
    """Fetch all event streams for one run UID from an open pymongo Database.

    Returns {stream_name: {"time": array, "data_keys": dict, field: array, ...}}.
    """
    descs = list(db["event_descriptor"].find({"run_start": uid}))
    result = {}
    for desc in descs:
        stream    = desc.get("name", "primary")
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
            for ev in db["event"].find({"descriptor": desc_uid}).sort("seq_num", 1):
                times.append(ev.get("time", 0))
                edata = ev.get("data", {})
                for field in data_keys:
                    field_data[field].append(edata.get(field))

        if not times:
            continue

        stream_dict = {"time": np.array(times, dtype=float), "data_keys": data_keys}
        for field, vals in field_data.items():
            try:
                stream_dict[field] = np.array(vals, dtype=float)
            except (TypeError, ValueError):
                stream_dict[field] = np.array(vals)
        result[stream] = stream_dict
    return result


# ── Background workers ─────────────────────────────────────────────────────────

class _RunListFetcher(QThread):
    """Fetch the most recent N runs from MongoDB, optionally filtered by experiment."""
    runs_ready = pyqtSignal(list)
    error      = pyqtSignal(str)

    def __init__(self, host, port, db_name, limit=300,
                 exp_dir_filter="", run_uids=None, parent=None):
        super().__init__(parent)
        self._host      = host
        self._port      = port
        self._db        = db_name
        self._limit     = limit
        self._exp_dir   = exp_dir_filter
        self._run_uids  = run_uids or []   # known UIDs from plans_log.jsonl

    def run(self):
        try:
            import pymongo
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db = client[self._db]

            query = {}
            if self._run_uids:
                # Primary: query by UID — works even when exp_dir was never stored.
                # Use OR so that any runs logged in plans_log.jsonl are found,
                # AND any newer runs whose exp_dir matches are also included.
                import re as _re
                conditions = [{"uid": {"$in": self._run_uids}}]
                if self._exp_dir:
                    folder = _re.escape(Path(self._exp_dir).name)
                    conditions.append({"exp_dir": {"$regex": folder + r"/?$"}})
                query = {"$or": conditions}
            elif self._exp_dir:
                # Fallback when plan log is empty: match by folder name.
                import re as _re
                folder = _re.escape(Path(self._exp_dir).name)
                query["exp_dir"] = {"$regex": folder + r"/?$"}

            starts = list(
                db["run_start"].find(query, {
                    "uid": 1, "scan_id": 1, "plan_name": 1,
                    "time": 1, "motors": 1, "detectors": 1, "hints": 1,
                    "exp_dir": 1, "sample_name": 1, "peak_stats": 1,
                }).sort("time", -1).limit(self._limit)
            )
            uids  = [s["uid"] for s in starts]
            stops = {
                s["run_start"]: s
                for s in db["run_stop"].find(
                    {"run_start": {"$in": uids}},
                    {"run_start": 1, "exit_status": 1, "time": 1, "num_events": 1},
                )
            }
            runs = [{"start": s, "stop": stops.get(s["uid"], {})} for s in starts]
            self.runs_ready.emit(runs)
            client.close()
        except Exception as exc:
            self.error.emit(str(exc))


class _MultiRunDataFetcher(QThread):
    """Fetch event data for one or more runs; returns a list of stream dicts."""
    data_ready = pyqtSignal(list)   # list of {uid, label, streams}
    error      = pyqtSignal(str)

    def __init__(self, host, port, db_name, uid_labels, parent=None):
        super().__init__(parent)
        self._host       = host
        self._port       = port
        self._db         = db_name
        self._uid_labels = uid_labels   # list of (uid, label_str)

    def run(self):
        try:
            import pymongo
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db      = client[self._db]
            results = []
            for uid, label in self._uid_labels:
                streams = _fetch_streams(db, uid)
                if streams:
                    results.append({"uid": uid, "label": label, "streams": streams})
            client.close()
            self.data_ready.emit(results)
        except Exception as exc:
            self.error.emit(str(exc))


class _HDF5Exporter(QThread):
    """Export selected runs from MongoDB to an HDF5 file readable by HDF5Viewer."""
    progress = pyqtSignal(int, int)   # (done, total)
    done     = pyqtSignal(str)        # output path
    error    = pyqtSignal(str)

    def __init__(self, host, port, db_name, runs, path, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._db   = db_name
        self._runs = runs   # list of {start: dict, stop: dict}
        self._path = path

    def run(self):
        try:
            import pymongo
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db = client[self._db]

            with h5py.File(self._path, "w") as hf:
                meta = hf.create_group("metadata")
                meta.attrs["n_scans"] = len(self._runs)

                for i, run in enumerate(self._runs):
                    start   = run["start"]
                    stop    = run.get("stop") or {}
                    uid     = start["uid"]
                    scan_id = start.get("scan_id", i + 1)
                    grp     = hf.create_group(f"scan_{scan_id:04d}")

                    grp.attrs["plan_name"]   = str(start.get("plan_name", ""))
                    grp.attrs["scan_num"]    = int(scan_id)
                    grp.attrs["uid"]         = uid
                    grp.attrs["exit_status"] = str(stop.get("exit_status", ""))
                    ts = start.get("time", 0)
                    if ts:
                        grp.attrs["timestamp"] = datetime.fromtimestamp(ts).isoformat()
                    t_start = start.get("time", 0)
                    t_stop  = stop.get("time",  0)
                    if t_start and t_stop:
                        grp.attrs["duration_s"] = float(t_stop - t_start)
                    for k in ("exp_dir", "sample_name", "sample_description"):
                        v = start.get(k, "")
                        if v:
                            grp.attrs[k] = str(v)
                    motors = start.get("motors", [])
                    if motors:
                        grp.attrs["motor"] = str(motors[0])
                    dets = start.get("detectors", [])
                    if dets:
                        grp.attrs["detectors"] = ", ".join(str(d) for d in dets)

                    streams  = _fetch_streams(db, uid)
                    primary  = streams.get("primary", {})
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

                    self.progress.emit(i + 1, len(self._runs))

            client.close()
            self.done.emit(self._path)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Run detail dialog (double-click) ──────────────────────────────────────────

class _FullStartFetcher(QThread):
    """Fetches the complete run_start document (no field projection) for one UID."""
    ready = pyqtSignal(dict)

    def __init__(self, host, port, db_name, uid, parent=None):
        super().__init__(parent)
        self._host = host; self._port = port
        self._db   = db_name; self._uid = uid

    def run(self):
        try:
            import pymongo
            client = pymongo.MongoClient(self._host, self._port,
                                         serverSelectionTimeoutMS=4000)
            doc = client[self._db]["run_start"].find_one({"uid": self._uid},
                                                          {"_id": 0}) or {}
            client.close()
            self.ready.emit(doc)
        except Exception:
            self.ready.emit({})


class _RunDetailDialog(QDialog):
    """Non-modal dialog showing all metadata and data for one run."""

    def __init__(self, run: dict, run_data: dict | None, seq_num: int,
                 mongo_profile: dict | None = None, parent=None):
        super().__init__(parent)
        start = run.get("start", {})
        stop  = run.get("stop",  {})
        uid   = start.get("uid", "")
        self.setWindowTitle(f"Run #{seq_num}  —  {start.get('plan_name','?')}  [{uid[:8]}…]")
        self.setMinimumSize(860, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint
        )

        import json as _json
        self._stop     = stop
        self._seq_num  = seq_num

        root = QVBoxLayout(self)
        root.setSpacing(6)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)

        # ── Tab 1: Metadata ───────────────────────────────────────────────────
        meta_widget   = QWidget()
        meta_lay      = QVBoxLayout(meta_widget)
        self._meta_txt = QTextEdit()
        self._meta_txt.setReadOnly(True)
        self._meta_txt.setFont(QFont("Courier", 10))
        self._meta_txt.setPlainText(self._format_metadata(start, stop, seq_num))
        meta_lay.addWidget(self._meta_txt)
        self._tabs.addTab(meta_widget, "Metadata")

        # ── Tab 2: Data table ─────────────────────────────────────────────────
        data_widget = QWidget()
        data_lay    = QVBoxLayout(data_widget)
        if run_data:
            stream_combo = QComboBox()
            streams = run_data.get("streams", {})
            for s in sorted(streams.keys()):
                stream_combo.addItem(s)
            data_lay.addWidget(stream_combo)
            self._data_table = QTableWidget()
            self._data_table.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers)
            self._data_table.setAlternatingRowColors(True)
            self._data_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows)
            self._data_table.verticalHeader().setVisible(False)
            data_lay.addWidget(self._data_table, 1)
            self._streams = streams
            self._populate_data_table(stream_combo.currentText())
            stream_combo.currentTextChanged.connect(self._populate_data_table)
        else:
            data_lay.addWidget(QLabel(
                "Data not yet loaded — select the run first, then double-click."))
        self._tabs.addTab(data_widget, "Data")

        # ── Tab 3: Peak Stats (populated now if already in start, or after fetch) ──
        self._ps_tab_idx = -1
        self._build_peak_stats_tab(start.get("peak_stats"))

        # ── Tab 4: Raw start doc ──────────────────────────────────────────────
        raw_widget   = QWidget()
        raw_lay      = QVBoxLayout(raw_widget)
        self._raw_txt = QTextEdit()
        self._raw_txt.setReadOnly(True)
        self._raw_txt.setFont(QFont("Courier", 9))
        self._raw_txt.setPlainText(_json.dumps(start, indent=2, default=str))
        raw_lay.addWidget(self._raw_txt)
        self._tabs.addTab(raw_widget, "Start Doc (raw)")

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

        # ── Fetch complete start doc in background ────────────────────────────
        if mongo_profile:
            self._fetcher = _FullStartFetcher(
                host    = mongo_profile.get("mongo_host", "localhost"),
                port    = int(mongo_profile.get("mongo_port", 27017)),
                db_name = mongo_profile.get("mongo_db", ""),
                uid     = uid,
                parent  = self,
            )
            self._fetcher.ready.connect(self._on_full_start)
            self._fetcher.start()

    def _build_peak_stats_tab(self, peak_stats: dict | None):
        """Build (or replace) the Peak Stats tab."""
        if not peak_stats:
            return
        if self._ps_tab_idx >= 0:
            self._tabs.removeTab(self._ps_tab_idx)
        insert_at = self._tabs.count() - 1   # before "Start Doc (raw)"
        ps_widget = QWidget()
        ps_lay    = QVBoxLayout(ps_widget)
        ps_table  = QTableWidget(0, 7)
        ps_table.setHorizontalHeaderLabels(
            ["Signal", "Center", "FWHM", "COM", "Max pos", "Max val", "Min val"])
        ps_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        ps_table.setAlternatingRowColors(True)
        ps_table.verticalHeader().setVisible(False)
        hh = ps_table.horizontalHeader()
        for i in range(7):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        for signal, st in peak_stats.items():
            r = ps_table.rowCount()
            ps_table.insertRow(r)
            ps_table.setItem(r, 0, QTableWidgetItem(signal))
            ps_table.setItem(r, 1, QTableWidgetItem(_fmt(st.get("cen"))))
            ps_table.setItem(r, 2, QTableWidgetItem(_fmt(st.get("fwhm"))))
            ps_table.setItem(r, 3, QTableWidgetItem(_fmt(st.get("com"))))
            ps_table.setItem(r, 4, QTableWidgetItem(_fmt(st.get("max_pos"))))
            ps_table.setItem(r, 5, QTableWidgetItem(_fmt(st.get("max_val"))))
            ps_table.setItem(r, 6, QTableWidgetItem(_fmt(st.get("min_val"))))
        ps_lay.addWidget(ps_table, 1)
        self._ps_tab_idx = self._tabs.insertTab(insert_at, ps_widget, "Peak Stats")

    def _on_full_start(self, full_doc: dict):
        """Called by _FullStartFetcher when the complete start doc is ready."""
        import json as _json
        if not full_doc:
            return
        self._meta_txt.setPlainText(
            self._format_metadata(full_doc, self._stop, self._seq_num)
        )
        self._raw_txt.setPlainText(_json.dumps(full_doc, indent=2, default=str))
        ps = full_doc.get("peak_stats")
        if ps:
            self._build_peak_stats_tab(ps)

    def _format_metadata(self, start: dict, stop: dict, seq_num: int) -> str:
        ts_start = start.get("time", 0)
        ts_stop  = stop.get("time", 0) if stop else 0
        dur = f"{ts_stop - ts_start:.2f} s" if ts_start and ts_stop else "—"
        lines = [
            f"Scan #        : {seq_num}",
            f"Plan          : {start.get('plan_name', '—')}",
            f"UID           : {start.get('uid', '—')}",
            f"Scan ID       : {start.get('scan_id', '—')}",
            f"Status        : {(stop or {}).get('exit_status', 'running')}",
            f"Start         : {datetime.fromtimestamp(ts_start).strftime('%Y-%m-%d %H:%M:%S') if ts_start else '—'}",
            f"Stop          : {datetime.fromtimestamp(ts_stop).strftime('%Y-%m-%d %H:%M:%S') if ts_stop else '—'}",
            f"Duration      : {dur}",
            f"Num events    : {(stop or {}).get('num_events', {}).get('primary', '—')}",
            f"Motors        : {', '.join(start.get('motors', [])) or '—'}",
            f"Detectors     : {', '.join(start.get('detectors', [])) or '—'}",
        ]
        sample = start.get("sample_name", "")
        if sample:
            lines.append(f"Sample        : {sample}")
        exp = start.get("exp_dir", "")
        if exp:
            lines.append(f"Exp dir       : {exp}")
        # extra md keys
        skip = {"uid", "time", "plan_name", "scan_id", "motors", "detectors",
                "sample_name", "exp_dir", "hints", "plan_args", "plan_pattern",
                "plan_type", "peak_stats"}
        extras = {k: v for k, v in start.items() if k not in skip}
        if extras:
            lines.append("")
            lines.append("─── Extra metadata ───")
            for k, v in extras.items():
                lines.append(f"{k:<14}: {v}")
        ps = start.get("peak_stats")
        if ps:
            lines.append("")
            lines.append("─── Peak stats ───")
            for sig, st in ps.items():
                cen  = st.get("cen")
                fwhm = st.get("fwhm")
                com  = st.get("com")
                mxv  = st.get("max_val")
                mxp  = st.get("max_pos")
                lines.append(
                    f"{sig:<14}: cen={_fmt(cen)}  FWHM={_fmt(fwhm)}"
                    f"  COM={_fmt(com)}  max={_fmt(mxv)}@{_fmt(mxp)}"
                )
        return "\n".join(lines)

    def _populate_data_table(self, stream_name: str):
        sdata = self._streams.get(stream_name, {})
        data_keys = sdata.get("data_keys", {})
        fields = [k for k in data_keys if k in sdata]
        time_arr = sdata.get("time", np.array([]))
        n = len(time_arr)
        if not n:
            self._data_table.setRowCount(0)
            self._data_table.setColumnCount(0)
            return

        cols = ["seq_num", "time"] + fields
        self._data_table.setColumnCount(len(cols))
        self._data_table.setHorizontalHeaderLabels(cols)
        self._data_table.setRowCount(n)

        hh = self._data_table.horizontalHeader()
        for i in range(len(cols)):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

        for i in range(n):
            self._data_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            t_str = datetime.fromtimestamp(float(time_arr[i])).strftime("%H:%M:%S.%f")[:-3]
            self._data_table.setItem(i, 1, QTableWidgetItem(t_str))
            for j, f in enumerate(fields):
                arr = sdata.get(f, [])
                v = arr[i] if i < len(arr) else ""
                try:
                    txt = f"{float(v):.6g}"
                except (TypeError, ValueError):
                    txt = str(v)
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._data_table.setItem(i, j + 2, item)


def _fmt(v, spec=".5g"):
    return "—" if v is None else format(float(v), spec)


# ── Main tab ───────────────────────────────────────────────────────────────────

class MongoDataBrowserTab(QWidget):
    """Browse bluesky runs stored in MongoDB; supports multi-run selection,
    common-column overlay plotting, experiment filtering, and HDF5 export."""

    COLORS = PLOT_COLORS

    move_requested = pyqtSignal(str, float)   # (motor_name, target_position)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings       = settings
        self._runs: list     = []       # raw run dicts from _RunListFetcher
        self._run_data_list  = []       # [{uid, label, streams}] for selected rows
        self._run_fetcher    = None
        self._data_fetcher   = None
        self._hdf5_exporter  = None
        self._btn_export_exp = None
        self._curves: dict      = {}
        self._error_items: dict = {}   # pg.ErrorBarItem per curve
        self._fit_curves: dict       = {}   # fit overlay curves {label: PlotDataItem}
        self._fit_texts: list        = []   # pg.TextItem annotations for fit results
        self._fit_preview_curve      = None   # live preview PlotDataItem (dotted)
        self._fit_dlg                = None   # open FitParamsDialog (non-modal ref)
        self._fit_datasets: list     = []     # datasets passed to the open dialog
        self._saved_fit_state: dict  = None   # {model_name, bg_name, params} — persists
        self._active_exp_dir = ""       # current experiment filter
        self._saved_x: str   = ""       # last X field key — restored on run switch
        self._saved_y: set   = set()    # last checked Y field names — restored on run switch
        self._crosshair_cleanup = None
        self._fetch_timer    = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.timeout.connect(self._schedule_data_fetch)

        self._build_ui()
        self._populate_profile_combo()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Top bar: profile / DB / experiment filter ─────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("Profile:"))

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(160)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        top.addWidget(self._profile_combo)

        self._db_label = QLabel("")
        self._db_label.setObjectName("dim_text")
        top.addWidget(self._db_label, 1)

        top.addWidget(_vline())

        self._exp_label = QLabel("All experiments")
        self._exp_label.setObjectName("dim_text")
        self._exp_label.setMinimumWidth(200)
        top.addWidget(self._exp_label)

        self._show_all_cb = QCheckBox("All runs")
        self._show_all_cb.setToolTip(
            "When unchecked, only runs from the active experiment are shown"
        )
        self._show_all_cb.toggled.connect(self._fetch_runs)
        top.addWidget(self._show_all_cb)

        btn_refresh = QPushButton("↻  Refresh")
        btn_refresh.setFixedHeight(28)
        btn_refresh.clicked.connect(self._fetch_runs)
        top.addWidget(btn_refresh)

        root.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # ── Main splitter ─────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: run table + info ─────────────────────────────────────────────
        left = QWidget()
        left.setMinimumWidth(320)
        left.setMaximumWidth(500)
        llayout = QVBoxLayout(left)
        llayout.setContentsMargins(0, 0, 0, 0)
        llayout.setSpacing(4)

        self._run_table = QTableWidget(0, 6)
        self._run_table.setHorizontalHeaderLabels(
            ["Scan #", "Plan", "Date / Time", "Status", "Points", "Detectors"]
        )
        hh = self._run_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._run_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._run_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._run_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._run_table.verticalHeader().setVisible(False)
        self._run_table.setAlternatingRowColors(True)
        self._run_table.itemSelectionChanged.connect(self._on_run_selected)
        self._run_table.cellDoubleClicked.connect(self._on_run_double_clicked)
        llayout.addWidget(self._run_table, 1)

        info_frame = QFrame()
        info_frame.setFrameShape(QFrame.Shape.StyledPanel)
        info_lay = QVBoxLayout(info_frame)
        info_lay.setContentsMargins(6, 4, 6, 4)
        info_lay.setSpacing(2)
        self._info_label = QLabel("Select a run to see details.")
        self._info_label.setWordWrap(True)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self._info_label.setFont(mono)
        info_lay.addWidget(self._info_label)
        llayout.addWidget(info_frame)

        splitter.addWidget(left)

        # ── Right: compact toolbar + plot ─────────────────────────────────────
        right   = QWidget()
        rlayout = QVBoxLayout(right)
        rlayout.setContentsMargins(0, 0, 0, 0)
        rlayout.setSpacing(2)

        # Single compact toolbar row — no GroupBox, labels inlined as text or tooltips
        ctrl_bar = QHBoxLayout()
        ctrl_bar.setContentsMargins(2, 2, 2, 2)
        ctrl_bar.setSpacing(4)

        ctrl_bar.addWidget(QLabel("Stream:"))
        self._stream_combo = QComboBox()
        self._stream_combo.setMinimumWidth(90)
        self._stream_combo.setMaximumWidth(180)
        self._stream_combo.setFixedHeight(26)
        self._stream_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._stream_combo.setToolTip("Event stream")
        self._stream_combo.currentIndexChanged.connect(self._on_stream_changed)
        ctrl_bar.addWidget(self._stream_combo)

        ctrl_bar.addWidget(_vline())
        ctrl_bar.addWidget(QLabel("X:"))
        self._x_combo = QComboBox()
        self._x_combo.setMinimumWidth(120)
        self._x_combo.setMaximumWidth(240)
        self._x_combo.setFixedHeight(26)
        self._x_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._x_combo.setToolTip("X axis signal")
        self._x_combo.currentIndexChanged.connect(self._auto_plot)
        ctrl_bar.addWidget(self._x_combo)

        ctrl_bar.addWidget(_vline())
        ctrl_bar.addWidget(QLabel("Norm:"))
        self._norm_combo = QComboBox()
        self._norm_combo.setMinimumWidth(100)
        self._norm_combo.setMaximumWidth(220)
        self._norm_combo.setFixedHeight(26)
        self._norm_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._norm_combo.setToolTip("Divide Y by this signal")
        self._norm_combo.addItem("None", userData=None)
        self._norm_combo.currentIndexChanged.connect(self._auto_plot)
        ctrl_bar.addWidget(self._norm_combo)

        ctrl_bar.addWidget(_vline())
        self._log_y_cb = QCheckBox("Log Y")
        self._log_y_cb.stateChanged.connect(self._auto_plot)
        ctrl_bar.addWidget(self._log_y_cb)

        self._err_cb = QCheckBox("± Errors")
        self._err_cb.setToolTip(
            "Overlay Poisson √N error bars (propagated through normalization;\n"
            "converted to log₁₀ space when Log Y is active)"
        )
        self._err_cb.stateChanged.connect(self._auto_plot)
        ctrl_bar.addWidget(self._err_cb)

        ctrl_bar.addWidget(_vline())
        ctrl_bar.addWidget(QLabel("Fit:"))
        self._fit_model_combo = QComboBox()
        for m in _peak_fit.PEAK_MODELS:
            self._fit_model_combo.addItem(m)
        self._fit_model_combo.insertSeparator(self._fit_model_combo.count())
        for m in _peak_fit.STEP_MODELS:
            self._fit_model_combo.addItem(m)
        self._fit_model_combo.setFixedHeight(26)
        self._fit_model_combo.setToolTip("Peak/step model for curve fitting")
        ctrl_bar.addWidget(self._fit_model_combo)

        bg_lbl = QLabel("+ BG:")
        bg_lbl.setStyleSheet("font-size: 11px;")
        ctrl_bar.addWidget(bg_lbl)
        self._fit_bg_combo = QComboBox()
        self._fit_bg_combo.setFixedHeight(26)
        self._fit_bg_combo.setMinimumWidth(80)
        self._fit_bg_combo.setMaximumWidth(110)
        for bg in _peak_fit.BACKGROUND_MODELS:
            self._fit_bg_combo.addItem(bg)
        self._fit_bg_combo.setToolTip("Background model added to the peak/step")
        ctrl_bar.addWidget(self._fit_bg_combo)

        self._btn_fit = QPushButton("Fit")
        self._btn_fit.setFixedHeight(26)
        self._btn_fit.setToolTip("Fit a peak to the plotted data")
        self._btn_fit.clicked.connect(self._fit_peak)
        ctrl_bar.addWidget(self._btn_fit)
        self._btn_clear_fit = QPushButton("✕")
        self._btn_clear_fit.setFixedSize(26, 26)
        self._btn_clear_fit.setToolTip("Clear fit overlays")
        self._btn_clear_fit.setEnabled(False)
        self._btn_clear_fit.clicked.connect(self._clear_fit_overlays)
        ctrl_bar.addWidget(self._btn_clear_fit)

        ctrl_bar.addWidget(_vline())
        btn_screenshot = QPushButton("Screenshot")
        btn_screenshot.setFixedHeight(26)
        btn_screenshot.setToolTip("Copy plot to clipboard")
        btn_screenshot.clicked.connect(self._save_screenshot)
        ctrl_bar.addWidget(btn_screenshot)

        self._btn_export_hdf5 = QPushButton("Export HDF5…")
        self._btn_export_hdf5.setFixedHeight(26)
        self._btn_export_hdf5.setToolTip(
            "Export selected run(s) to an HDF5 file readable by the HDF5 Viewer tab"
        )
        self._btn_export_hdf5.clicked.connect(self._export_hdf5)
        if not H5PY_AVAILABLE:
            self._btn_export_hdf5.setEnabled(False)
            self._btn_export_hdf5.setToolTip("pip install h5py to enable HDF5 export")
        ctrl_bar.addWidget(self._btn_export_hdf5)

        self._btn_export_exp = QPushButton("Export Exp…")
        self._btn_export_exp.setFixedHeight(26)
        self._btn_export_exp.setToolTip(
            "Export ALL currently-displayed runs to a single HDF5 file\n"
            "(respects the experiment filter when active)"
        )
        self._btn_export_exp.clicked.connect(self._export_experiment_hdf5)
        if not H5PY_AVAILABLE:
            self._btn_export_exp.setEnabled(False)
        ctrl_bar.addWidget(self._btn_export_exp)

        rlayout.addLayout(ctrl_bar)

        self._coord_label = QLabel("")
        self._coord_label.setObjectName("dim_text")
        self._coord_label.setStyleSheet(
            "font-size: 11px; padding: 2px 4px;"
            " font-family: Menlo, Monaco, 'Courier New', monospace;"
        )

        # Y list on the right of the plot (in a resizable splitter)
        self._y_list = QListWidget()
        self._y_list.setMinimumWidth(100)
        self._y_list.setToolTip("Y signals — check to plot")
        self._y_list.itemChanged.connect(self._auto_plot)
        y_lbl = QLabel("Y signals")
        y_lbl.setObjectName("dim_text")
        y_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        y_container = QWidget()
        y_layout = QVBoxLayout(y_container)
        y_layout.setSpacing(2)
        y_layout.setContentsMargins(4, 0, 0, 0)
        y_layout.addWidget(y_lbl)
        y_layout.addWidget(self._y_list, 1)

        if PG_AVAILABLE:
            self._plot_widget = pg.PlotWidget(background="#1e1e1e")
            self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self._plot_widget.addLegend()
            self._plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)
            self._crosshair_cleanup = setup_crosshair(
                self._plot_widget, self._coord_label, lambda: self._curves
            )
            plot_area = self._plot_widget
        else:
            self._plot_widget = None
            plot_area = QLabel("pyqtgraph not available — pip install pyqtgraph")

        plot_splitter = QSplitter(Qt.Orientation.Horizontal)
        plot_splitter.addWidget(plot_area)
        plot_splitter.addWidget(y_container)
        plot_splitter.setSizes([720, 180])
        plot_splitter.setStretchFactor(0, 1)
        plot_splitter.setStretchFactor(1, 0)
        rlayout.addWidget(plot_splitter, 1)
        rlayout.addWidget(self._coord_label)
        splitter.addWidget(right)
        splitter.setSizes([400, 900])
        root.addWidget(splitter, 1)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

    # ── Profile management ─────────────────────────────────────────────────────

    def _populate_profile_combo(self):
        active   = self._settings.get("active_profile", "")
        profiles = [
            p for p in self._settings.get("profiles", [])
            if p.get("mongo_db", "").strip()
        ]
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        active_idx = 0
        for i, p in enumerate(profiles):
            self._profile_combo.addItem(p["name"], userData=p)
            if p["name"] == active:
                active_idx = i
        self._profile_combo.blockSignals(False)

        if not profiles:
            self._db_label.setText(
                "No profiles with MongoDB configured.  "
                "Add a Database name in File → Connection Settings."
            )
            self._profile_combo.setEnabled(False)
            return

        self._profile_combo.setEnabled(True)
        self._profile_combo.setCurrentIndex(active_idx)
        self._on_profile_changed(active_idx)

    def update_settings(self, settings: dict):
        self._settings = settings
        self._populate_profile_combo()

    def _current_profile(self):
        return self._profile_combo.currentData() or {}

    def _on_profile_changed(self, _idx: int):
        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if db:
            self._db_label.setText(f"  {db}  @  {host}:{port}")
            self._runs          = []
            self._run_data_list = []
            self._run_table.setRowCount(0)
            self._clear_axis_controls()
            self._fetch_runs()
        else:
            self._db_label.setText("  (no MongoDB configured)")

    # ── Experiment filter ──────────────────────────────────────────────────────

    def set_active_experiment(self, exp_dir: str):
        """Called by the main window when the active experiment changes."""
        self._active_exp_dir = exp_dir
        name = Path(exp_dir).name if exp_dir else ""
        self._exp_label.setText(f"Exp: {name}" if name else "All experiments")
        self._show_all_cb.setChecked(False)   # auto-apply the filter
        self._fetch_runs()

    def refresh(self):
        """Re-fetch the run list (called after a scan completes)."""
        self._fetch_runs()

    # ── Run list ───────────────────────────────────────────────────────────────

    def _read_exp_run_uids(self) -> list:
        """Read run UIDs from the active experiment's plans_log.jsonl."""
        if not self._active_exp_dir:
            return []
        log_file = Path(self._active_exp_dir) / "plans_log.jsonl"
        if not log_file.exists():
            return []
        uids = []
        try:
            import json as _json
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = _json.loads(line)
                        uids.extend(entry.get("run_uids", []))
                    except Exception:
                        pass
        except Exception:
            pass
        return uids

    def _fetch_runs(self):
        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if not db:
            return
        if self._run_fetcher and self._run_fetcher.isRunning():
            return

        show_all   = self._show_all_cb.isChecked()
        exp_filter = "" if show_all else self._active_exp_dir
        run_uids   = [] if show_all else self._read_exp_run_uids()
        self._set_status("Fetching runs…", busy=True)
        self._run_fetcher = _RunListFetcher(
            host, int(port), db, limit=300,
            exp_dir_filter=exp_filter, run_uids=run_uids, parent=self,
        )
        self._run_fetcher.runs_ready.connect(self._on_runs_ready)
        self._run_fetcher.error.connect(self._on_fetch_error)
        self._run_fetcher.start()

    def _on_runs_ready(self, runs: list):
        self._runs = runs
        self._run_table.setRowCount(0)

        for row, run in enumerate(runs):
            start = run["start"]
            stop  = run["stop"]

            self._run_table.insertRow(row)

            # Sequential scan number: oldest run in the result set = #1,
            # newest = #N.  The query sorts time desc so row 0 is newest.
            seq_num = str(len(runs) - row)
            plan    = start.get("plan_name", "—")
            ts      = start.get("time", 0)
            dt_str  = (datetime.fromtimestamp(ts).strftime("%Y-%m-%d  %H:%M:%S")
                       if ts else "—")
            exit_st = stop.get("exit_status", "running" if not stop else "—")
            num_ev  = (str(stop.get("num_events", {}).get("primary", "—"))
                       if stop else "—")
            dets    = ", ".join(start.get("detectors", [])) or "—"

            if exit_st == "success":
                status_color, status_icon = "#2ca02c", "✓ success"
            elif exit_st in ("fail", "error"):
                status_color, status_icon = "#d62728", "✗ fail"
            elif exit_st == "abort":
                status_color, status_icon = "#ff7f0e", "⊘ abort"
            else:
                status_color, status_icon = "#888888", "… running"

            cols = [seq_num, plan, dt_str, status_icon, num_ev, dets]
            for col, text in enumerate(cols):
                item = QTableWidgetItem(text)
                if col in (0, 4):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if col == 3:
                    item.setForeground(QColor(status_color))
                self._run_table.setItem(row, col, item)

        n      = len(runs)
        suffix = (" (experiment)" if (self._active_exp_dir and
                                       not self._show_all_cb.isChecked()) else "")
        self._set_status(f"{n} run{'s' if n != 1 else ''} loaded{suffix}.")

    def _on_fetch_error(self, msg: str):
        self._set_status(f"Error: {msg}", error=True)

    # ── Run selection (debounced) ──────────────────────────────────────────────

    def _on_run_selected(self):
        rows = self._run_table.selectionModel().selectedRows()
        if not rows:
            self._info_label.setText("Select a run to see details.")
            self._run_data_list = []
            self._clear_axis_controls()
            self._clear_plot()
            return

        if len(rows) == 1:
            self._update_info_single(rows[0].row())
        else:
            self._info_label.setText(f"{len(rows)} runs selected.")

        self._fetch_timer.stop()
        self._fetch_timer.start(180)

    def _on_run_double_clicked(self, row: int, _col: int):
        if row >= len(self._runs):
            return
        run     = self._runs[row]
        seq_num = len(self._runs) - row
        # Find matching run_data if already fetched
        uid = run["start"].get("uid", "")
        run_data = next(
            (rd for rd in self._run_data_list if rd.get("uid") == uid), None
        )
        dlg = _RunDetailDialog(run, run_data, seq_num,
                              mongo_profile=self._current_profile(), parent=self)
        dlg.show()

    def _update_info_single(self, row: int):
        if row >= len(self._runs):
            return
        run   = self._runs[row]
        start = run["start"]
        stop  = run["stop"]

        seq_num  = len(self._runs) - row
        plan     = start.get("plan_name", "—")
        ts_start = start.get("time", 0)
        ts_stop  = stop.get("time", 0) if stop else 0
        dur      = f"{ts_stop - ts_start:.1f} s" if ts_start and ts_stop else "—"
        motors   = ", ".join(start.get("motors", [])) or "—"
        dets     = ", ".join(start.get("detectors", [])) or "—"
        uid      = start.get("uid", "")[:8]
        exit_st  = stop.get("exit_status", "running") if stop else "running"

        self._info_label.setText(
            f"Scan #  : {seq_num}\n"
            f"Plan    : {plan}\n"
            f"Start   : {datetime.fromtimestamp(ts_start).strftime('%Y-%m-%d %H:%M:%S') if ts_start else '—'}\n"
            f"Duration: {dur}\n"
            f"Status  : {exit_st}\n"
            f"Motors  : {motors}\n"
            f"Dets    : {dets}\n"
            f"UID     : {uid}…"
        )

    def _schedule_data_fetch(self):
        """Debounce callback — launches the data fetcher for all selected rows."""
        rows = self._run_table.selectionModel().selectedRows()
        if not rows:
            return

        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if not db:
            return

        MAX_RUNS = 10
        if len(rows) > MAX_RUNS:
            self._set_status(
                f"Too many runs selected ({len(rows)}). "
                f"Loading data for the first {MAX_RUNS}.", busy=True
            )
            rows = rows[:MAX_RUNS]
        else:
            self._set_status(f"Loading data for {len(rows)} run(s)…", busy=True)

        uid_labels = []
        for idx in rows:
            row = idx.row()
            if row >= len(self._runs):
                continue
            start   = self._runs[row]["start"]
            uid     = start.get("uid", "")
            seq_num = len(self._runs) - row
            plan    = start.get("plan_name", "?")
            uid_labels.append((uid, f"#{seq_num} {plan}"))

        if not uid_labels:
            return

        if self._data_fetcher and self._data_fetcher.isRunning():
            self._data_fetcher.terminate()

        self._data_fetcher = _MultiRunDataFetcher(
            host, int(port), db, uid_labels, parent=self
        )
        self._data_fetcher.data_ready.connect(self._on_data_ready)
        self._data_fetcher.error.connect(self._on_fetch_error)
        self._data_fetcher.start()

    def _on_data_ready(self, run_data_list: list):
        self._run_data_list = run_data_list
        self._populate_axis_controls()

        n_events = sum(
            len(v.get("time", []))
            for rd in run_data_list
            for v in rd.get("streams", {}).values()
        )
        n_runs = len(run_data_list)
        self._set_status(
            f"Data loaded — {n_runs} run(s), ~{n_events} total events."
        )
        rows = self._run_table.selectionModel().selectedRows()
        if len(rows) == 1 and rows[0].row() < len(self._runs):
            self._update_info_single(rows[0].row())

        self._plot()

    # ── Axis controls ──────────────────────────────────────────────────────────

    def _clear_axis_controls(self):
        self._stream_combo.blockSignals(True)
        self._stream_combo.clear()
        self._stream_combo.blockSignals(False)
        self._x_combo.clear()
        self._y_list.clear()
        self._norm_combo.blockSignals(True)
        self._norm_combo.clear()
        self._norm_combo.addItem("None", userData=None)
        self._norm_combo.blockSignals(False)

    def _populate_axis_controls(self):
        if not self._run_data_list:
            self._clear_axis_controls()
            return

        # Streams present in ALL runs (intersection)
        stream_sets    = [set(rd["streams"].keys()) for rd in self._run_data_list]
        common_streams = stream_sets[0].intersection(*stream_sets[1:])

        self._stream_combo.blockSignals(True)
        self._stream_combo.clear()
        for s in sorted(common_streams):
            self._stream_combo.addItem(s)
        idx = self._stream_combo.findText("primary")
        if idx >= 0:
            self._stream_combo.setCurrentIndex(idx)
        self._stream_combo.blockSignals(False)
        self._update_field_lists()

    def _on_stream_changed(self, _idx: int):
        self._update_field_lists()
        self._auto_plot()

    def _update_field_lists(self):
        if not self._run_data_list:
            return
        stream = self._stream_combo.currentText()
        if not stream:
            return

        def numeric_fields(rd):
            sdata = rd["streams"].get(stream, {})
            return {
                k for k, v in sdata.items()
                if k not in ("time", "data_keys")
                and isinstance(v, np.ndarray)
                and v.dtype.kind in ("f", "i", "u")
            }

        runs_with_stream = [rd for rd in self._run_data_list if stream in rd["streams"]]
        if not runs_with_stream:
            return
        field_sets = [numeric_fields(rd) for rd in runs_with_stream]
        common     = field_sets[0].intersection(*field_sets[1:])

        first_sdata = runs_with_stream[0]["streams"].get(stream, {})
        keys = [k for k in first_sdata
                if k not in ("time", "data_keys") and k in common]

        # Save current selections before repopulating so they survive run switches
        cur_x = self._x_combo.currentData()
        if cur_x:
            self._saved_x = cur_x
        cur_y = {
            self._y_list.item(i).text()
            for i in range(self._y_list.count())
            if self._y_list.item(i).checkState() == Qt.CheckState.Checked
        }
        if cur_y:
            self._saved_y = cur_y

        # ── X ─────────────────────────────────────────────────────────────────
        self._x_combo.clear()
        self._x_combo.addItem("time (s)", userData="time")
        self._x_combo.addItem("sequence #", userData="seq_num")

        auto_motor = ""
        rows = self._run_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._runs):
            start = self._runs[rows[0].row()]["start"]
            dims  = start.get("hints", {}).get("dimensions", [])
            if dims:
                for f in (dims[0][0] if dims[0] else []):
                    if f in keys:
                        auto_motor = f
                        break
            if not auto_motor:
                motors = start.get("motors", [])
                if motors:
                    for k in keys:
                        if k == motors[0] or k.startswith(motors[0]):
                            auto_motor = k
                            break

        for k in sorted(keys):
            self._x_combo.addItem(k, userData=k)

        # Restore saved X if available; fall back to auto-detected motor
        restored_x = False
        if self._saved_x:
            for i in range(self._x_combo.count()):
                if self._x_combo.itemData(i) == self._saved_x:
                    self._x_combo.setCurrentIndex(i)
                    restored_x = True
                    break
        if not restored_x and auto_motor:
            for i in range(self._x_combo.count()):
                if self._x_combo.itemData(i) == auto_motor:
                    self._x_combo.setCurrentIndex(i)
                    break

        # ── Y ─────────────────────────────────────────────────────────────────
        motor_names = set()
        if rows and rows[0].row() < len(self._runs):
            motor_names = set(self._runs[rows[0].row()]["start"].get("motors", []))

        key_set = set(keys)
        self._y_list.blockSignals(True)
        self._y_list.clear()
        for k in sorted(keys):
            item = QListWidgetItem(k)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            is_motor = any(k == m or k.startswith(m) for m in motor_names)
            item.setCheckState(
                Qt.CheckState.Unchecked if is_motor else Qt.CheckState.Checked
            )
            self._y_list.addItem(item)

        # Restore saved Y if any saved field is present in the new run
        if self._saved_y & key_set:
            for i in range(self._y_list.count()):
                item = self._y_list.item(i)
                item.setCheckState(
                    Qt.CheckState.Checked if item.text() in self._saved_y
                    else Qt.CheckState.Unchecked
                )
        self._y_list.blockSignals(False)

        # ── Norm by ───────────────────────────────────────────────────────────
        prev_norm = self._norm_combo.currentData()
        self._norm_combo.blockSignals(True)
        self._norm_combo.clear()
        self._norm_combo.addItem("None", userData=None)
        for k in sorted(keys):
            self._norm_combo.addItem(k, userData=k)
        for i in range(self._norm_combo.count()):
            if self._norm_combo.itemData(i) == prev_norm:
                self._norm_combo.setCurrentIndex(i)
                break
        self._norm_combo.blockSignals(False)

    # ── Plotting ───────────────────────────────────────────────────────────────

    def _plot(self):
        if not PG_AVAILABLE or self._plot_widget is None:
            return
        if not self._run_data_list:
            self._set_status("No data — select run(s) first.", error=True)
            return

        stream     = self._stream_combo.currentText()
        x_field    = self._x_combo.currentData() or self._x_combo.currentText()
        y_fields   = [
            self._y_list.item(i).text()
            for i in range(self._y_list.count())
            if self._y_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        norm_field = self._norm_combo.currentData()

        if not y_fields:
            self._set_status("Select at least one Y signal.", error=True)
            return

        for item in list(self._error_items.values()) + list(self._curves.values()):
            try:
                self._plot_widget.removeItem(item)
            except Exception:
                pass
        pi = self._plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves     = {}
        self._error_items = {}

        log_y     = self._log_y_cb.isChecked()
        show_err  = self._err_cb.isChecked()
        color_idx = 0
        multi_run = len(self._run_data_list) > 1

        for rd in self._run_data_list:
            sdata = rd["streams"].get(stream)
            if not sdata:
                continue

            run_label = rd["label"] if multi_run else ""

            if x_field == "time":
                x_raw = sdata.get("time")
                if x_raw is None or not len(x_raw):
                    continue
                x_arr   = x_raw - x_raw[0]
                x_label = "Time  (s)"
            elif x_field == "seq_num":
                t     = sdata.get("time")
                x_arr = np.arange(1, len(t) + 1) if t is not None else np.array([])
                x_label = "Sequence #"
            else:
                x_arr = sdata.get(x_field)
                x_label = x_field
                if x_arr is None:
                    continue

            norm_arr = None
            if norm_field and norm_field in sdata:
                norm_arr = sdata[norm_field].astype(float)

            for field in y_fields:
                y_arr = sdata.get(field)
                if y_arr is None or not len(y_arr):
                    continue
                n = min(len(x_arr), len(y_arr))
                x     = x_arr[:n].astype(float)
                y_raw = y_arr[:n].astype(float)
                norm_raw = norm_arr[:n] if norm_arr is not None else None

                # Normalization
                y = y_raw.copy()
                if norm_raw is not None:
                    denom = norm_raw
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(denom != 0, y / denom, np.nan)

                # Poisson σ in linear space (before log transform)
                sigma    = _poisson_sigma(y_raw, norm_raw)
                y_linear = y.copy()   # y after norm, before log — needed for σ conversion

                if log_y:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.log10(np.where(y > 0, y, np.nan))
                    # Convert σ to log₁₀ space: σ_log = σ_lin / (y_lin · ln10)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        sigma = np.where(
                            y_linear > 0,
                            sigma / (y_linear * np.log(10)),
                            np.nan,
                        )

                mask = np.isfinite(x) & np.isfinite(y)
                x, y  = x[mask], y[mask]
                sigma = sigma[mask]
                if not len(x):
                    continue

                color     = self.COLORS[color_idx % len(self.COLORS)]
                pen       = pg.mkPen(color=color, width=2)
                base_name = field if not norm_field else f"{field}/{norm_field}"
                name      = f"{base_name}  [{run_label}]" if run_label else base_name
                curve     = self._plot_widget.plot(
                    x, y, pen=pen, name=name,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
                self._curves[name] = curve

                if show_err and np.any(np.isfinite(sigma)):
                    err_item = pg.ErrorBarItem(
                        x=x, y=y, height=2 * sigma,
                        beam=0.0, pen=pg.mkPen(color=color, width=1),
                    )
                    self._plot_widget.addItem(err_item)
                    self._error_items[name] = err_item

                color_idx += 1

        self._plot_widget.setLabel("bottom", x_label)
        y_label = ", ".join(y_fields)
        if norm_field:
            y_label += f"  /  {norm_field}"
        if log_y:
            y_label += "  [log₁₀]"
        self._plot_widget.setLabel("left", y_label)

        rows = self._run_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._runs):
            start   = self._runs[rows[0].row()]["start"]
            seq_num = len(self._runs) - rows[0].row()
            plan    = start.get("plan_name", "")
            title   = f"Scan {seq_num}  —  {plan}"
            if len(rows) > 1:
                title += f"  (+{len(rows)-1} more)"
            self._plot_widget.setTitle(title)
        smart_legend_position(self._plot_widget)

    def _clear_plot(self):
        if self._plot_widget is None:
            return
        self._clear_fit_overlays()
        for item in list(self._error_items.values()) + list(self._curves.values()):
            try:
                self._plot_widget.removeItem(item)
            except Exception:
                pass
        pi = self._plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves      = {}
        self._error_items = {}
        self._plot_widget.setTitle("")

    def _auto_plot(self, *_args):
        """Re-plot whenever axis controls change — guard against no data."""
        if self._run_data_list:
            self._plot()

    # ── Peak fitting ───────────────────────────────────────────────────────────

    def _clear_fit_overlays(self):
        """Remove all fit curves and text annotations from the plot."""
        if self._plot_widget is None:
            return
        for curve in self._fit_curves.values():
            try:
                self._plot_widget.removeItem(curve)
            except Exception:
                pass
        for ti in self._fit_texts:
            try:
                self._plot_widget.removeItem(ti)
            except Exception:
                pass
        self._fit_curves = {}
        self._fit_texts  = []
        if hasattr(self, "_btn_clear_fit"):
            self._btn_clear_fit.setEnabled(False)

    def _clear_fit_preview(self):
        """Remove the live preview curve from the plot."""
        if self._fit_preview_curve is not None and self._plot_widget is not None:
            try:
                self._plot_widget.removeItem(self._fit_preview_curve)
            except Exception:
                pass
            self._fit_preview_curve = None

    def _get_xy_for_fit(self, sdata, x_field, y_field, norm_field):
        """Return (x, y) arrays ready for fitting, or (None, None) on failure."""
        y_raw = sdata.get(y_field)
        if y_raw is None or not len(y_raw):
            return None, None

        if x_field == "time":
            t = sdata.get("time")
            if t is None:
                return None, None
            x_raw = t - t[0]
        elif x_field == "seq_num":
            t = sdata.get("time")
            if t is None:
                return None, None
            x_raw = np.arange(1, len(t) + 1, dtype=float)
        else:
            x_raw = sdata.get(x_field)
            if x_raw is None:
                return None, None

        n = min(len(x_raw), len(y_raw))
        x = x_raw[:n].astype(float)
        y = y_raw[:n].astype(float)

        if norm_field and norm_field in sdata:
            denom = sdata[norm_field][:n].astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                y = np.where(denom != 0, y / denom, np.nan)

        mask = np.isfinite(x) & np.isfinite(y)
        return x[mask], y[mask]

    def _add_fit_overlay(self, x_fit, y_fit, info, label, log_y, color_idx):
        """Draw one fit curve + vertical line at x₀ on the plot."""
        if not PG_AVAILABLE or self._plot_widget is None:
            return
        color = self.COLORS[color_idx % len(self.COLORS)]
        pen   = pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine)

        y_plot = y_fit.copy()
        if log_y:
            with np.errstate(divide="ignore", invalid="ignore"):
                y_plot = np.log10(np.where(y_fit > 0, y_fit, np.nan))

        curve = self._plot_widget.plot(x_fit, y_plot, pen=pen, name=f"fit: {label}")
        self._fit_curves[label] = curve

        # Thin vertical line at x₀ — avoids text overlap when multiple datasets
        vline = pg.InfiniteLine(
            pos=float(info["x0"]), angle=90,
            pen=pg.mkPen(color=color, width=1, style=Qt.PenStyle.DashLine),
        )
        self._plot_widget.addItem(vline)
        self._fit_texts.append(vline)

    def _fit_peak(self):
        """Fit a peak or step to the currently plotted data."""
        if not _peak_fit.LMFIT_AVAILABLE:
            QMessageBox.warning(
                self, "Missing dependency",
                "lmfit is required for curve fitting.\n\n  pip install lmfit"
            )
            return
        if not self._run_data_list:
            QMessageBox.warning(self, "No data", "Load run data first.")
            return

        stream     = self._stream_combo.currentText()
        x_field    = self._x_combo.currentData() or self._x_combo.currentText()
        y_fields   = [
            self._y_list.item(i).text()
            for i in range(self._y_list.count())
            if self._y_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        norm_field = self._norm_combo.currentData()
        log_y      = self._log_y_cb.isChecked()
        model_name = self._fit_model_combo.currentText()

        if not y_fields:
            QMessageBox.warning(self, "No Y signal", "Check at least one Y signal.")
            return

        # Build datasets list
        combine = False
        if len(self._run_data_list) > 1:
            dlg = _MultiRunFitDialog(parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            combine = dlg.combine

        datasets = []   # (x, y, label)
        if combine:
            for y_field in y_fields:
                xs, ys = [], []
                for rd in self._run_data_list:
                    sdata = rd["streams"].get(stream, {})
                    x_arr, y_arr = self._get_xy_for_fit(sdata, x_field, y_field, norm_field)
                    if x_arr is not None and len(x_arr):
                        xs.append(x_arr); ys.append(y_arr)
                if xs:
                    x_all = np.concatenate(xs); y_all = np.concatenate(ys)
                    order = np.argsort(x_all)
                    datasets.append((x_all[order], y_all[order], f"Combined / {y_field}"))
        else:
            for rd in self._run_data_list:
                sdata     = rd["streams"].get(stream, {})
                run_label = rd.get("label", "Run")
                for y_field in y_fields:
                    x_arr, y_arr = self._get_xy_for_fit(sdata, x_field, y_field, norm_field)
                    if x_arr is not None and len(x_arr):
                        lbl = f"{run_label} / {y_field}" if len(self._run_data_list) > 1 else y_field
                        datasets.append((x_arr, y_arr, lbl))

        if not datasets:
            QMessageBox.warning(self, "No data", "No plottable data found.")
            return

        self._fit_datasets = datasets   # stored for _on_fit_applied
        self._fit_log_y    = log_y       # stored for _on_fit_applied

        # Close any existing fit dialog before opening a new one
        if self._fit_dlg is not None:
            try:
                self._fit_dlg.close()
            except Exception:
                pass

        initial_bg_name = self._fit_bg_combo.currentText()
        initial_params  = None
        if self._saved_fit_state:
            model_name      = self._saved_fit_state.get("model_name", model_name)
            initial_bg_name = self._saved_fit_state.get("bg_name", initial_bg_name)
            initial_params  = self._saved_fit_state.get("params")

        self._fit_dlg = _FitParamsDialog(
            datasets, model_name, initial_bg_name, initial_params, parent=self
        )
        self._fit_dlg.preview_changed.connect(self._on_fit_preview)
        self._fit_dlg.fit_applied.connect(self._on_fit_applied)
        self._fit_dlg.rejected.connect(self._on_fit_cancelled)
        self._fit_dlg.show()
        self._fit_dlg.raise_()
        self._fit_dlg.activateWindow()

    def _on_fit_preview(self, x_fit, y_fit):
        """Draw or update the dotted preview curve on the main plot."""
        if not PG_AVAILABLE or self._plot_widget is None:
            return
        try:
            log_y = getattr(self, "_fit_log_y", False)
            y_plot = y_fit.copy()
            if log_y:
                with np.errstate(divide="ignore", invalid="ignore"):
                    y_plot = np.log10(np.where(y_fit > 0, y_fit, np.nan))
            if self._fit_preview_curve is None:
                pen = pg.mkPen("#ffcc44", width=2, style=Qt.PenStyle.DotLine)
                self._fit_preview_curve = self._plot_widget.plot(x_fit, y_plot, pen=pen)
            else:
                self._fit_preview_curve.setData(x_fit, y_plot)
        except Exception:
            pass

    def _on_fit_applied(self, fit_items):
        """Remove preview, draw permanent overlays for all datasets, save state."""
        self._clear_fit_preview()
        self._clear_fit_overlays()
        log_y = getattr(self, "_fit_log_y", False)
        for idx, item in enumerate(fit_items):
            self._add_fit_overlay(
                item["x_fit"], item["y_fit"], item["info"], item["label"], log_y, idx
            )
        if fit_items:
            self._btn_clear_fit.setEnabled(True)
            info0 = fit_items[0]["info"]
            self._saved_fit_state = {
                "model_name": fit_items[0]["model_name"],
                "bg_name":    fit_items[0]["bg_name"],
                "params":     info0["result"].params,
            }

    def _on_fit_cancelled(self):
        """Remove the preview curve when the fit dialog is cancelled."""
        self._clear_fit_preview()

    # ── Double-click: move motor ───────────────────────────────────────────────

    def _on_plot_clicked(self, event):
        if not event.double():
            return
        pos = event.scenePos()
        if not self._plot_widget.sceneBoundingRect().contains(pos):
            return

        vb     = self._plot_widget.getPlotItem().vb
        mp     = vb.mapSceneToView(pos)
        x_val  = mp.x()

        x_field = self._x_combo.currentData() or self._x_combo.currentText()
        if x_field in ("time", "seq_num", None, ""):
            return   # time/sequence axes are not motor positions

        # Strip common readback suffixes to get the motor name
        motor_guess = x_field
        for suffix in ("_user_readback", "_readback", "_user_setpoint", "_setpoint"):
            if motor_guess.endswith(suffix):
                motor_guess = motor_guess[: -len(suffix)]
                break

        # Show last known position from the run data as context
        last_pos = None
        if self._run_data_list:
            stream = self._stream_combo.currentText()
            sdata  = self._run_data_list[-1]["streams"].get(stream, {})
            arr    = sdata.get(x_field)
            if arr is not None and len(arr):
                last_pos = float(arr[-1])

        msg = f"Move  '{motor_guess}'  to  {x_val:.6g}"
        if last_pos is not None:
            msg += f"\n\nLast scan end position: {last_pos:.6g}"
        msg += f"\n\n(X-axis signal: {x_field})"

        r = QMessageBox.question(
            self, "Move Motor", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self.move_requested.emit(motor_guess, x_val)

    # ── Screenshot ─────────────────────────────────────────────────────────────

    def _save_screenshot(self):
        if not PG_AVAILABLE or self._plot_widget is None:
            return
        QApplication.clipboard().setPixmap(self._plot_widget.grab())
        self._set_status("✓ Plot copied to clipboard — paste into any document")

    # ── HDF5 export ────────────────────────────────────────────────────────────

    def _export_hdf5(self):
        if not H5PY_AVAILABLE:
            QMessageBox.warning(
                self, "h5py Missing", "Install h5py first:\n  pip install h5py"
            )
            return

        rows = self._run_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(
                self, "No Selection", "Select at least one run to export."
            )
            return

        selected_runs = [
            self._runs[idx.row()]
            for idx in rows
            if idx.row() < len(self._runs)
        ]
        if not selected_runs:
            return

        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if not db:
            QMessageBox.warning(self, "No MongoDB", "No MongoDB database configured.")
            return

        if self._active_exp_dir and not self._show_all_cb.isChecked():
            exp_name     = Path(self._active_exp_dir).name
            default_path = str(Path(self._active_exp_dir) / f"{exp_name}.h5")
        else:
            default_path = "runs_export.h5"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Selected Runs to HDF5", default_path,
            "HDF5 Files (*.h5 *.hdf5)"
        )
        if not path:
            return

        if self._hdf5_exporter and self._hdf5_exporter.isRunning():
            QMessageBox.warning(self, "Busy", "An export is already in progress.")
            return

        self._btn_export_hdf5.setEnabled(False)
        self._set_status(
            f"Exporting {len(selected_runs)} run(s) to HDF5…", busy=True
        )
        self._hdf5_exporter = _HDF5Exporter(
            host, int(port), db, selected_runs, path, parent=self
        )
        self._hdf5_exporter.progress.connect(self._on_export_progress)
        self._hdf5_exporter.done.connect(self._on_export_done)
        self._hdf5_exporter.error.connect(self._on_export_error)
        self._hdf5_exporter.start()

    def _on_export_progress(self, done: int, total: int):
        self._set_status(f"Exporting… {done}/{total}", busy=True)

    def _on_export_done(self, path: str):
        self._btn_export_hdf5.setEnabled(H5PY_AVAILABLE)
        n = len(self._run_table.selectionModel().selectedRows())
        self._set_status(f"✓ Exported {n} run(s) → {Path(path).name}")
        QMessageBox.information(
            self, "Export Complete",
            f"Exported {n} run(s) to:\n{path}"
        )

    def _on_export_error(self, msg: str):
        self._btn_export_hdf5.setEnabled(H5PY_AVAILABLE)
        self._set_status(f"Export failed: {msg}", error=True)
        QMessageBox.critical(self, "Export Failed", msg)

    # ── Experiment export (all displayed runs) ─────────────────────────────────

    def _export_experiment_hdf5(self):
        if not H5PY_AVAILABLE:
            QMessageBox.warning(
                self, "h5py Missing", "Install h5py first:\n  pip install h5py"
            )
            return

        if not self._runs:
            QMessageBox.warning(self, "No Runs", "No runs to export.")
            return

        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if not db:
            QMessageBox.warning(self, "No MongoDB", "No MongoDB database configured.")
            return

        if self._active_exp_dir and not self._show_all_cb.isChecked():
            exp_name     = Path(self._active_exp_dir).name
            default_path = str(Path(self._active_exp_dir) / f"{exp_name}.h5")
        else:
            default_path = "runs_export.h5"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export All Displayed Runs to HDF5", default_path,
            "HDF5 Files (*.h5 *.hdf5)"
        )
        if not path:
            return

        if self._hdf5_exporter and self._hdf5_exporter.isRunning():
            QMessageBox.warning(self, "Busy", "An export is already in progress.")
            return

        self._btn_export_exp.setEnabled(False)
        self._set_status(
            f"Exporting {len(self._runs)} run(s) to HDF5…", busy=True
        )
        self._hdf5_exporter = _HDF5Exporter(
            host, int(port), db, self._runs, path, parent=self
        )
        self._hdf5_exporter.progress.connect(self._on_exp_export_progress)
        self._hdf5_exporter.done.connect(self._on_exp_export_done)
        self._hdf5_exporter.error.connect(self._on_exp_export_error)
        self._hdf5_exporter.start()

    def _on_exp_export_progress(self, done: int, total: int):
        self._set_status(f"Exporting… {done}/{total}", busy=True)

    def _on_exp_export_done(self, path: str):
        self._btn_export_exp.setEnabled(H5PY_AVAILABLE)
        n = len(self._runs)
        self._set_status(f"✓ Exported {n} run(s) → {Path(path).name}")
        QMessageBox.information(
            self, "Export Complete",
            f"Exported {n} run(s) to:\n{path}"
        )

    def _on_exp_export_error(self, msg: str):
        self._btn_export_exp.setEnabled(H5PY_AVAILABLE)
        self._set_status(f"Export failed: {msg}", error=True)
        QMessageBox.critical(self, "Export Failed", msg)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, error: bool = False, busy: bool = False):
        self._status_label.setText(msg)
        if error:
            self._status_label.setStyleSheet("color: #d62728;")
        elif busy:
            self._status_label.setStyleSheet("color: #ff7f0e;")
        else:
            self._status_label.setStyleSheet("color: #888888;")

    def closeEvent(self, event):
        if self._crosshair_cleanup:
            self._crosshair_cleanup()
        super().closeEvent(event)


class _MultiRunFitDialog(QDialog):
    """Ask whether to fit each run individually or combine all into one dataset."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fit — Multiple Runs")
        self.combine = False
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Multiple runs are selected.\n"
            "How would you like to fit the data?"
        ))
        btn_row = QHBoxLayout()
        btn_indiv = QPushButton("Fit Individually")
        btn_comb  = QPushButton("Combine All Runs")
        btn_cancel = QPushButton("Cancel")
        btn_indiv.clicked.connect(self._individual)
        btn_comb.clicked.connect(self._combine)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_indiv)
        btn_row.addWidget(btn_comb)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

    def _individual(self):
        self.combine = False
        self.accept()

    def _combine(self):
        self.combine = True
        self.accept()


class _PeakFitReportDialog(QDialog):
    """Non-blocking dialog showing detailed peak-fit results."""

    def __init__(self, results: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Peak Fit Report")
        self.setMinimumSize(520, 380)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)

        txt = QTextEdit()
        txt.setReadOnly(True)
        from PyQt6.QtGui import QFont
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(11)
        txt.setFont(mono)

        lines = []
        sep = "─" * 52
        for i, (label, info) in enumerate(results):
            if i:
                lines.append(sep)
            lines.append(f"Dataset  :  {label}")
            lines.append(f"Model    :  {info['model']}")
            lines.append(f"Points   :  {info['n_points']}")
            lines.append("")
            for name, val, err in zip(
                info["param_names"], info["params"], info["perr"]
            ):
                lines.append(f"  {name:<26} {val:>14.6g}  ±  {err:.4g}")
            fwhm_val = info.get("fwhm", float("nan"))
            _nan = isinstance(fwhm_val, float) and (fwhm_val != fwhm_val)
            if not _nan:
                _is_step = info.get("model", "").startswith("Step")
                _wlbl    = "10–90% width" if _is_step else "FWHM"
                lines.append(f"  {_wlbl:<26} {fwhm_val:>14.6g}")
            lines.append(f"  {'R²':<26} {info['r2']:>14.6f}")
            lines.append("")

        txt.setPlainText("\n".join(lines))
        lay.addWidget(txt)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("Copy to Clipboard")
        btn_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(txt.toPlainText())
        )
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_copy)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)


def _vline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    return f
