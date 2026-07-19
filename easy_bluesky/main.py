"""main.py — MainWindow and application entry point."""

import sys
import threading
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QStatusBar, QLabel,
)
from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtGui import QPalette, QColor
from .config import ZMQ_CONTROL, APP_NAME, ACCENT
from .styles import APP_STYLE
from .worker import ZMQWorker
from .queue_manager import QueueManager
from .plan_builder import PlanBuilder
from .live_viewer import LiveViewer
from .data_browser import DataBrowser

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EasyBluesky")
        self.setMinimumSize(1200, 800)
        self.worker = ZMQWorker()
        self._setup_ui()
        self._setup_worker()
        self._connect()

    def _setup_ui(self):
        self.setStyleSheet(APP_STYLE)

        # Central tabs
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        self.queue_mgr   = QueueManager(self.worker)
        self.plan_builder = PlanBuilder(self.worker)
        self.live_viewer  = LiveViewer()
        self.data_browser = DataBrowser()

        self.tabs.addTab(self.queue_mgr,    "⚙  Queue Manager")
        self.tabs.addTab(self.plan_builder, "🔧  Plan Builder")
        self.tabs.addTab(self.live_viewer,  "📡  Live Viewer")
        self.tabs.addTab(self.data_browser, "📂  Data Browser")

        self.setCentralWidget(self.tabs)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.conn_label = QLabel("⬤  Connecting...")
        self.conn_label.setStyleSheet("color: #ffcc00;")
        self.status_bar.addPermanentWidget(self.conn_label)
        self.status_bar.showMessage("EasyBluesky  |  ZMQ: " + ZMQ_CONTROL)

    def _setup_worker(self):
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.status_updated.connect(self.queue_mgr.update_status)
        self.worker.queue_updated.connect(self.queue_mgr.update_queue)
        self.worker.history_updated.connect(self.queue_mgr.update_history)
        self.worker.plans_updated.connect(self._on_plans_updated)
        self.worker.devices_updated.connect(self._on_devices_updated)

        self.worker_thread.start()

    def _connect(self):
        QTimer.singleShot(100, self._do_connect)

    def _do_connect(self):
        ok = self.worker.connect()
        if ok:
            # Start polling in background
            poll_thread = threading.Thread(
                target=self.worker.poll, daemon=True)
            poll_thread.start()

    def _on_connected(self):
        self.conn_label.setText("⬤  Connected")
        self.conn_label.setStyleSheet("color: #2ca02c;")
        self.status_bar.showMessage("Connected to RE Manager at " + ZMQ_CONTROL)

    def _on_disconnected(self):
        self.conn_label.setText("⬤  Disconnected")
        self.conn_label.setStyleSheet("color: #d62728;")

    def _on_error(self, msg):
        self.conn_label.setText("⬤  Error")
        self.conn_label.setStyleSheet("color: #ff7f0e;")
        self.queue_mgr.append_console(f"[ERROR] {msg}")

    def _on_plans_updated(self, plans):
        self.queue_mgr.plans   = plans
        self.queue_mgr.devices = self.worker.rm.devices_allowed().get("devices_allowed", {}) if self.worker.rm else {}
        self.plan_builder.update_plans(plans)

    def _on_devices_updated(self, devices):
        self.queue_mgr.devices = devices
        self.plan_builder.update_devices(devices)

    def closeEvent(self, event):
        self.worker.stop()
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EasyBluesky")
    app.setStyle("Fusion")

    # Dark palette base
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor("#1e1e1e"))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor("#d4d4d4"))
    palette.setColor(QPalette.ColorRole.Base,            QColor("#1e1e1e"))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor("#252526"))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor("#252526"))
    palette.setColor(QPalette.ColorRole.ToolTipText,     QColor("#d4d4d4"))
    palette.setColor(QPalette.ColorRole.Text,            QColor("#d4d4d4"))
    palette.setColor(QPalette.ColorRole.Button,          QColor("#3c3c3c"))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor("#d4d4d4"))
    palette.setColor(QPalette.ColorRole.BrightText,      QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link,            QColor("#1f77b4"))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor("#1f77b4"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
