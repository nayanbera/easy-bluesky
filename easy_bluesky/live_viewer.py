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
    QComboBox, QListWidget, QListWidgetItem, QAbstractItemView,
)
from PyQt6.QtCore import QThread, pyqtSignal
from .config import PLOT_COLORS, ZMQ_DOC_PORT


class ZMQDocThread(QThread):
    """Background thread: receive bluesky documents from ZMQ PUB socket."""
    doc_received   = pyqtSignal(str, dict)
    status_changed = pyqtSignal(str)

    def run(self):
        if not ZMQ_AVAILABLE:
            self.status_changed.emit("pyzmq not installed")
            return

        ctx  = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.connect(f"tcp://localhost:{ZMQ_DOC_PORT}")
        sock.subscribe(b"")
        sock.setsockopt(zmq.RCVTIMEO, 500)

        self.status_changed.emit(f"Listening on ZMQ port {ZMQ_DOC_PORT}…")

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

    def __init__(self, parent=None):
        super().__init__(parent)
        # All signal data stored independently: {key: [float, ...]}
        # "time" and "seq_num" are always present after the first event.
        self._data     = {}   # key → list of float values
        self._curves   = {}   # y_signal → PlotDataItem
        self._run_uid  = None
        self._x_signal = None
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
        self.x_combo.currentTextChanged.connect(self._on_x_changed)
        ctrl.addWidget(self.x_combo)

        ctrl.addWidget(QLabel("Y:"))
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMaximumHeight(56)
        self.y_list.setMaximumWidth(220)
        self.y_list.itemSelectionChanged.connect(self._update_plot)
        ctrl.addWidget(self.y_list)

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._reset_run)
        ctrl.addWidget(btn_clear)
        ctrl.addStretch()

        self.run_label = QLabel("No active run")
        self.run_label.setObjectName("dim_text")
        ctrl.addWidget(self.run_label)
        main.addLayout(ctrl)

        if PYQTGRAPH_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            main.addWidget(self.plot_widget, 1)
        else:
            main.addWidget(QLabel("pyqtgraph not available — pip install pyqtgraph"), 1)

        self.status_bar = QLabel("Waiting for run…")
        self.status_bar.setObjectName("dim_text")
        self.status_bar.setStyleSheet("font-size: 12px; padding: 4px;")
        main.addWidget(self.status_bar)

    # ── ZMQ thread ─────────────────────────────────────────────────────────────

    def _start_zmq(self):
        if not ZMQ_AVAILABLE:
            self.status_bar.setText("pyzmq not installed — pip install pyzmq")
            return
        self.zmq_thread = ZMQDocThread()
        self.zmq_thread.doc_received.connect(self._on_doc)
        self.zmq_thread.status_changed.connect(self.status_bar.setText)
        self.zmq_thread.start()

    # ── Document handler ───────────────────────────────────────────────────────

    def _on_doc(self, name, doc):
        if name == "start":
            self._run_uid = doc.get("uid", "")
            self._reset_run()
            self.run_label.setText(
                f"Run: {doc.get('plan_name','?')}  [{self._run_uid[:8]}]")
            self.status_bar.setText("Run started — waiting for events…")

        elif name == "descriptor":
            keys = list(doc.get("data_keys", {}).keys())
            # Build display list: data signals first, then time
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

            # Auto-select: first non-detector signal as X, detectors as Y
            # Heuristic: motor-like = has "motor" or "pos" in name, else first key
            motor_keys = [k for k in keys if any(w in k.lower() for w in ("motor", "pos", "stage", "enc"))]
            det_keys   = [k for k in keys if k not in motor_keys]

            if motor_keys:
                x_default = motor_keys[0]
            elif keys:
                x_default = keys[0]
            else:
                x_default = "time"

            self.x_combo.setCurrentText(x_default)
            self._x_signal = x_default

            # Auto-select Y: detector signals (exclude x and time)
            for i in range(self.y_list.count()):
                sig = self.y_list.item(i).text()
                should_select = (sig in det_keys) or (not det_keys and sig != x_default and sig != "time")
                self.y_list.item(i).setSelected(should_select)

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
            self.run_label.setText(
                f"Run complete — {status}  ({n} events)")
            self.status_bar.setText("Run finished — waiting for next run…")

    def _ingest_event(self, seq, t, data):
        """Store one event's data and refresh the plot."""
        self._data.setdefault("seq_num", []).append(float(seq))
        self._data.setdefault("time",    []).append(float(t))
        for k, v in data.items():
            try:
                self._data.setdefault(k, []).append(float(v))
            except (TypeError, ValueError):
                pass
        self._update_plot()
        self.status_bar.setText(f"Event #{seq}")

    # ── Plot ───────────────────────────────────────────────────────────────────

    def _on_x_changed(self, text):
        self._x_signal = text
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

        # Remove curves for deselected signals
        for sig in list(self._curves):
            if sig not in y_signals:
                self.plot_widget.removeItem(self._curves.pop(sig))

        for i, sig in enumerate(y_signals):
            y_vals = self._data.get(sig, [])
            n = min(len(x_arr), len(y_vals))
            if n == 0:
                continue
            x = x_arr[:n]
            y = np.array(y_vals[:n], dtype=float)
            color = self.COLORS[i % len(self.COLORS)]
            if sig not in self._curves:
                pen = pg.mkPen(color=color, width=2)
                self._curves[sig] = self.plot_widget.plot(
                    x, y, pen=pen, name=sig,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
            else:
                self._curves[sig].setData(x, y)

        self.plot_widget.setLabel("bottom", x_key)
        self.plot_widget.setLabel("left",   ", ".join(y_signals) if y_signals else "Y")

    def _reset_run(self):
        self._data   = {}
        self._curves = {}
        if PYQTGRAPH_AVAILABLE:
            self.plot_widget.clear()

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if hasattr(self, "zmq_thread"):
            self.zmq_thread.requestInterruption()
            self.zmq_thread.wait(2000)
        super().closeEvent(event)
