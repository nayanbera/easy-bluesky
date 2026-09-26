"""live_viewer.py — Live Viewer tab: ZMQ subscriber + pyqtgraph live plots."""

import json
import numpy as np

try:
    import zmq
    ZMQ_AVAILABLE = True
except ImportError:
    ZMQ_AVAILABLE = False

try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QMessageBox, QApplication, QSplitter, QSizePolicy, QStackedWidget, QTabBar,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from .config import PLOT_COLORS, ZMQ_DOC_ADDR
from .plot_tools import setup_crosshair, smart_legend_position, TwoDMapWidget
from . import peak_fit as _peak_fit


def _parse_motor_ranges(start_doc: dict) -> dict:
    """Extract {motor_name: (start, stop)} from a bluesky start document or
    a plan kwargs dict.

    Handles multiple storage formats:
    - Nested: plan_args["args"] = [[motor, start, stop, num], ...]  (grid_scan)
    - Flat:   plan_args["args"] = [motor, start, stop, num, motor, ...]
    - Top-level keys: plan_args["motor"] / plan_args["start"] / plan_args["stop"]
    Returns an empty dict when the structure is not recognised.
    """
    ranges = {}
    try:
        plan_args = start_doc.get("plan_args", {}) or {}
        args = plan_args.get("args", []) or []

        # Nested format: [[motor, start, stop, num], ...]
        if args and isinstance(args[0], (list, tuple)):
            for entry in args:
                if (len(entry) >= 3
                        and isinstance(entry[1], (int, float))
                        and isinstance(entry[2], (int, float))):
                    ranges[str(entry[0])] = (float(entry[1]), float(entry[2]))

        # Flat interleaved format: [motor, start, stop, num, motor, start, stop, num, ...]
        if not ranges and args and not isinstance(args[0], (list, tuple)):
            i = 0
            while i + 3 <= len(args):
                m, s, e = args[i], args[i + 1], args[i + 2]
                if isinstance(s, (int, float)) and isinstance(e, (int, float)):
                    ranges[str(m)] = (float(s), float(e))
                    # skip 4 elements (motor, start, stop, num)
                    i += 4
                else:
                    i += 1

        # scan / rel_scan top-level keys
        if not ranges:
            for key in ("motor", "motor1", "motor2"):
                m = plan_args.get(key)
                s = plan_args.get(key.replace("motor", "start"))
                e = plan_args.get(key.replace("motor", "stop"))
                if m and isinstance(s, (int, float)) and isinstance(e, (int, float)):
                    ranges[str(m)] = (float(s), float(e))
    except Exception:
        pass
    return ranges


def _poisson_sigma(y_raw, norm_raw=None):
    """Poisson √N error with propagation through y/norm normalization."""
    y = np.abs(y_raw)
    if norm_raw is None:
        return np.sqrt(y)
    n = np.abs(norm_raw)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n > 0, np.sqrt(y / n ** 2 + y ** 2 / n ** 3), np.nan)



class ZMQDocThread(QThread):
    """Background thread: receive bluesky documents from ZMQ PUB socket."""
    doc_received   = pyqtSignal(str, dict)
    status_changed = pyqtSignal(str)

    def __init__(self, addr=None, parent=None):
        super().__init__(parent)
        self._addr = addr or ZMQ_DOC_ADDR

    def run(self):
        if not ZMQ_AVAILABLE:
            self.status_changed.emit("pyzmq not installed")
            return

        ctx  = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.connect(self._addr)
        sock.subscribe(b"")
        sock.setsockopt(zmq.RCVTIMEO, 500)

        self.status_changed.emit(f"Listening on {self._addr}…")

        while not self.isInterruptionRequested():
            try:
                raw  = sock.recv_string()
                name, doc = json.loads(raw)
                self.doc_received.emit(name, doc)
            except zmq.error.Again:
                continue
            except Exception:
                pass

        sock.close()
        ctx.term()


class LiveViewer(QWidget):
    COLORS = PLOT_COLORS
    move_requested        = pyqtSignal(str, float)           # (motor_name, target_position) — 1D
    move_2d_requested     = pyqtSignal(str, float, str, float)  # (x_motor, x_val, y_motor, y_val)
    scan_point_completed  = pyqtSignal(int)          # seq_num of each event doc

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker    = worker
        self._data        = {}   # key → list of float values
        self._curves      = {}   # curve_name → PlotDataItem
        self._error_items = {}   # curve_name → pg.ErrorBarItem
        self._run_uid  = None
        self._x_signal = None
        self._saved_x: str  = ""    # X signal from the previous run (for restore)
        self._saved_y: list = []    # selected Y signals from the previous run
        self._start_motors:    list = []   # from start doc — used for X default
        self._start_detectors: list = []   # from start doc — used for Y default
        self._live_fit_curve = None          # dashed fit overlay (PlotDataItem)
        self._live_fit_n_fitted = 0          # point count at last live-fit call
        self._crosshair_cleanup = None
        self._map_mode    = False            # True when 2D map is active
        self._pending_2d  = False            # auto-switch when descriptor arrives
        self._all_cols:   list = []          # all signal columns from last descriptor
        self._start_motor_ranges: dict = {}  # motor_name → (min, max) from start doc
        self._build()
        self._start_zmq()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(130)
        self.x_combo.setMaximumWidth(240)
        self.x_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.x_combo.currentTextChanged.connect(self._on_x_changed)
        ctrl.addWidget(self.x_combo)

        ctrl.addWidget(QLabel("Norm by:"))
        self.norm_combo = QComboBox()
        self.norm_combo.setMinimumWidth(110)
        self.norm_combo.setMaximumWidth(220)
        self.norm_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.norm_combo.addItem("None", userData=None)
        self.norm_combo.currentIndexChanged.connect(self._update_plot)
        ctrl.addWidget(self.norm_combo)

        ctrl.addStretch()
        self.run_label = QLabel("No active run")
        self.run_label.setObjectName("dim_text")
        ctrl.addWidget(self.run_label)
        main.addLayout(ctrl)

        # ── Mode tabs: 1D Plot / 2D Map ────────────────────────────────────────
        self._mode_tabs = QTabBar()
        self._mode_tabs.setDocumentMode(True)

        # Tab 0 — 1D Plot controls
        _tab_1d = QWidget()
        _1d_bar = QHBoxLayout(_tab_1d)
        _1d_bar.setContentsMargins(4, 2, 4, 2)
        _1d_bar.setSpacing(4)

        self._err_cb = QCheckBox("± Errors")
        self._err_cb.setToolTip(
            "Overlay Poisson √N error bars (propagated through normalization)"
        )
        self._err_cb.stateChanged.connect(self._on_err_toggled)
        _1d_bar.addWidget(self._err_cb)

        btn_screenshot = QPushButton("Screenshot")
        btn_screenshot.setToolTip("Save the current plot as a PNG image")
        btn_screenshot.clicked.connect(self._save_screenshot)
        _1d_bar.addWidget(btn_screenshot)

        _1d_bar.addSpacing(12)
        self._live_fit_cb = QCheckBox("Live Fit:")
        self._live_fit_cb.setToolTip(
            "Fit a model to data as the scan runs (first selected Y signal).\n"
            "Final parameters appear in the plot title on run stop."
        )
        self._live_fit_cb.setEnabled(_peak_fit.LMFIT_AVAILABLE)
        if not _peak_fit.LMFIT_AVAILABLE:
            self._live_fit_cb.setToolTip("pip install lmfit to enable Live Fit")
        self._live_fit_cb.stateChanged.connect(self._on_live_fit_toggled)
        self._live_fit_cb.stateChanged.connect(lambda: self._run_live_fit(force=True))
        _1d_bar.addWidget(self._live_fit_cb)

        self._live_fit_model_combo = QComboBox()
        self._live_fit_model_combo.setFixedHeight(26)
        self._live_fit_model_combo.setMinimumWidth(110)
        self._live_fit_model_combo.setMaximumWidth(180)
        self._live_fit_model_combo.addItem("None")
        self._live_fit_model_combo.insertSeparator(
            self._live_fit_model_combo.count()
        )
        for m in _peak_fit.PEAK_MODELS:
            self._live_fit_model_combo.addItem(m)
        self._live_fit_model_combo.insertSeparator(
            self._live_fit_model_combo.count()
        )
        for m in _peak_fit.STEP_MODELS:
            self._live_fit_model_combo.addItem(m)
        self._live_fit_model_combo.setCurrentText(_peak_fit.PEAK_MODELS[0])
        self._live_fit_model_combo.currentTextChanged.connect(
            lambda: self._run_live_fit(force=True)
        )
        _1d_bar.addWidget(self._live_fit_model_combo)

        bg_lbl = QLabel("+ BG:")
        bg_lbl.setStyleSheet("font-size: 11px;")
        _1d_bar.addWidget(bg_lbl)

        self._live_fit_bg_combo = QComboBox()
        self._live_fit_bg_combo.setFixedHeight(26)
        self._live_fit_bg_combo.setMinimumWidth(80)
        self._live_fit_bg_combo.setMaximumWidth(120)
        for bg in _peak_fit.BACKGROUND_MODELS:
            self._live_fit_bg_combo.addItem(bg)
        self._live_fit_bg_combo.currentTextChanged.connect(
            lambda: self._run_live_fit(force=True)
        )
        _1d_bar.addWidget(self._live_fit_bg_combo)
        _1d_bar.addStretch()

        self._mode_tabs.addTab("1D Plot")
        self._mode_tabs.addTab("2D Map")
        self._mode_tabs.currentChanged.connect(self._on_mode_tab_changed)
        main.addWidget(self._mode_tabs)
        self._1d_controls = _tab_1d
        main.addWidget(self._1d_controls)

        # Y list on the right of the plot (in a resizable splitter)
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMinimumWidth(100)
        self.y_list.setToolTip("Y signals — click to select/deselect")
        self.y_list.itemSelectionChanged.connect(self._update_plot)
        self.y_list.itemSelectionChanged.connect(lambda: self._run_live_fit(force=True))
        y_panel = QVBoxLayout()
        y_panel.setSpacing(2)
        y_panel.setContentsMargins(4, 0, 0, 0)
        y_lbl = QLabel("Y signals")
        y_lbl.setObjectName("dim_text")
        y_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        y_panel.addWidget(y_lbl)
        y_panel.addWidget(self.y_list, 1)
        y_container = QWidget()
        y_container.setLayout(y_panel)

        if PYQTGRAPH_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)
            plot_area = self.plot_widget
        else:
            plot_area = QLabel("pyqtgraph not available — pip install pyqtgraph")

        plot_splitter = QSplitter(Qt.Orientation.Horizontal)
        plot_splitter.addWidget(plot_area)
        plot_splitter.addWidget(y_container)
        plot_splitter.setSizes([900, 180])
        plot_splitter.setStretchFactor(0, 1)
        plot_splitter.setStretchFactor(1, 0)

        self._2d_widget = TwoDMapWidget(parent=self)
        self._2d_widget.selection_changed.connect(self._update_2d_plot)
        self._2d_widget.move_2d_requested.connect(self.move_2d_requested)

        self._plot_stack = QStackedWidget()
        self._plot_stack.addWidget(plot_splitter)   # index 0 → 1D
        self._plot_stack.addWidget(self._2d_widget) # index 1 → 2D
        main.addWidget(self._plot_stack, 1)

        # Bottom bar: status left, cursor coords right
        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        self.status_bar = QLabel("Waiting for run…")
        self.status_bar.setObjectName("dim_text")
        self.status_bar.setStyleSheet("font-size: 12px; padding: 4px;")
        bot.addWidget(self.status_bar, 1)

        self._log_y_cb = QCheckBox("Log Y")
        self._log_y_cb.stateChanged.connect(self._update_plot)
        bot.addWidget(self._log_y_cb)

        bot.addWidget(QLabel("Deriv:"))
        self._deriv_combo = QComboBox()
        self._deriv_combo.addItems(["—", "dy/dx", "d²y/dx²"])
        self._deriv_combo.setFixedHeight(22)
        self._deriv_combo.setFixedWidth(90)
        self._deriv_combo.currentIndexChanged.connect(self._update_plot)
        bot.addWidget(self._deriv_combo)

        self.coord_label = QLabel("")
        self.coord_label.setObjectName("dim_text")
        self.coord_label.setStyleSheet("font-size: 11px; padding: 4px; font-family: Menlo, Consolas, Monaco, 'Courier New';")
        bot.addWidget(self.coord_label)
        main.addLayout(bot)

        if PYQTGRAPH_AVAILABLE:
            self._crosshair_cleanup = setup_crosshair(
                self.plot_widget, self.coord_label, lambda: self._curves
            )

    # ── ZMQ thread ─────────────────────────────────────────────────────────────

    def _start_zmq(self, addr=None):
        if not ZMQ_AVAILABLE:
            self.status_bar.setText("pyzmq not installed — pip install pyzmq")
            return
        self.zmq_thread = ZMQDocThread(addr=addr)
        self.zmq_thread.doc_received.connect(self._on_doc)
        self.zmq_thread.status_changed.connect(self.status_bar.setText)
        self.zmq_thread.start()
        # When the app starts mid-scan, start/descriptor are already gone from ZMQ.
        # Bootstrap motor info from the RE Manager running-item poll instead.
        if self.worker:
            self.worker.running_item_updated.connect(self._on_running_item)

    def restart_zmq(self, addr: str):
        """Stop the current ZMQ thread and start a new one with a new address."""
        if hasattr(self, "zmq_thread") and self.zmq_thread.isRunning():
            self.zmq_thread.requestInterruption()
            self.zmq_thread.wait(2000)
        self._start_zmq(addr)

    # ── Document handler ───────────────────────────────────────────────────────

    def _on_doc(self, name, doc):
        if name == "start":
            # Save current selections before resetting so they can be restored
            # when the new run's descriptor arrives (if signals match).
            self._saved_x = self._x_signal or ""
            self._saved_y = [
                self.y_list.item(i).text()
                for i in range(self.y_list.count())
                if self.y_list.item(i).isSelected()
            ]
            self._run_uid = doc.get("uid", "")
            self._start_motors    = [str(m) for m in (doc.get("motors",    []) or [])]
            self._start_detectors = [str(d) for d in (doc.get("detectors", []) or [])]
            self._pending_2d = len(self._start_motors) >= 2
            self._start_motor_ranges = _parse_motor_ranges(doc)
            self._reset_run()
            self.run_label.setText(
                f"Run: {doc.get('plan_name','?')}  [{self._run_uid[:8]}]")
            self.status_bar.setText("Run started — waiting for events…")

        elif name == "descriptor":
            keys = list(doc.get("data_keys", {}).keys())
            all_cols = keys + ["time"]
            self.x_combo.blockSignals(True)
            self.x_combo.clear()
            self.x_combo.addItems(all_cols)
            self.x_combo.blockSignals(False)

            self.y_list.blockSignals(True)
            self.y_list.clear()
            for k in all_cols:
                self.y_list.addItem(QListWidgetItem(k))
            self.y_list.blockSignals(False)

            prev_norm = self.norm_combo.currentData()
            self.norm_combo.blockSignals(True)
            self.norm_combo.clear()
            self.norm_combo.addItem("None", userData=None)
            for k in all_cols:
                self.norm_combo.addItem(k, userData=k)
            for i in range(self.norm_combo.count()):
                if self.norm_combo.itemData(i) == prev_norm:
                    self.norm_combo.setCurrentIndex(i)
                    break
            self.norm_combo.blockSignals(False)

            avail = set(all_cols)

            def _match_device(key, dev_name):
                return key == dev_name or key.startswith(dev_name + "_")

            def _fields_for(device_names):
                """Return data_keys that match any of the given device names."""
                matched = []
                for dev in device_names:
                    for k in keys:
                        if _match_device(k, dev) and k not in matched:
                            matched.append(k)
                return matched

            # ── X selection ──────────────────────────────────────────────────
            restored_x = self._saved_x if self._saved_x in avail else None
            if restored_x:
                x_chosen = restored_x
            else:
                # Use start-doc motors → name heuristic → first key
                motor_fields = _fields_for(self._start_motors)
                if motor_fields:
                    x_chosen = motor_fields[0]
                else:
                    heuristic = [k for k in keys
                                 if any(w in k.lower()
                                        for w in ("motor", "pos", "stage", "enc"))]
                    x_chosen = heuristic[0] if heuristic else (keys[0] if keys else "time")

            self.x_combo.setCurrentText(x_chosen)
            self._x_signal = x_chosen

            # ── Y selection ──────────────────────────────────────────────────
            restored_y = [s for s in self._saved_y if s in avail]
            if restored_y:
                y_chosen = set(restored_y)
            else:
                # Use start-doc detectors → everything-except-X fallback
                det_fields = _fields_for(self._start_detectors)
                if det_fields:
                    y_chosen = set(det_fields)
                else:
                    y_chosen = {k for k in keys
                                if k != x_chosen and k not in ("time", "seq_num")}

            for i in range(self.y_list.count()):
                self.y_list.item(i).setSelected(
                    self.y_list.item(i).text() in y_chosen)

            self._all_cols = all_cols
            self.status_bar.setText(f"Signals: {', '.join(all_cols)}")

            # Populate 2D widget combos every time so Y/Z stay valid
            self._2d_widget.set_columns(
                all_cols, x_chosen,
                self._start_motors, self._start_detectors,
            )
            self._apply_2d_scan_range(x_chosen)
            # Auto-switch to 2D map when ≥ 2 motors detected in start doc
            if self._pending_2d and not self._map_mode:
                self._pending_2d = False
                self._mode_tabs.setCurrentIndex(1)

        elif name == "event":
            seq = doc.get("seq_num", 0)
            self._ingest_event(
                seq=seq,
                t=doc.get("time", 0.0),
                data=doc.get("data", {}),
            )
            self.scan_point_completed.emit(seq)

        elif name == "event_page":
            seq_nums  = doc.get("seq_num", [])
            times     = doc.get("time", [])
            data_cols = doc.get("data", {})
            if seq_nums:
                self.scan_point_completed.emit(int(seq_nums[-1]))
            for i, seq in enumerate(seq_nums):
                self._ingest_event(
                    seq=seq,
                    t=times[i] if i < len(times) else 0.0,
                    data={k: col[i] for k, col in data_cols.items() if i < len(col)},
                )

        elif name == "stop":
            status = doc.get("exit_status", "unknown")
            n      = doc.get("num_events", "?")
            self.run_label.setText(f"Run complete — {status}  ({n} events)")
            self.status_bar.setText("Run finished — waiting for next run…")
            self._show_peak_stats()
            self._run_live_fit(force=True)

    def _ingest_event(self, seq, t, data):
        self._data.setdefault("seq_num", []).append(float(seq))
        self._data.setdefault("time",    []).append(float(t))
        for k, v in data.items():
            try:
                self._data.setdefault(k, []).append(float(v))
            except (TypeError, ValueError):
                pass

        # Descriptor was missed (ZMQ subscriber connected after it was published).
        # Auto-populate X/Y controls from the event data so plotting still works.
        if self.y_list.count() == 0 and data:
            self._auto_setup_from_event(data)

        self._update_plot()
        if self._map_mode:
            self._update_2d_plot()
        self._run_live_fit()
        self.status_bar.setText(f"Event #{seq}")

    def _on_running_item(self, item: dict):
        """Bootstrap motor/detector hints from the RE Manager running-item poll.

        Called on every worker poll tick.  Only acts once per run (when start doc
        was missed) and only when we already have ZMQ event data but no start-doc
        info yet (i.e. the app started mid-scan).
        """
        if not item or self._run_uid:
            # start doc already received — nothing to do
            return
        if not self._data:
            # no event data yet — nothing to bootstrap
            return
        kwargs    = item.get("kwargs", {}) or {}
        md        = kwargs.get("md", {}) or {}
        detectors = [str(d) for d in (kwargs.get("detectors", []) or
                                      md.get("detectors", []) or [])]
        # Parse motor ranges from kwargs directly (handles grid_scan args format)
        fake_start = {"plan_args": kwargs}
        ranges = _parse_motor_ranges(fake_start)
        # Derive motor list from parsed ranges; fall back to explicit motors key
        motors = list(ranges.keys()) or [str(m) for m in (kwargs.get("motors", []) or
                                                           md.get("motors", []) or [])]
        if (motors or ranges) and not self._start_motors:
            self._start_motors       = motors
            self._start_detectors    = detectors
            self._start_motor_ranges = ranges
            # Re-run auto-setup with the now-known motor/detector hints
            first_event_keys = [k for k in self._data if k not in ("seq_num", "time")]
            self._auto_setup_from_event({k: 0 for k in first_event_keys})
            # Apply axis range immediately (auto_setup may not have ranges yet)
            x_key = self._x_signal or self.x_combo.currentText() or "seq_num"
            self._apply_2d_scan_range(x_key)

    def _auto_setup_from_event(self, data):
        """Populate X/Y controls from event data keys when the descriptor was missed."""
        keys = sorted(data.keys())
        if not keys:
            return
        all_cols = keys + ["time"]
        avail    = set(all_cols)
        self._all_cols = all_cols  # cache so 2D widget can use it

        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.addItems(all_cols)
        self.x_combo.blockSignals(False)

        self.y_list.blockSignals(True)
        self.y_list.clear()
        for k in all_cols:
            self.y_list.addItem(QListWidgetItem(k))
        self.y_list.blockSignals(False)

        def _match_device(key, dev_name):
            return key == dev_name or key.startswith(dev_name + "_")

        def _fields_for(device_names):
            matched = []
            for dev in device_names:
                for k in keys:
                    if _match_device(k, dev) and k not in matched:
                        matched.append(k)
            return matched

        # ── X ──
        restored_x = self._saved_x if self._saved_x in avail else None
        if restored_x:
            x_chosen = restored_x
        else:
            motor_fields = _fields_for(self._start_motors)
            if motor_fields:
                x_chosen = motor_fields[0]
            else:
                heuristic = [k for k in keys
                             if any(w in k.lower()
                                    for w in ("motor", "pos", "stage", "enc"))]
                x_chosen = heuristic[0] if heuristic else keys[0]

        self.x_combo.setCurrentText(x_chosen)
        self._x_signal = x_chosen

        # ── Y ──
        restored_y = [s for s in self._saved_y if s in avail]
        if restored_y:
            y_chosen = set(restored_y)
        else:
            det_fields = _fields_for(self._start_detectors)
            if det_fields:
                y_chosen = set(det_fields)
            else:
                y_chosen = {k for k in keys
                            if k != x_chosen and k not in ("time", "seq_num")}

        for i in range(self.y_list.count()):
            self.y_list.item(i).setSelected(
                self.y_list.item(i).text() in y_chosen)

        # Also populate the 2D widget combos with what we know
        self._2d_widget.set_columns(
            all_cols, x_chosen,
            self._start_motors, self._start_detectors,
        )
        self._apply_2d_scan_range(x_chosen)
        if self._map_mode:
            self._update_2d_plot()

    # ── Plot ───────────────────────────────────────────────────────────────────

    def _on_mode_tab_changed(self, idx: int):
        self._1d_controls.setVisible(idx == 0)
        self._toggle_map_mode(idx == 1)

    def _toggle_map_mode(self, checked: bool):
        self._map_mode = checked
        self._plot_stack.setCurrentIndex(1 if checked else 0)
        if checked:
            # Use cached descriptor columns; fall back to keys currently in _data
            cols = self._all_cols or [k for k in self._data if k != "seq_num"]
            if cols:
                x_key = self._x_signal or self.x_combo.currentText() or "seq_num"
                self._2d_widget.set_columns(
                    cols, x_key,
                    self._start_motors, self._start_detectors,
                )
                self._apply_2d_scan_range(x_key)
            self._update_2d_plot()

    def _apply_2d_scan_range(self, x_key: str):
        """Pass planned motor extents to TwoDMapWidget if available."""
        y_key = self._2d_widget.get_y_signal()
        xr = self._start_motor_ranges.get(x_key)
        yr = self._start_motor_ranges.get(y_key)
        if xr and yr:
            self._2d_widget.set_scan_range(xr[0], xr[1], yr[0], yr[1])

    def _update_2d_plot(self):
        if not self._data:
            return
        x_key = self._x_signal or "seq_num"
        y_key = self._2d_widget.get_y_signal()
        z_key = self._2d_widget.get_z_signal()
        if not y_key or not z_key:
            return
        xs = np.array(self._data.get(x_key, []), dtype=float)
        ys = np.array(self._data.get(y_key, []), dtype=float)
        zs = np.array(self._data.get(z_key, []), dtype=float)
        self._2d_widget.replot(xs, ys, zs, x_key, y_key, z_key)

    def _on_x_changed(self, text):
        self._x_signal = text
        self._update_plot()
        if self._map_mode:
            self._update_2d_plot()
        self._run_live_fit(force=True)

    def _on_err_toggled(self):
        """Remove error items immediately when the checkbox is unchecked."""
        if not self._err_cb.isChecked():
            for item in self._error_items.values():
                try:
                    self.plot_widget.removeItem(item)
                except Exception:
                    pass
            self._error_items = {}
        else:
            self._update_plot()

    def _update_plot(self):
        if not PYQTGRAPH_AVAILABLE or not self._data:
            return

        x_key = self._x_signal or "seq_num"
        x_arr = np.array(self._data.get(x_key, []), dtype=float)
        if len(x_arr) == 0:
            return

        y_signals = [
            self.y_list.item(i).text()
            for i in range(self.y_list.count())
            if self.y_list.item(i).isSelected()
        ]
        norm_key = self.norm_combo.currentData()
        norm_arr = np.array(self._data.get(norm_key, []), dtype=float) \
                   if norm_key and norm_key in self._data else None
        show_err = self._err_cb.isChecked()

        # Curve names change when norm_key changes — remove stale curves and error items
        expected = {
            (sig if not norm_key else f"{sig}/{norm_key}")
            for sig in y_signals
        }
        for name in list(self._curves):
            if name not in expected:
                self.plot_widget.removeItem(self._curves.pop(name))
        for name in list(self._error_items):
            if name not in expected:
                try:
                    self.plot_widget.removeItem(self._error_items.pop(name))
                except Exception:
                    self._error_items.pop(name, None)

        for i, sig in enumerate(y_signals):
            y_vals = self._data.get(sig, [])
            n = min(len(x_arr), len(y_vals))
            if norm_arr is not None:
                n = min(n, len(norm_arr))
            if n == 0:
                continue
            x     = x_arr[:n]
            y_raw = np.array(y_vals[:n], dtype=float)
            norm_raw = norm_arr[:n] if norm_arr is not None else None

            y = y_raw.copy()
            if norm_raw is not None:
                denom = norm_raw
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = np.where(denom != 0, y / denom, np.nan)

            sigma = _poisson_sigma(y_raw, norm_raw)

            deriv_mode = self._deriv_combo.currentIndex()
            if deriv_mode > 0:
                _, y, sigma = self._apply_deriv(x, y, sigma, order=deriv_mode)
            if self._log_y_cb.isChecked():
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = np.log10(np.where(y > 0, y, np.nan))

            mask = np.isfinite(x) & np.isfinite(y)
            x, y, sigma = x[mask], y[mask], sigma[mask]

            curve_name = sig if not norm_key else f"{sig}/{norm_key}"
            color = self.COLORS[i % len(self.COLORS)]
            if curve_name not in self._curves:
                pen = pg.mkPen(color=color, width=2)
                self._curves[curve_name] = self.plot_widget.plot(
                    x, y, pen=pen, name=curve_name,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
            else:
                self._curves[curve_name].setData(x, y)

            if show_err and np.any(np.isfinite(sigma)):
                height = 2 * sigma
                if curve_name in self._error_items:
                    self._error_items[curve_name].setData(x=x, y=y, height=height)
                else:
                    err_item = pg.ErrorBarItem(
                        x=x, y=y, height=height,
                        beam=0.0, pen=pg.mkPen(color=color, width=1),
                    )
                    self.plot_widget.addItem(err_item)
                    self._error_items[curve_name] = err_item

        self.plot_widget.setLabel("bottom", x_key)
        y_label = ", ".join(y_signals) if y_signals else "Y"
        if norm_key:
            y_label += f"  /  {norm_key}"
        deriv_mode = self._deriv_combo.currentIndex()
        if deriv_mode == 1:
            y_label += "  [dy/dx]"
        elif deriv_mode == 2:
            y_label += "  [d²y/dx²]"
        if self._log_y_cb.isChecked():
            y_label = f"log₁₀({y_label})"
        self.plot_widget.setLabel("left", y_label)
        smart_legend_position(self.plot_widget)

    @staticmethod
    def _apply_deriv(x, y, sigma=None, order=1):
        """Central-difference derivative; same implementation as MongoDataBrowserTab."""
        finite = np.isfinite(x) & np.isfinite(y)
        if finite.sum() < 3:
            nan_y = np.full_like(y, np.nan)
            nan_s = np.full_like(sigma, np.nan) if sigma is not None else None
            return x, nan_y, nan_s
        xf, yf = x[finite], y[finite]
        dy = np.gradient(yf, xf)
        if order == 2:
            dy = np.gradient(dy, xf)
        y_out = np.full_like(y, np.nan)
        y_out[finite] = dy
        if sigma is None:
            return x, y_out, None
        sf = sigma[finite]
        n  = len(sf)
        dsf = np.full(n, np.nan)
        if n >= 3:
            fin_s = np.isfinite(sf)
            if fin_s.sum() >= 2:
                dxc = xf[2:] - xf[:-2]
                valid = fin_s[2:] & fin_s[:-2] & (dxc != 0)
                dsf[1:-1] = np.where(
                    valid,
                    np.sqrt(np.where(fin_s[2:], sf[2:], 0)**2
                            + np.where(fin_s[:-2], sf[:-2], 0)**2)
                    / np.where(dxc != 0, dxc, np.nan),
                    np.nan,
                )
                dx0 = xf[1] - xf[0]
                if fin_s[0] and fin_s[1] and dx0 != 0:
                    dsf[0] = np.sqrt(sf[0]**2 + sf[1]**2) / abs(dx0)
                dxn = xf[-1] - xf[-2]
                if fin_s[-1] and fin_s[-2] and dxn != 0:
                    dsf[-1] = np.sqrt(sf[-1]**2 + sf[-2]**2) / abs(dxn)
            if order == 2:
                dsf2 = np.full(n, np.nan)
                fin2 = np.isfinite(dsf)
                if fin2.sum() >= 3:
                    dxc2 = xf[2:] - xf[:-2]
                    v2 = fin2[2:] & fin2[:-2] & (dxc2 != 0)
                    dsf2[1:-1] = np.where(
                        v2,
                        np.sqrt(np.where(fin2[2:], dsf[2:], 0)**2
                                + np.where(fin2[:-2], dsf[:-2], 0)**2)
                        / np.where(dxc2 != 0, dxc2, np.nan),
                        np.nan,
                    )
                    dx0b = xf[1] - xf[0]
                    if fin2[0] and fin2[1] and dx0b != 0:
                        dsf2[0] = np.sqrt(dsf[0]**2 + dsf[1]**2) / abs(dx0b)
                    dxnb = xf[-1] - xf[-2]
                    if fin2[-1] and fin2[-2] and dxnb != 0:
                        dsf2[-1] = np.sqrt(dsf[-1]**2 + dsf[-2]**2) / abs(dxnb)
                dsf = dsf2
        sigma_out = np.full_like(sigma, np.nan)
        sigma_out[finite] = dsf
        return x, y_out, sigma_out

    def _reset_run(self):
        self._data    = {}
        self._x_signal = None
        self._live_fit_n_fitted = 0
        self._2d_widget.clear()
        if PYQTGRAPH_AVAILABLE and self._live_fit_curve is not None:
            try:
                self.plot_widget.removeItem(self._live_fit_curve)
            except Exception:
                pass
            self._live_fit_curve = None
        if PYQTGRAPH_AVAILABLE:
            self.plot_widget.setTitle("")
        if PYQTGRAPH_AVAILABLE:
            for curve in self._curves.values():
                try:
                    self.plot_widget.removeItem(curve)
                except Exception:
                    pass
            for item in self._error_items.values():
                try:
                    self.plot_widget.removeItem(item)
                except Exception:
                    pass
            pi = self.plot_widget.getPlotItem()
            if pi.legend:
                pi.legend.clear()
        self._curves = {}
        self._error_items = {}
        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.blockSignals(False)
        self.y_list.blockSignals(True)
        self.y_list.clear()
        self.y_list.blockSignals(False)

    # ── Peak stats + Live Fit ──────────────────────────────────────────────────

    def _on_live_fit_toggled(self):
        if not self._live_fit_cb.isChecked():
            if self._live_fit_curve is not None and PYQTGRAPH_AVAILABLE:
                try:
                    self.plot_widget.removeItem(self._live_fit_curve)
                except Exception:
                    pass
                self._live_fit_curve = None
            self._live_fit_n_fitted = 0

    def _get_fit_xy(self):
        """Return (x, y, model_name) arrays for the first selected Y signal, or None."""
        if not self._data:
            return None
        x_key = self._x_signal or "seq_num"
        x = np.array(self._data.get(x_key, []), dtype=float)
        y_signals = [
            self.y_list.item(i).text()
            for i in range(self.y_list.count())
            if self.y_list.item(i).isSelected()
        ]
        if not y_signals:
            return None
        y_key = y_signals[0]
        y = np.array(self._data.get(y_key, []), dtype=float)
        n = min(len(x), len(y))
        if n < 5:
            return None
        x_, y_ = x[:n], y[:n]
        mask = np.isfinite(x_) & np.isfinite(y_)
        x_, y_ = x_[mask], y_[mask]
        if len(x_) < 5:
            return None
        model_name = self._live_fit_model_combo.currentText()
        if model_name not in _peak_fit.SIGNAL_MODELS:
            return None
        return x_, y_, model_name

    def _show_peak_stats(self):
        """Compute peak statistics from accumulated data and show in plot title."""
        if not PYQTGRAPH_AVAILABLE or not self._data:
            return
        result = self._get_fit_xy()
        if result is None:
            return
        x, y, _ = result
        try:
            i_max  = int(np.argmax(y))
            y_pos  = y - y.min()
            denom  = float(np.sum(y_pos))
            com    = float(np.sum(x * y_pos) / denom) if denom > 0 else float(x[i_max])

            half   = (float(y.max()) + float(y.min())) / 2.0
            above  = y >= half
            edges  = np.where(np.diff(above.astype(int)))[0]

            if len(edges) >= 2:
                def _ic(i):
                    x0, x1 = float(x[i]), float(x[i+1])
                    y0, y1 = float(y[i]), float(y[i+1])
                    return x0 + (half - y0) / (y1 - y0) * (x1 - x0) if y1 != y0 else (x0+x1)/2
                xl   = _ic(edges[0])
                xr   = _ic(edges[-1])
                cen  = (xl + xr) / 2.0
                fwhm = abs(xr - xl)
                title = (f"cen = {cen:.5g}    FWHM = {fwhm:.4g}"
                         f"    max = {y[i_max]:.4g} @ {x[i_max]:.5g}"
                         f"    COM = {com:.5g}")
            else:
                title = (f"max = {y[i_max]:.4g} @ {x[i_max]:.5g}"
                         f"    COM = {com:.5g}")

            self.plot_widget.setTitle(title, color="#aaaaaa", size="11pt")
        except Exception:
            pass

    def _run_live_fit(self, force=False):
        """Fit the first selected Y signal with lmfit if Live Fit is enabled.

        Throttled to every 5 new points during a run; always runs when force=True
        (called on the stop document for a clean final fit).
        """
        if not self._live_fit_cb.isChecked():
            return
        if not _peak_fit.LMFIT_AVAILABLE or not PYQTGRAPH_AVAILABLE:
            return
        result = self._get_fit_xy()
        if result is None:
            return
        x, y, model_name = result
        n = len(x)
        if not force and (n - self._live_fit_n_fitted) < 5:
            return
        bg_name = self._live_fit_bg_combo.currentText()
        try:
            params = _peak_fit.auto_guess(x, y, model_name, bg_name)
            x_fit, y_fit, info = _peak_fit.run_fit(x, y, params, model_name, bg_name=bg_name)
            self._live_fit_n_fitted = n

            fit_pen = pg.mkPen("#ffcc44", width=2, style=Qt.PenStyle.DashLine)
            if self._live_fit_curve is None:
                self._live_fit_curve = self.plot_widget.plot(
                    x_fit, y_fit, pen=fit_pen, name="live fit"
                )
            else:
                self._live_fit_curve.setData(x_fit, y_fit)

            is_step      = model_name.startswith("Step")
            w_lbl        = "10–90% w" if is_step else "FWHM"
            display_name = info.get("model", model_name)
            cen  = info.get("x0", float("nan"))
            fwhm = info.get("fwhm", float("nan"))
            r2   = info.get("r2", 0.0)
            if model_name == "None":
                title = (f"Live Fit: {display_name}"
                         f"    R² = {r2:.4f}")
            else:
                title = (f"Live Fit: {display_name}"
                         f"    cen = {cen:.5g}"
                         f"    {w_lbl} = {fwhm:.4g}"
                         f"    R² = {r2:.4f}")
            self.plot_widget.setTitle(title, color="#ffcc44", size="11pt")
        except Exception:
            pass

    # ── Double-click: move motor ───────────────────────────────────────────────

    def _on_plot_clicked(self, event):
        if not event.double():
            return
        if not self.worker:
            return
        pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return

        vb = self.plot_widget.getPlotItem().vb
        mp = vb.mapSceneToView(pos)
        x_val  = mp.x()
        x_label = self._x_signal or self.plot_widget.getAxis("bottom").labelText or ""

        # Strip common readback suffixes to get motor name
        motor_guess = x_label
        for suffix in ("_readback", "_setpoint", "_user_readback", "_user_setpoint"):
            if motor_guess.endswith(suffix):
                motor_guess = motor_guess[: -len(suffix)]
                break

        r = QMessageBox.question(
            self, "Move Motor",
            f"Move  '{motor_guess}'  to  {x_val:.5g} ?\n\n"
            f"(X-axis signal: {x_label})",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self.move_requested.emit(motor_guess, x_val)

    # ── Screenshot ─────────────────────────────────────────────────────────────

    def _save_screenshot(self):
        if not PYQTGRAPH_AVAILABLE:
            return
        QApplication.clipboard().setPixmap(self.plot_widget.grab())
        self.status_bar.setText("Plot copied to clipboard — paste into any document")

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if hasattr(self, "zmq_thread"):
            self.zmq_thread.requestInterruption()
            self.zmq_thread.wait(2000)
        if self._crosshair_cleanup:
            self._crosshair_cleanup()
        super().closeEvent(event)
