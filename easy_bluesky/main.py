"""main.py — MainWindow and application entry point."""

import sys
import threading
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QStatusBar, QLabel,
    QWidget, QVBoxLayout, QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, QTimer
from .config import ZMQ_CONTROL, APP_NAME, ACCENT
from .themes import (
    build_stylesheet, build_palette, load_saved_theme, save_theme,
    theme_names, THEMES,
)
from .worker import ZMQWorker
from .re_control_bar import REControlBar
from .queue_manager import QueueManager
from .plan_builder import PlanBuilder
from .experiments_tab import ExperimentsTab
from .devices_plans_tab import DevicesPlansTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EasyBluesky")
        self.setMinimumSize(1200, 800)
        self._current_theme = load_saved_theme()
        self.worker = ZMQWorker()
        self._setup_ui()
        self._setup_worker()
        self._connect()
        # Apply saved theme after UI is fully built
        self.apply_theme(self._current_theme)

    def _setup_ui(self):
        self.setStyleSheet(build_stylesheet(self._current_theme))

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        self.experiments_tab    = ExperimentsTab(self.worker)
        self.queue_mgr          = QueueManager(self.worker)
        self.plan_builder       = PlanBuilder(self.worker)
        self.devices_plans_tab  = DevicesPlansTab()

        self.tabs.addTab(self.experiments_tab,   "🧪  Experiments")
        self.tabs.addTab(self.queue_mgr,         "⚙  Queue Manager")
        self.tabs.addTab(self.plan_builder,      "🔧  Plan Builder")
        self.tabs.addTab(self.devices_plans_tab, "🔬  Devices & Plans")

        # RE control bar sits above the tabs
        self.re_bar = REControlBar()

        central = QWidget()
        vlay = QVBoxLayout(central)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(self.re_bar)
        vlay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # Menu bar — View > Theme
        self._build_menu()

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.conn_label = QLabel("⬤  Connecting...")
        self.conn_label.setStyleSheet("color: #ffcc00;")
        self.status_bar.addPermanentWidget(self.conn_label)
        self.status_bar.showMessage("EasyBluesky  |  ZMQ: " + ZMQ_CONTROL)

    def _build_menu(self):
        from PyQt6.QtGui import QActionGroup
        menubar = self.menuBar()

        # ── File menu ──────────────────────────────────────────────────────────
        file_menu = menubar.addMenu("File")
        self._recent_menu = file_menu.addMenu("Recent Experiments")
        self._refresh_recent_menu()

        # ── View menu ──────────────────────────────────────────────────────────
        view_menu = menubar.addMenu("View")
        theme_menu = view_menu.addMenu("Theme")

        self._theme_actions = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        for name in theme_names():
            act = theme_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(name == self._current_theme)
            act.triggered.connect(lambda checked, n=name: self.apply_theme(n))
            group.addAction(act)
            self._theme_actions[name] = act

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        try:
            recent = self.experiments_tab.get_recent_experiments(10)
        except Exception:
            return
        if not recent:
            self._recent_menu.addAction("(none)").setEnabled(False)
            return
        for path, info in recent:
            name    = info.get("name", Path(path).name)
            created = info.get("created", "")[:10]
            label   = f"{name}  ({created})" if created else name
            act = self._recent_menu.addAction(label)
            act.triggered.connect(
                lambda checked, p=path, i=info:
                    self.experiments_tab.load_experiment(p, i)
            )

    def apply_theme(self, name: str):
        if name not in THEMES:
            return
        self._current_theme = name
        # Update checkmark
        if hasattr(self, "_theme_actions"):
            for n, act in self._theme_actions.items():
                act.setChecked(n == name)
        # Apply stylesheet + palette
        self.setStyleSheet(build_stylesheet(name))
        QApplication.instance().setPalette(build_palette(name))
        # Update the RE control bar (has its own stylesheet)
        self.re_bar.apply_theme(name)
        # Persist selection
        save_theme(name)
        self.status_bar.showMessage(f"Theme: {name}", 2000)

    def _setup_worker(self):
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        # Worker → window
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.re_manager_started.connect(self._on_re_manager_started)

        # Worker → re_bar
        self.worker.status_updated.connect(self.re_bar.update_status)
        self.worker.queue_updated.connect(
            lambda items: self.re_bar.update_queue_count(len(items))
        )

        # Worker → queue_mgr
        self.worker.queue_updated.connect(self.queue_mgr.update_queue)
        self.worker.history_updated.connect(self.queue_mgr.update_history)

        # Worker → experiments_tab
        self.worker.history_updated.connect(self.experiments_tab.update_history)
        self.worker.queue_updated.connect(self.experiments_tab.update_compact_queue)

        # Worker → devices_plans_tab
        self.worker.plans_updated.connect(self.devices_plans_tab.update_plans)
        self.worker.devices_updated.connect(self.devices_plans_tab.update_devices)

        # Worker → experiments_tab plans/devices (for PlanDialog)
        self.worker.plans_updated.connect(self.experiments_tab.set_plans)
        self.worker.devices_updated.connect(self.experiments_tab.set_devices)

        # Worker → plan/device handlers (queue_mgr + plan_builder)
        self.worker.plans_updated.connect(self._on_plans_updated)
        self.worker.devices_updated.connect(self._on_devices_updated)

        # RE bar → MainWindow action handlers
        self.re_bar.start_requested.connect(self._on_start_requested)
        self.re_bar.pause_requested.connect(self._on_pause_requested)
        self.re_bar.resume_requested.connect(self._on_resume_requested)
        self.re_bar.abort_requested.connect(self._on_abort_requested)
        self.re_bar.stop_requested.connect(self._on_stop_requested)
        self.re_bar.open_env_requested.connect(self._on_open_env_requested)
        self.re_bar.close_env_requested.connect(self._on_close_env_requested)
        self.re_bar.start_manager_requested.connect(self._on_start_manager_requested)
        self.re_bar.reconnect_requested.connect(self._on_reconnect_requested)

        # Experiments tab → MainWindow
        self.experiments_tab.experiment_changed.connect(self._on_experiment_changed)

        self.worker_thread.start()

    def _connect(self):
        poll_thread = threading.Thread(target=self.worker.poll, daemon=True)
        poll_thread.start()
        QTimer.singleShot(100, self._do_connect)

    def _do_connect(self):
        ok = self.worker.connect()
        if not ok:
            self.re_bar.set_disconnected()

    # ── Worker signal handlers ─────────────────────────────────────────────────

    def _on_connected(self):
        self.conn_label.setText("⬤  Connected")
        self.conn_label.setStyleSheet("color: #2ca02c;")
        self.status_bar.showMessage("Connected to RE Manager at " + ZMQ_CONTROL)

    def _on_re_manager_started(self, pid):
        self.conn_label.setText("⬤  RE Manager starting…")
        self.conn_label.setStyleSheet("color: #ffcc00;")
        self.status_bar.showMessage(
            f"RE Manager started (PID {pid}) — click Reconnect when ready"
        )
        self.re_bar.set_disconnected()

    def _on_disconnected(self):
        self.conn_label.setText("⬤  Disconnected")
        self.conn_label.setStyleSheet("color: #d62728;")
        self.re_bar.set_disconnected()

    def _log(self, msg: str):
        self.queue_mgr.append_console(msg)
        self.experiments_tab.append_console(msg)

    def _on_error(self, msg):
        self.conn_label.setText("⬤  Error")
        self.conn_label.setStyleSheet("color: #ff7f0e;")
        self._log(f"[ERROR] {msg}")

    def _on_plans_updated(self, plans):
        self.queue_mgr.plans = plans
        self.plan_builder.update_plans(plans)

    def _on_devices_updated(self, devices):
        self.queue_mgr.devices = devices
        self.plan_builder.update_devices(devices)

    # ── RE control action handlers ─────────────────────────────────────────────

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _on_start_requested(self):
        ready, reason = self.experiments_tab.is_ready_to_run()
        if not ready:
            QMessageBox.warning(self, "Cannot Start Queue", reason)
            return
        ok, msg = self.worker.queue_start()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Start queue: {msg}")

    def _on_pause_requested(self):
        ok, msg = self.worker.re_pause()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Pause: {msg}")

    def _on_resume_requested(self):
        ok, msg = self.worker.re_resume()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Resume: {msg}")

    def _on_abort_requested(self):
        r = QMessageBox.question(self, "Abort", "Abort the currently running plan?")
        if r != QMessageBox.StandardButton.Yes:
            return
        ok, msg = self.worker.re_abort()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Abort: {msg}")

    def _on_stop_requested(self):
        ok, msg = self.worker.re_stop()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Stop: {msg}")

    def _on_open_env_requested(self):
        ok, msg = self.worker.open_environment()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Open environment: {msg}")

    def _on_close_env_requested(self):
        ok, msg = self.worker.close_environment()
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} Close environment: {msg}")

    def _on_start_manager_requested(self):
        ok = self.worker.start_re_manager()
        if ok:
            self._log(f"[{self._ts()}] ✓ RE Manager starting — reconnecting in 5 s…")
            QTimer.singleShot(5000, self._auto_reconnect)
        else:
            self._log(f"[{self._ts()}] ✗ Start RE Manager failed")

    def _auto_reconnect(self):
        self._log(f"[{self._ts()}] Auto-reconnecting…")
        ok = self.worker.connect()
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected")
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Still starting — click Reconnect when ready")

    def _on_reconnect_requested(self):
        self._log(f"[{self._ts()}] Reconnecting to RE Manager…")
        ok = self.worker.connect()
        if ok:
            self._log(f"[{self._ts()}] ✓ Reconnected")
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Reconnect failed — RE Manager may still be starting")

    def _on_experiment_changed(self, runs_dir: str):
        self._log(f"[{self._ts()}] ✓ Active experiment changed → {runs_dir}")
        self._refresh_recent_menu()

    def closeEvent(self, event):
        self.worker.stop()
        self.worker.stop_re_manager()
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EasyBluesky")
    app.setStyle("Fusion")

    # Initial palette set from saved theme; MainWindow.apply_theme() updates it after startup
    app.setPalette(build_palette(load_saved_theme()))

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
