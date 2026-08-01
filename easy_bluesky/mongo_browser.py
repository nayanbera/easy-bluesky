"""mongo_browser.py — Browse and plot bluesky runs stored in per-profile MongoDB."""

from datetime import datetime

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


# ── Background workers ─────────────────────────────────────────────────────────

class _RunListFetcher(QThread):
    """Fetch the most recent N runs from a MongoDB database."""
    runs_ready = pyqtSignal(list)   # list of {start, stop} dicts
    error      = pyqtSignal(str)

    def __init__(self, host, port, db_name, limit=100, parent=None):
        super().__init__(parent)
        self._host  = host
        self._port  = port
        self._db    = db_name
        self._limit = limit

    def run(self):
        try:
            import pymongo
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db = client[self._db]
            starts = list(
                db["run_start"].find({}, {
                    "uid": 1, "scan_id": 1, "plan_name": 1,
                    "time": 1, "motors": 1, "detectors": 1, "hints": 1,
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


class _DataFetcher(QThread):
    """Fetch event data for one run; returns dict keyed by stream name."""
    data_ready = pyqtSignal(dict)   # {stream: {field: ndarray, 'time': ndarray}}
    error      = pyqtSignal(str)

    def __init__(self, host, port, db_name, run_uid, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._db   = db_name
        self._uid  = run_uid

    def run(self):
        try:
            import numpy as np
            import pymongo
            client = pymongo.MongoClient(
                self._host, self._port, serverSelectionTimeoutMS=5000
            )
            db    = client[self._db]
            descs = list(db["event_descriptor"].find({"run_start": self._uid}))
            result = {}

            for desc in descs:
                stream    = desc.get("name", "primary")
                desc_uid  = desc["uid"]
                data_keys = desc.get("data_keys", {})
                if not data_keys:
                    continue

                times     = []
                field_data = {k: [] for k in data_keys}

                # Try event_page first (suitcase.mongo_normalized format), then event
                pages = list(
                    db["event_page"].find({"descriptor": desc_uid})
                )
                if pages:
                    pages.sort(key=lambda p: (p.get("seq_num") or [0])[0])
                    for page in pages:
                        times.extend(page.get("time", []))
                        pdata = page.get("data", {})
                        for field in data_keys:
                            field_data[field].extend(pdata.get(field, []))
                else:
                    # Fall back to individual event documents
                    for ev in db["event"].find(
                        {"descriptor": desc_uid}
                    ).sort("seq_num", 1):
                        times.append(ev.get("time", 0))
                        edata = ev.get("data", {})
                        for field in data_keys:
                            field_data[field].append(edata.get(field))

                if not times:
                    continue

                stream_dict = {
                    "time":      np.array(times, dtype=float),
                    "data_keys": data_keys,
                }
                for field, vals in field_data.items():
                    try:
                        stream_dict[field] = np.array(vals, dtype=float)
                    except (TypeError, ValueError):
                        stream_dict[field] = np.array(vals)

                result[stream] = stream_dict

            client.close()
            self.data_ready.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Matplotlib canvas (lazy — skips gracefully if not installed) ────────────────

def _make_canvas():
    """Return (canvas, toolbar_class) or raise ImportError."""
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg as _Canvas,
        NavigationToolbar2QT as _Toolbar,
    )
    from matplotlib.figure import Figure
    fig    = Figure(tight_layout=True)
    canvas = _Canvas(fig)
    return canvas, fig, _Toolbar


# ── Main tab ───────────────────────────────────────────────────────────────────

class MongoDataBrowserTab(QWidget):
    """
    Browse bluesky runs stored in per-profile MongoDB databases and plot them.

    The profile selector defaults to the currently active profile; the user can
    switch to any other profile that has a mongo_db configured.
    """

    # ── setup ──────────────────────────────────────────────────────────────────

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings    = settings   # shared reference — updates propagate
        self._runs: list  = []         # cached run list for selected profile
        self._stream_data = {}         # fetched stream data for selected run
        self._run_fetcher = None
        self._data_fetcher = None
        self._canvas      = None
        self._fig         = None
        self._toolbar_cls = None

        self._build_ui()
        self._try_make_canvas()
        self._populate_profile_combo()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Profile / DB bar ────────────────────────────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("Profile:"))

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(160)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        top.addWidget(self._profile_combo)

        self._db_label = QLabel("")
        self._db_label.setObjectName("dim_text")
        top.addWidget(self._db_label, 1)

        self._load_spin = QLabel("")
        top.addWidget(self._load_spin)

        btn_refresh = QPushButton("↻  Refresh Runs")
        btn_refresh.setFixedHeight(28)
        btn_refresh.clicked.connect(self._fetch_runs)
        top.addWidget(btn_refresh)

        root.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # ── Main splitter ────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left panel: run list + info ──────────────────────────────────────
        left = QWidget()
        left.setMinimumWidth(320)
        left.setMaximumWidth(480)
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
        self._run_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._run_table.verticalHeader().setVisible(False)
        self._run_table.setAlternatingRowColors(True)
        self._run_table.itemSelectionChanged.connect(self._on_run_selected)
        llayout.addWidget(self._run_table, 1)

        # Run info box
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

        # ── Right panel: axis controls + plot ────────────────────────────────
        right = QWidget()
        rlayout = QVBoxLayout(right)
        rlayout.setContentsMargins(0, 0, 0, 0)
        rlayout.setSpacing(4)

        # Axis controls
        ctrl_box = QGroupBox("Axis / Signals")
        ctrl_lay = QHBoxLayout(ctrl_box)
        ctrl_lay.setSpacing(12)

        # Stream selector
        stream_col = QVBoxLayout()
        stream_col.addWidget(QLabel("Stream:"))
        self._stream_combo = QComboBox()
        self._stream_combo.setMinimumWidth(100)
        self._stream_combo.currentIndexChanged.connect(self._on_stream_changed)
        stream_col.addWidget(self._stream_combo)
        stream_col.addStretch()
        ctrl_lay.addLayout(stream_col)

        sep_v = QFrame()
        sep_v.setFrameShape(QFrame.Shape.VLine)
        ctrl_lay.addWidget(sep_v)

        # X axis
        x_col = QVBoxLayout()
        x_col.addWidget(QLabel("X axis:"))
        self._x_combo = QComboBox()
        self._x_combo.setMinimumWidth(130)
        x_col.addWidget(self._x_combo)
        x_col.addStretch()
        ctrl_lay.addLayout(x_col)

        sep_v2 = QFrame()
        sep_v2.setFrameShape(QFrame.Shape.VLine)
        ctrl_lay.addWidget(sep_v2)

        # Y axis (scrollable checklist)
        y_col = QVBoxLayout()
        y_col.addWidget(QLabel("Y signals (select one or more):"))
        self._y_list = QListWidget()
        self._y_list.setMinimumHeight(80)
        self._y_list.setMaximumHeight(120)
        self._y_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        y_col.addWidget(self._y_list)
        ctrl_lay.addLayout(y_col, 1)

        sep_v3 = QFrame()
        sep_v3.setFrameShape(QFrame.Shape.VLine)
        ctrl_lay.addWidget(sep_v3)

        # Options + plot button
        opt_col = QVBoxLayout()
        self._norm_cb = QCheckBox("Normalise to first point")
        opt_col.addWidget(self._norm_cb)
        self._log_y_cb = QCheckBox("Log Y scale")
        opt_col.addWidget(self._log_y_cb)
        self._overlay_cb = QCheckBox("Overlay (don't clear)")
        opt_col.addWidget(self._overlay_cb)
        opt_col.addStretch()
        btn_plot = QPushButton("Plot")
        btn_plot.setFixedHeight(32)
        btn_plot.setDefault(True)
        btn_plot.clicked.connect(self._plot)
        opt_col.addWidget(btn_plot)
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedHeight(28)
        btn_clear.clicked.connect(self._clear_plot)
        opt_col.addWidget(btn_clear)
        ctrl_lay.addLayout(opt_col)

        rlayout.addWidget(ctrl_box)

        # Plot area placeholder — filled by _try_make_canvas()
        self._plot_placeholder = QLabel(
            "matplotlib not installed.\n"
            "Run:  pip install matplotlib"
        )
        self._plot_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plot_placeholder.setStyleSheet("color: #888;")
        rlayout.addWidget(self._plot_placeholder, 1)
        self._plot_area_layout = rlayout   # save ref to attach canvas later

        splitter.addWidget(right)
        splitter.setSizes([360, 700])
        root.addWidget(splitter, 1)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

    def _try_make_canvas(self):
        try:
            self._canvas, self._fig, self._toolbar_cls = _make_canvas()
            self._plot_placeholder.hide()
            self._plot_area_layout.addWidget(self._canvas, 1)
            toolbar = self._toolbar_cls(self._canvas, self)
            self._plot_area_layout.addWidget(toolbar)
        except ImportError:
            pass   # placeholder stays visible

    # ── Profile management ─────────────────────────────────────────────────────

    def _populate_profile_combo(self):
        """Fill the profile combo with profiles that have mongo_db set."""
        active = self._settings.get("active_profile", "")
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
        """Call when the active profile or settings change."""
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
            self._runs = []
            self._run_table.setRowCount(0)
            self._clear_axis_controls()
            self._fetch_runs()
        else:
            self._db_label.setText("  (no MongoDB configured)")

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

        self._set_status("Fetching runs…", busy=True)
        self._run_fetcher = _RunListFetcher(host, int(port), db, limit=200, parent=self)
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

            scan_id   = str(start.get("scan_id", "—"))
            plan      = start.get("plan_name", "—")
            ts        = start.get("time", 0)
            dt_str    = datetime.fromtimestamp(ts).strftime("%Y-%m-%d  %H:%M:%S") if ts else "—"
            exit_st   = stop.get("exit_status", "running" if not stop else "—")
            num_ev    = str(stop.get("num_events", {}).get("primary", "—")) if stop else "—"
            dets      = ", ".join(start.get("detectors", [])) or "—"

            # colour-code status
            if exit_st == "success":
                status_color = "#2ca02c"
                status_icon  = "✓ success"
            elif exit_st in ("fail", "error"):
                status_color = "#d62728"
                status_icon  = "✗ fail"
            elif exit_st == "abort":
                status_color = "#ff7f0e"
                status_icon  = "⊘ abort"
            else:
                status_color = "#888888"
                status_icon  = "… running"

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

        n = len(runs)
        self._set_status(f"{n} run{'s' if n != 1 else ''} loaded.")

    def _on_fetch_error(self, msg: str):
        self._set_status(f"Error: {msg}", error=True)

    # ── Run selection ──────────────────────────────────────────────────────────

    def _on_run_selected(self):
        rows = self._run_table.selectionModel().selectedRows()
        if not rows:
            return
        row   = rows[0].row()
        if row >= len(self._runs):
            return

        run   = self._runs[row]
        start = run["start"]
        stop  = run["stop"]

        # Update info panel
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

        # Fetch data
        profile = self._current_profile()
        db   = profile.get("mongo_db",   "")
        host = profile.get("mongo_host", "") or "localhost"
        port = profile.get("mongo_port", 27017)
        if not db or not start.get("uid"):
            return

        if self._data_fetcher and self._data_fetcher.isRunning():
            self._data_fetcher.terminate()

        self._set_status("Loading run data…", busy=True)
        self._data_fetcher = _DataFetcher(
            host, int(port), db, start["uid"], parent=self
        )
        self._data_fetcher.data_ready.connect(self._on_data_ready)
        self._data_fetcher.error.connect(self._on_fetch_error)
        self._data_fetcher.start()

    def _on_data_ready(self, data: dict):
        self._stream_data = data
        self._populate_axis_controls(data)
        n_pts = {
            k: len(v.get("time", []))
            for k, v in data.items()
        }
        summary = ",  ".join(f"{k}: {v} pts" for k, v in n_pts.items())
        self._set_status(f"Data loaded — {summary}")

    # ── Axis controls ──────────────────────────────────────────────────────────

    def _clear_axis_controls(self):
        self._stream_combo.blockSignals(True)
        self._stream_combo.clear()
        self._stream_combo.blockSignals(False)
        self._x_combo.clear()
        self._y_list.clear()
        self._stream_data = {}

    def _populate_axis_controls(self, data: dict):
        self._stream_combo.blockSignals(True)
        self._stream_combo.clear()
        for stream in data:
            self._stream_combo.addItem(stream)
        # default to "primary"
        idx = self._stream_combo.findText("primary")
        if idx >= 0:
            self._stream_combo.setCurrentIndex(idx)
        self._stream_combo.blockSignals(False)
        self._update_field_lists()

    def _on_stream_changed(self, _idx: int):
        self._update_field_lists()

    def _update_field_lists(self):
        stream = self._stream_combo.currentText()
        sdata  = self._stream_data.get(stream, {})
        keys   = [k for k in sdata if k not in ("time", "data_keys")]

        # ── X axis ────────────────────────────────────────────────────────────
        self._x_combo.clear()
        self._x_combo.addItem("time (epoch)", userData="time")
        self._x_combo.addItem("sequence #",   userData="seq_num")

        # Auto-detect motor from run metadata
        rows = self._run_table.selectionModel().selectedRows()
        auto_motor = ""
        if rows and rows[0].row() < len(self._runs):
            start = self._runs[rows[0].row()]["start"]
            motors = start.get("motors", [])
            if motors:
                auto_motor = motors[0]
                # Find matching key (ophyd uses device_attr naming)
                for k in keys:
                    if k == auto_motor or k.startswith(auto_motor):
                        auto_motor = k
                        break

            # Also try run_start.hints.dimensions
            dims = start.get("hints", {}).get("dimensions", [])
            if dims:
                dim_fields = dims[0][0] if dims[0] else []
                for f in dim_fields:
                    if f in keys:
                        auto_motor = f
                        break

        for k in sorted(keys):
            self._x_combo.addItem(k, userData=k)

        # Select motor as default X
        if auto_motor:
            for i in range(self._x_combo.count()):
                if self._x_combo.itemData(i) == auto_motor:
                    self._x_combo.setCurrentIndex(i)
                    break

        # ── Y axis (checkboxes) ────────────────────────────────────────────────
        self._y_list.clear()
        motor_names = set()
        if rows and rows[0].row() < len(self._runs):
            motor_names = set(self._runs[rows[0].row()]["start"].get("motors", []))

        for k in sorted(keys):
            item = QListWidgetItem(k)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # Pre-check detectors; uncheck motors
            is_motor = any(k == m or k.startswith(m) for m in motor_names)
            item.setCheckState(
                Qt.CheckState.Unchecked if is_motor else Qt.CheckState.Checked
            )
            self._y_list.addItem(item)

    # ── Plotting ───────────────────────────────────────────────────────────────

    def _plot(self):
        if self._canvas is None:
            return

        stream = self._stream_combo.currentText()
        sdata  = self._stream_data.get(stream)
        if not sdata:
            self._set_status("No data — select a run first.", error=True)
            return

        x_field = self._x_combo.currentData() or self._x_combo.currentText()
        y_fields = []
        for i in range(self._y_list.count()):
            item = self._y_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                y_fields.append(item.text())

        if not y_fields:
            self._set_status("Select at least one Y signal.", error=True)
            return

        # Get X array
        if x_field == "time":
            x_raw = sdata.get("time")
            if x_raw is not None and len(x_raw):
                import numpy as np
                x_arr  = x_raw - x_raw[0]   # relative seconds
                x_label = "Time  (s)"
            else:
                self._set_status("No time data.", error=True)
                return
        elif x_field == "seq_num":
            import numpy as np
            t = sdata.get("time")
            x_arr  = np.arange(1, len(t) + 1) if t is not None else np.array([])
            x_label = "Sequence #"
        else:
            x_arr  = sdata.get(x_field)
            x_label = x_field
            if x_arr is None:
                self._set_status(f"Field '{x_field}' not found.", error=True)
                return

        # Draw
        if not self._overlay_cb.isChecked():
            self._fig.clear()

        ax = self._fig.gca() if self._fig.axes else self._fig.add_subplot(111)

        import numpy as np
        normalise = self._norm_cb.isChecked()
        log_y     = self._log_y_cb.isChecked()

        for field in y_fields:
            y_arr = sdata.get(field)
            if y_arr is None or len(y_arr) == 0:
                continue
            # Align lengths
            n = min(len(x_arr), len(y_arr))
            x = x_arr[:n]
            y = y_arr[:n]
            if normalise and y[0] != 0:
                y = y / y[0]
            ax.plot(x, y, marker=".", markersize=3, label=field)

        ax.set_xlabel(x_label)
        y_unit = ", ".join(y_fields) if len(y_fields) <= 3 else f"{len(y_fields)} signals"
        if normalise:
            y_unit += "  (normalised)"
        ax.set_ylabel(y_unit)
        if log_y:
            ax.set_yscale("log")
        ax.legend(loc="best", fontsize=8)
        ax.grid(True, alpha=0.3)

        rows = self._run_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._runs):
            start   = self._runs[rows[0].row()]["start"]
            scan_id = start.get("scan_id", "")
            plan    = start.get("plan_name", "")
            ax.set_title(f"Scan {scan_id}  —  {plan}", fontsize=10)

        self._canvas.draw()

    def _clear_plot(self):
        if self._canvas is None:
            return
        self._fig.clear()
        self._canvas.draw()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, error: bool = False, busy: bool = False):
        self._status_label.setText(msg)
        if error:
            self._status_label.setStyleSheet("color: #d62728;")
        elif busy:
            self._status_label.setStyleSheet("color: #ff7f0e;")
        else:
            self._status_label.setStyleSheet("color: #888888;")
