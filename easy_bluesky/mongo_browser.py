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
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

try:
    import pyqtgraph.exporters  # noqa: F401 — ensure exporters are registered
except ImportError:
    pass

from .config import PLOT_COLORS


# ── Module-level helper ────────────────────────────────────────────────────────

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
    """Fetch the most recent N runs from MongoDB, optionally filtered by exp_dir."""
    runs_ready = pyqtSignal(list)
    error      = pyqtSignal(str)

    def __init__(self, host, port, db_name, limit=300, exp_dir_filter="", parent=None):
        super().__init__(parent)
        self._host    = host
        self._port    = port
        self._db      = db_name
        self._limit   = limit
        self._exp_dir = exp_dir_filter

    def run(self):
        try:
            import pymongo
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db = client[self._db]

            query = {}
            if self._exp_dir:
                query["exp_dir"] = self._exp_dir

            starts = list(
                db["run_start"].find(query, {
                    "uid": 1, "scan_id": 1, "plan_name": 1,
                    "time": 1, "motors": 1, "detectors": 1, "hints": 1,
                    "exp_dir": 1, "sample_name": 1,
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
        self._curves: dict   = {}
        self._active_exp_dir = ""       # current experiment filter
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

        # ── Right: axis controls + plot ───────────────────────────────────────
        right   = QWidget()
        rlayout = QVBoxLayout(right)
        rlayout.setContentsMargins(0, 0, 0, 0)
        rlayout.setSpacing(4)

        ctrl_box = QGroupBox("Axis / Signals")
        ctrl_lay = QHBoxLayout(ctrl_box)
        ctrl_lay.setSpacing(12)

        stream_col = QVBoxLayout()
        stream_col.addWidget(QLabel("Stream:"))
        self._stream_combo = QComboBox()
        self._stream_combo.setMinimumWidth(100)
        self._stream_combo.currentIndexChanged.connect(self._on_stream_changed)
        stream_col.addWidget(self._stream_combo)
        stream_col.addStretch()
        ctrl_lay.addLayout(stream_col)

        ctrl_lay.addWidget(_vline())

        x_col = QVBoxLayout()
        x_col.addWidget(QLabel("X axis:"))
        self._x_combo = QComboBox()
        self._x_combo.setMinimumWidth(130)
        self._x_combo.currentIndexChanged.connect(self._auto_plot)
        x_col.addWidget(self._x_combo)
        x_col.addStretch()
        ctrl_lay.addLayout(x_col)

        ctrl_lay.addWidget(_vline())

        y_col = QVBoxLayout()
        y_col.addWidget(QLabel("Y signals:"))
        self._y_list = QListWidget()
        self._y_list.setMinimumHeight(80)
        self._y_list.setMaximumHeight(120)
        self._y_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._y_list.itemChanged.connect(self._auto_plot)
        y_col.addWidget(self._y_list)
        ctrl_lay.addLayout(y_col, 1)

        ctrl_lay.addWidget(_vline())

        norm_col = QVBoxLayout()
        norm_col.addWidget(QLabel("Norm by:"))
        self._norm_combo = QComboBox()
        self._norm_combo.setMinimumWidth(120)
        self._norm_combo.addItem("None", userData=None)
        self._norm_combo.currentIndexChanged.connect(self._auto_plot)
        norm_col.addWidget(self._norm_combo)
        norm_col.addStretch()
        ctrl_lay.addLayout(norm_col)

        ctrl_lay.addWidget(_vline())

        opt_col = QVBoxLayout()
        self._log_y_cb = QCheckBox("Log Y")
        self._log_y_cb.stateChanged.connect(self._auto_plot)
        opt_col.addWidget(self._log_y_cb)
        opt_col.addStretch()

        btn_screenshot = QPushButton("Screenshot")
        btn_screenshot.setFixedHeight(28)
        btn_screenshot.setToolTip("Save the current plot as a PNG image")
        btn_screenshot.clicked.connect(self._save_screenshot)
        opt_col.addWidget(btn_screenshot)

        self._btn_export_hdf5 = QPushButton("Export HDF5…")
        self._btn_export_hdf5.setFixedHeight(28)
        self._btn_export_hdf5.setToolTip(
            "Export selected run(s) to an HDF5 file readable by the HDF5 Viewer tab"
        )
        self._btn_export_hdf5.clicked.connect(self._export_hdf5)
        if not H5PY_AVAILABLE:
            self._btn_export_hdf5.setEnabled(False)
            self._btn_export_hdf5.setToolTip("pip install h5py to enable HDF5 export")
        opt_col.addWidget(self._btn_export_hdf5)

        ctrl_lay.addLayout(opt_col)
        rlayout.addWidget(ctrl_box)

        if PG_AVAILABLE:
            self._plot_widget = pg.PlotWidget(background="#1e1e1e")
            self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self._plot_widget.addLegend()
            self._plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)
            rlayout.addWidget(self._plot_widget, 1)
        else:
            self._plot_widget = None
            rlayout.addWidget(
                QLabel("pyqtgraph not available — pip install pyqtgraph"), 1
            )

        splitter.addWidget(right)
        splitter.setSizes([400, 700])
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

    def _fetch_runs(self):
        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if not db:
            return
        if self._run_fetcher and self._run_fetcher.isRunning():
            return

        exp_filter = "" if self._show_all_cb.isChecked() else self._active_exp_dir
        self._set_status("Fetching runs…", busy=True)
        self._run_fetcher = _RunListFetcher(
            host, int(port), db, limit=300,
            exp_dir_filter=exp_filter, parent=self,
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

            scan_id = str(start.get("scan_id", "—"))
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

            cols = [scan_id, plan, dt_str, status_icon, num_ev, dets]
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

    def _update_info_single(self, row: int):
        if row >= len(self._runs):
            return
        run   = self._runs[row]
        start = run["start"]
        stop  = run["stop"]

        scan_id  = start.get("scan_id", "—")
        plan     = start.get("plan_name", "—")
        ts_start = start.get("time", 0)
        ts_stop  = stop.get("time", 0) if stop else 0
        dur      = f"{ts_stop - ts_start:.1f} s" if ts_start and ts_stop else "—"
        motors   = ", ".join(start.get("motors", [])) or "—"
        dets     = ", ".join(start.get("detectors", [])) or "—"
        uid      = start.get("uid", "")[:8]
        exit_st  = stop.get("exit_status", "running") if stop else "running"

        self._info_label.setText(
            f"Scan ID : {scan_id}\n"
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
            scan_id = start.get("scan_id", "?")
            plan    = start.get("plan_name", "?")
            uid_labels.append((uid, f"#{scan_id} {plan}"))

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

        if auto_motor:
            for i in range(self._x_combo.count()):
                if self._x_combo.itemData(i) == auto_motor:
                    self._x_combo.setCurrentIndex(i)
                    break

        # ── Y ─────────────────────────────────────────────────────────────────
        motor_names = set()
        if rows and rows[0].row() < len(self._runs):
            motor_names = set(self._runs[rows[0].row()]["start"].get("motors", []))

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

        for curve in self._curves.values():
            try:
                self._plot_widget.removeItem(curve)
            except Exception:
                pass
        pi = self._plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves = {}

        log_y     = self._log_y_cb.isChecked()
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
                x = x_arr[:n].astype(float)
                y = y_arr[:n].astype(float)

                if norm_arr is not None:
                    denom = norm_arr[:n]
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(denom != 0, y / denom, np.nan)

                if log_y:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.log10(np.where(y > 0, y, np.nan))

                mask = np.isfinite(x) & np.isfinite(y)
                x, y = x[mask], y[mask]
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
            scan_id = start.get("scan_id", "")
            plan    = start.get("plan_name", "")
            title   = f"Scan {scan_id}  —  {plan}"
            if len(rows) > 1:
                title += f"  (+{len(rows)-1} more)"
            self._plot_widget.setTitle(title)

    def _clear_plot(self):
        if self._plot_widget is None:
            return
        for curve in self._curves.values():
            try:
                self._plot_widget.removeItem(curve)
            except Exception:
                pass
        pi = self._plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves = {}
        self._plot_widget.setTitle("")

    def _auto_plot(self, *_args):
        """Re-plot whenever axis controls change — guard against no data."""
        if self._run_data_list:
            self._plot()

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
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plot Screenshot", "mongodb_plot.png",
            "PNG Images (*.png);;All Files (*)"
        )
        if not path:
            return
        try:
            exporter = pg.exporters.ImageExporter(self._plot_widget.plotItem)
            exporter.export(path)
            self._set_status(f"✓ Screenshot saved → {Path(path).name}")
        except Exception as exc:
            QMessageBox.warning(self, "Screenshot Failed", str(exc))

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

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, error: bool = False, busy: bool = False):
        self._status_label.setText(msg)
        if error:
            self._status_label.setStyleSheet("color: #d62728;")
        elif busy:
            self._status_label.setStyleSheet("color: #ff7f0e;")
        else:
            self._status_label.setStyleSheet("color: #888888;")


def _vline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    return f
