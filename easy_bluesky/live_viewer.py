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
    QMessageBox, QApplication, QSplitter, QSizePolicy,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from .config import PLOT_COLORS, ZMQ_DOC_ADDR
from .plot_tools import setup_crosshair
from . import peak_fit as _peak_fit


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

        self._err_cb = QCheckBox("± Errors")
        self._err_cb.setToolTip(
            "Overlay Poisson √N error bars (propagated through normalization)"
        )
        self._err_cb.stateChanged.connect(self._on_err_toggled)
        ctrl.addWidget(self._err_cb)

        btn_screenshot = QPushButton("Screenshot")
        btn_screenshot.setToolTip("Save the current plot as a PNG image")
        btn_screenshot.clicked.connect(self._save_screenshot)
        ctrl.addWidget(btn_screenshot)

        ctrl.addSpacing(12)
        self._live_fit_cb = QCheckBox("Live Fit:")
        self._live_fit_cb.setToolTip(
            "Fit a model to data as the scan runs (first selected Y signal).\n"
            "Final parameters appear in the plot title on run stop."
        )
        self._live_fit_cb.setEnabled(_peak_fit.LMFIT_AVAILABLE)
        if not _peak_fit.LMFIT_AVAILABLE:
            self._live_fit_cb.setToolTip("pip install lmfit to enable Live Fit")
        self._live_fit_cb.stateChanged.connect(self._on_live_fit_toggled)
        ctrl.addWidget(self._live_fit_cb)

        self._live_fit_model_combo = QComboBox()
        self._live_fit_model_combo.setFixedHeight(26)
        self._live_fit_model_combo.setMinimumWidth(110)
        self._live_fit_model_combo.setMaximumWidth(180)
        for m in _peak_fit.PEAK_MODELS:
            self._live_fit_model_combo.addItem(m)
        self._live_fit_model_combo.insertSeparator(
            self._live_fit_model_combo.count()
        )
        for m in _peak_fit.STEP_MODELS:
            self._live_fit_model_combo.addItem(m)
        ctrl.addWidget(self._live_fit_model_combo)

        ctrl.addStretch()

        self.run_label = QLabel("No active run")
        self.run_label.setObjectName("dim_text")
        ctrl.addWidget(self.run_label)
        main.addLayout(ctrl)

        # Y list on the right of the plot (in a resizable splitter)
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMinimumWidth(100)
        self.y_list.setToolTip("Y signals — click to select/deselect")
        self.y_list.itemSelectionChanged.connect(self._update_plot)
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
        main.addWidget(plot_splitter, 1)

        # Bottom bar: status left, cursor coords right
        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        self.status_bar = QLabel("Waiting for run…")
        self.status_bar.setObjectName("dim_text")
        self.status_bar.setStyleSheet("font-size: 12px; padding: 4px;")
        bot.addWidget(self.status_bar, 1)

        self.coord_label = QLabel("")
        self.coord_label.setObjectName("dim_text")
        self.coord_label.setStyleSheet("font-size: 11px; padding: 4px; font-family: Menlo, Monaco, Courier New, monospace;")
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

            self.status_bar.setText(f"Signals: {', '.join(all_cols)}")

        elif name == "event":
            self._ingest_event(
                seq=doc.get("seq_num", 0),
                t=doc.get("time", 0.0),
                data=doc.get("data", {}),
            )

        elif name == "event_page":
            seq_nums  = doc.get("seq_num", [])
            times     = doc.get("time", [])
            data_cols = doc.get("data", {})
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
        self._run_live_fit()
        self.status_bar.setText(f"Event #{seq}")

    def _auto_setup_from_event(self, data):
        """Populate X/Y controls from event data keys when the descriptor was missed."""
        keys = sorted(data.keys())
        if not keys:
            return
        all_cols = keys + ["time"]
        avail    = set(all_cols)

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

    # ── Plot ───────────────────────────────────────────────────────────────────

    def _on_x_changed(self, text):
        self._x_signal = text
        self._update_plot()

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
        self.plot_widget.setLabel("left", y_label)

    def _reset_run(self):
        self._data    = {}
        self._x_signal = None
        self._live_fit_n_fitted = 0
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
        if model_name not in _peak_fit.MODELS:
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
        try:
            params = _peak_fit.auto_guess(x, y, model_name)
            x_fit, y_fit, info = _peak_fit.run_fit(x, y, params, model_name)
            self._live_fit_n_fitted = n

            fit_pen = pg.mkPen("#ffcc44", width=2, style=Qt.PenStyle.DashLine)
            if self._live_fit_curve is None:
                self._live_fit_curve = self.plot_widget.plot(
                    x_fit, y_fit, pen=fit_pen, name="live fit"
                )
            else:
                self._live_fit_curve.setData(x_fit, y_fit)

            is_step = model_name.startswith("Step")
            w_lbl   = "10–90% w" if is_step else "FWHM"
            cen  = info.get("x0", float("nan"))
            fwhm = info.get("fwhm", float("nan"))
            r2   = info.get("r2", 0.0)
            title = (f"Live Fit: {model_name}"
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

        item = {
            "name":      "mv",
            "args":      [motor_guess, x_val],
            "kwargs":    {},
            "item_type": "plan",
        }
        ok, msg = self.worker.execute_item(item)
        if not ok:
            QMessageBox.warning(self, "Move Failed", msg)

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
