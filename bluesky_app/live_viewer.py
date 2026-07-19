"""live_viewer.py — Live Viewer tab: Kafka consumer + pyqtgraph."""

import time
import numpy as np
try:
    import msgpack
    import msgpack_numpy as mpn
    from confluent_kafka import Consumer as KafkaConsumer
    KAFKA_AVAILABLE = True
except ImportError:
    KAFKA_AVAILABLE = False
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
from .config import KAFKA_SERVER, KAFKA_TOPIC, PLOT_COLORS

class KafkaThread(QThread):
    doc_received = pyqtSignal(str, dict)

    def run(self):
        if not KAFKA_AVAILABLE:
            return
        c = KafkaConsumer({
            "bootstrap.servers": KAFKA_SERVER,
            "group.id":          f"bluesky-app-{int(time.time())}",
            "auto.offset.reset": "latest",
        })
        c.subscribe([KAFKA_TOPIC])
        while not self.isInterruptionRequested():
            msg = c.poll(0.5)
            if msg is None or msg.error():
                continue
            try:
                doc = msgpack.unpackb(msg.value(), object_hook=mpn.decode)
                if isinstance(doc, list) and len(doc) == 2:
                    self.doc_received.emit(doc[0], doc[1])
            except Exception:
                pass


class LiveViewer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._x_data   = {}
        self._y_data   = {}
        self._curves   = {}
        self._run_uid  = None
        self._signals  = []
        self._x_signal = None
        self._build()
        self._start_kafka()

    def _build(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)

        # Controls
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("X axis:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(140)
        ctrl.addWidget(self.x_combo)
        ctrl.addWidget(QLabel("Y axes:"))
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMaximumHeight(60)
        self.y_list.setMaximumWidth(240)
        ctrl.addWidget(self.y_list)
        btn_apply = QPushButton("Apply")
        btn_apply.setObjectName("btn_primary")
        btn_apply.clicked.connect(self._apply_axes)
        ctrl.addWidget(btn_apply)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear_plot)
        ctrl.addWidget(btn_clear)
        ctrl.addStretch()
        self.run_label = QLabel("No active run")
        self.run_label.setStyleSheet("color: #888;")
        ctrl.addWidget(self.run_label)
        main.addLayout(ctrl)

        # Plot
        if PYQTGRAPH_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.setLabel("bottom", "X")
            self.plot_widget.setLabel("left",   "Y")
            self.plot_widget.addLegend()
            main.addWidget(self.plot_widget, 1)
        else:
            main.addWidget(QLabel("pyqtgraph not available — pip install pyqtgraph"), 1)

        # Status
        self.status_bar = QLabel("Waiting for Kafka data...")
        self.status_bar.setStyleSheet("color: #888; font-size: 12px; padding: 4px;")
        main.addWidget(self.status_bar)

    COLORS = ["#1f77b4","#ff7f0e","#2ca02c","#d62728",
              "#9467bd","#8c564b","#e377c2","#17becf"]

    def _start_kafka(self):
        if not KAFKA_AVAILABLE:
            self.status_bar.setText("Kafka not available")
            return
        self.kafka_thread = KafkaThread()
        self.kafka_thread.doc_received.connect(self._on_doc)
        self.kafka_thread.start()

    def _on_doc(self, name, body):
        if name == "start":
            self._run_uid = body.get("uid")
            self._x_data  = {}
            self._y_data  = {}
            self._signals = []
            self.run_label.setText(
                f"Run: {body.get('plan_name','?')} [{self._run_uid[:8]}]")
            self.status_bar.setText("Run started — waiting for descriptor...")

        elif name == "descriptor":
            keys = list(body.get("data_keys", {}).keys())
            self._signals = keys
            self.x_combo.clear()
            self.x_combo.addItems(keys)
            self.y_list.clear()
            for k in keys:
                self.y_list.addItem(QListWidgetItem(k))
            # Auto-select first as X, rest as Y
            if keys:
                self._x_signal = keys[0]
                for i in range(1, self.y_list.count()):
                    self.y_list.item(i).setSelected(True)
                self._apply_axes()
            self.status_bar.setText(f"Signals: {', '.join(keys)}")

        elif name == "event":
            data = body.get("data", {})
            seq  = body.get("seq_num", 0)
            for k, v in data.items():
                if k not in self._x_data:
                    self._x_data[k] = []
                    self._y_data[k] = []
                try:
                    self._x_data[k].append(float(data.get(self._x_signal or k, seq)))
                    self._y_data[k].append(float(v))
                except Exception:
                    pass
            self._update_plot()
            self.status_bar.setText(f"Event #{seq} received")

        elif name == "stop":
            status = body.get("exit_status", "unknown")
            n      = body.get("num_events", {})
            self.run_label.setText(
                f"Run complete — {status} — {n} events")

    def _apply_axes(self):
        self._x_signal = self.x_combo.currentText()
        self._update_plot()

    def _update_plot(self):
        if not PYQTGRAPH_AVAILABLE:
            return
        y_signals = [self.y_list.item(i).text()
                     for i in range(self.y_list.count())
                     if self.y_list.item(i).isSelected()]
        if not y_signals or not self._x_signal:
            return

        for sig, curve in list(self._curves.items()):
            if sig not in y_signals:
                self.plot_widget.removeItem(curve)
                del self._curves[sig]

        for i, sig in enumerate(y_signals):
            x = self._x_data.get(self._x_signal, [])
            y = self._y_data.get(sig, [])
            if not x or not y or len(x) != len(y):
                continue
            color = self.COLORS[i % len(self.COLORS)]
            if sig not in self._curves:
                pen   = pg.mkPen(color=color, width=2)
                curve = self.plot_widget.plot(
                    np.array(x), np.array(y),
                    pen=pen, name=sig,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
                self._curves[sig] = curve
            else:
                self._curves[sig].setData(np.array(x), np.array(y))

    def _clear_plot(self):
        if PYQTGRAPH_AVAILABLE:
            self.plot_widget.clear()
        self._curves  = {}
        self._x_data  = {}
        self._y_data  = {}
        self.run_label.setText("No active run")
        self.status_bar.setText("Cleared")
