"""main.py — MainWindow and application entry point."""

import sys
import threading
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, QThread, QTimer
from .config import APP_NAME, ACCENT
from .connection_settings import (
    load_connection, save_connection, make_zmq_addrs,
    get_active_profile, ConnectionDialog, is_local_host,
    profile_slug, delete_profile, restore_profile,
    purge_old_deleted, find_free_ports, _all_used_ports,
)
from .sim_generator import generate_sim_script
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
from .hdf5_viewer import HDF5Viewer
from .re_console import REConsoleWidget


# ── Single-instance guard (one app per profile) ────────────────────────────────

class SingleInstanceGuard:
    """Uses QLocalServer to enforce one app instance per profile name."""

    def __init__(self):
        self._server = None
        self._current_name = None

    def try_acquire(self, profile_name: str) -> bool:
        """Try to claim exclusive lock for profile. Returns True if acquired."""
        try:
            from PyQt6.QtNetwork import QLocalServer, QLocalSocket
        except ImportError:
            return True  # QtNetwork not available — skip locking

        name = f"easy-bluesky-{profile_slug(profile_name)}"

        # Check if another instance already holds this lock
        sock = QLocalSocket()
        sock.connectToServer(name)
        already_held = sock.waitForConnected(200)
        sock.close()
        if already_held:
            return False

        # Release previous lock (profile switch)
        if self._server:
            self._server.close()
            from PyQt6.QtNetwork import QLocalServer as _LS
            _LS.removeServer(self._current_name or "")

        from PyQt6.QtNetwork import QLocalServer
        QLocalServer.removeServer(name)  # clean stale socket from crash
        self._server = QLocalServer()
        if not self._server.listen(name):
            return False
        self._current_name = name
        return True

    def release(self):
        try:
            from PyQt6.QtNetwork import QLocalServer
            if self._server:
                self._server.close()
            if self._current_name:
                QLocalServer.removeServer(self._current_name)
        except Exception:
            pass
        self._server = None
        self._current_name = None

    def locked_profiles(self, profile_names: list) -> set:
        """Return names of profiles locked by OTHER instances (not this one)."""
        try:
            from PyQt6.QtNetwork import QLocalSocket
        except ImportError:
            return set()
        locked = set()
        for name in profile_names:
            slug_name = f"easy-bluesky-{profile_slug(name)}"
            if slug_name == self._current_name:
                continue  # we hold this one
            sock = QLocalSocket()
            sock.connectToServer(slug_name)
            if sock.waitForConnected(100):
                sock.close()
                locked.add(name)
            sock.close()
        return locked


# ── Profile picker dialog helpers ──────────────────────────────────────────────

class _DeleteConfirmDialog(QDialog):
    """Require the user to type the profile name exactly before deleting."""

    def __init__(self, profile_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Delete Profile")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)

        msg = QLabel(
            f"This will delete profile <b>{profile_name}</b>.<br>"
            "It can be recovered from <i>Restore Deleted…</i> for 30 days."
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)
        layout.addSpacing(8)

        layout.addWidget(QLabel(f"Type  <b>{profile_name}</b>  to confirm:"))
        self._input = QLineEdit()
        self._input.setPlaceholderText(profile_name)
        layout.addWidget(self._input)

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self.accept)
        self._btn_delete.setEnabled(False)
        self._btn_delete.setStyleSheet("color: #d62728; font-weight: bold;")
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self._btn_delete)
        layout.addLayout(btn_row)

        self._profile_name = profile_name
        self._input.textChanged.connect(
            lambda t: self._btn_delete.setEnabled(t == self._profile_name)
        )


class _NewProfileDialog(QDialog):
    """Mini dialog to create a new profile from the picker."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Profile")
        self.setMinimumWidth(380)
        self._settings = settings
        self.profile_name = ""
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setHorizontalSpacing(12)

        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. ASWAXS, SURF, Local Sim")
        form.addRow("Name:", self._name)

        self._is_local = QCheckBox("Local (runs on this computer)")
        form.addRow("", self._is_local)

        self._devices = QLineEdit("devices.py")
        form.addRow("Devices file:", self._devices)

        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_accept(self):
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Required", "Please enter a profile name.")
            return
        if any(p.get("name") == name for p in self._settings.get("profiles", [])):
            QMessageBox.warning(self, "Duplicate", f"A profile named '{name}' already exists.")
            return

        used = _all_used_ports(self._settings)
        start = (max(used) + 1) if used else 60615
        ports = find_free_ports(4, start, used)

        new_profile = {
            "name": name,
            "devices_file": self._devices.text().strip() or "devices.py",
            "is_local": self._is_local.isChecked(),
            "control_port":  ports[0] if len(ports) > 0 else 60700,
            "info_port":     ports[1] if len(ports) > 1 else 60701,
            "doc_port":      ports[2] if len(ports) > 2 else 60702,
            "procserv_port": ports[3] if len(ports) > 3 else 60703,
        }
        self._settings.setdefault("profiles", []).append(new_profile)
        self.profile_name = name
        self.accept()


class _RestoreDialog(QDialog):
    """Show deleted profiles and let the user pick one to restore."""

    def __init__(self, deleted_profiles: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restore Deleted Profile")
        self.setMinimumWidth(420)
        self.selected_entry = None
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Select a profile to restore:"))
        self._list = QListWidget()
        for entry in reversed(deleted_profiles):  # most recent first
            name = entry.get("name", "Unknown")
            ts = entry.get("_deleted_at", "")
            try:
                dt = datetime.fromisoformat(ts)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_str = ts[:16]
            item = QListWidgetItem(f"{name}  — deleted {ts_str}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._list.addItem(item)
        layout.addWidget(self._list)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = btns.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("Restore")
        self._ok.setEnabled(False)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._list.currentItemChanged.connect(
            lambda cur, _: self._ok.setEnabled(cur is not None)
        )
        self._list.itemDoubleClicked.connect(lambda _: self._on_accept())

    def _on_accept(self):
        item = self._list.currentItem()
        if item:
            self.selected_entry = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


# ── Profile picker ─────────────────────────────────────────────────────────────

class ProfilePickerDialog(QDialog):
    """
    Startup dialog — user picks which profile to launch.

    Locked profiles (held by another running instance) are shown greyed out
    with "(already running)" and cannot be selected.
    """

    def __init__(self, settings: dict, guard: SingleInstanceGuard, parent=None):
        super().__init__(parent)
        self.setWindowTitle("EasyBluesky — Select Profile")
        self.setMinimumWidth(480)
        self.setMinimumHeight(300)
        self._settings = settings
        self._guard = guard
        self.selected_profile = None

        profiles = settings.get("profiles", [])
        self._locked = guard.locked_profiles([p.get("name", "") for p in profiles])

        self._build_ui()
        self._populate_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        lbl = QLabel("Select a profile to launch:")
        layout.addWidget(lbl)

        self._list = QListWidget()
        self._list.setMinimumHeight(160)
        self._list.itemDoubleClicked.connect(self._on_launch)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list)

        # Bottom button row
        btn_row = QHBoxLayout()

        self._btn_restore = QPushButton("Restore Deleted…")
        self._btn_restore.clicked.connect(self._on_restore)
        btn_row.addWidget(self._btn_restore)

        self._btn_new = QPushButton("New Profile")
        self._btn_new.clicked.connect(self._on_new)
        btn_row.addWidget(self._btn_new)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btn_row.addWidget(self._btn_delete)

        btn_row.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self._btn_launch = QPushButton("Launch")
        self._btn_launch.clicked.connect(self._on_launch)
        self._btn_launch.setDefault(True)
        self._btn_launch.setEnabled(False)
        btn_row.addWidget(self._btn_launch)

        layout.addLayout(btn_row)

        self._btn_restore.setEnabled(bool(self._settings.get("deleted_profiles", [])))

    def _populate_list(self):
        self._list.clear()
        profiles = self._settings.get("profiles", [])
        first_selectable = None
        for p in profiles:
            name = p.get("name", "Unknown")
            is_local = p.get("is_local", False)
            locked = name in self._locked

            if locked:
                label = f"{name}  (already running)"
            elif is_local:
                label = f"{name}  [LOCAL]"
            else:
                label = name

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)

            if locked:
                item.setFlags(
                    item.flags()
                    & ~Qt.ItemFlag.ItemIsEnabled
                    & ~Qt.ItemFlag.ItemIsSelectable
                )
                item.setForeground(Qt.GlobalColor.gray)
            elif first_selectable is None:
                first_selectable = item

            self._list.addItem(item)

        if first_selectable:
            self._list.setCurrentItem(first_selectable)

    def _on_selection_changed(self, current, previous):
        enabled = (
            current is not None
            and bool(current.flags() & Qt.ItemFlag.ItemIsEnabled)
        )
        self._btn_launch.setEnabled(enabled)
        self._btn_delete.setEnabled(enabled)

    def _on_launch(self):
        item = self._list.currentItem()
        if not item or not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        for p in self._settings.get("profiles", []):
            if p.get("name") == name:
                self.selected_profile = p
                break
        if self.selected_profile:
            self.accept()

    def _on_delete(self):
        item = self._list.currentItem()
        if not item:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        profiles = self._settings.get("profiles", [])
        if len(profiles) <= 1:
            QMessageBox.warning(self, "Cannot Delete", "Cannot delete the last profile.")
            return

        dlg = _DeleteConfirmDialog(name, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        delete_profile(self._settings, name)
        save_connection(self._settings)
        self._refresh()

    def _on_new(self):
        dlg = _NewProfileDialog(self._settings, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        save_connection(self._settings)
        self._refresh()
        # Select the new profile
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == dlg.profile_name:
                self._list.setCurrentItem(item)
                break

    def _on_restore(self):
        deleted = self._settings.get("deleted_profiles", [])
        if not deleted:
            return
        dlg = _RestoreDialog(deleted, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_entry:
            return
        restore_profile(self._settings, dlg.selected_entry)
        save_connection(self._settings)
        self._refresh()

    def _refresh(self):
        profiles = self._settings.get("profiles", [])
        self._locked = self._guard.locked_profiles([p.get("name", "") for p in profiles])
        self._populate_list()
        self._btn_restore.setEnabled(bool(self._settings.get("deleted_profiles", [])))


# ── First-run helper ───────────────────────────────────────────────────────────

def _create_first_run_profile(settings: dict):
    """Create a 'Local Sim' profile for first-time users with no profiles."""
    from .worker import _get_scripts_dir
    scripts_dir = _get_scripts_dir()
    devices_sim = scripts_dir / "devices_sim.py"
    if not devices_sim.exists():
        try:
            generate_sim_script(scripts_dir / "re_startup_mongo.py", devices_sim)
        except Exception:
            pass

    ports = find_free_ports(4, 60615)
    profile = {
        "name": "Local Sim",
        "devices_file": "devices_sim.py",
        "is_local": True,
        "control_port":  ports[0] if len(ports) > 0 else 60615,
        "info_port":     ports[1] if len(ports) > 1 else 60625,
        "doc_port":      ports[2] if len(ports) > 2 else 60630,
        "procserv_port": ports[3] if len(ports) > 3 else 60635,
    }
    settings["profiles"] = [profile]
    settings["active_profile"] = "Local Sim"
    settings.setdefault("deleted_profiles", [])


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, guard: SingleInstanceGuard = None):
        super().__init__()
        self.setWindowTitle("EasyBluesky")
        self.setMinimumSize(1200, 800)
        self._current_theme = load_saved_theme()
        self._conn_settings = load_connection()
        self._guard = guard
        self.worker = ZMQWorker()
        self._setup_ui()
        self._setup_worker()
        self._connect()
        self.apply_theme(self._current_theme)

    def _setup_ui(self):
        self.setStyleSheet(build_stylesheet(self._current_theme))

        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        self.experiments_tab    = ExperimentsTab(self.worker)
        self.queue_mgr          = QueueManager(self.worker)
        self.plan_builder       = PlanBuilder(self.worker)
        self.devices_plans_tab  = DevicesPlansTab()
        self.hdf5_viewer        = HDF5Viewer()
        self.re_console         = REConsoleWidget()

        self.tabs.addTab(self.experiments_tab,   "🧪  Experiments")
        self.tabs.addTab(self.queue_mgr,         "⚙  Queue Manager")
        self.tabs.addTab(self.plan_builder,      "🔧  Plan Builder")
        self.tabs.addTab(self.devices_plans_tab, "🔬  Devices & Plans")
        self.tabs.addTab(self.hdf5_viewer,       "🗄  HDF5 Viewer")
        self.tabs.addTab(self.re_console,        "🖥  RE Console")

        self.re_bar = REControlBar()

        central = QWidget()
        vlay = QVBoxLayout(central)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(self.re_bar)
        vlay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._build_menu()

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.conn_label = QLabel("⬤  Connecting...")
        self.conn_label.setStyleSheet("color: #ffcc00;")
        self.status_bar.addPermanentWidget(self.conn_label)
        ctrl_addr, _, _ = make_zmq_addrs(self._conn_settings)
        self.status_bar.showMessage("EasyBluesky  |  ZMQ: " + ctrl_addr)

        profiles = self._conn_settings.get("profiles", [])
        names = [p.get("name", "") for p in profiles]
        active = self._conn_settings.get("active_profile", "Default")
        self.re_bar.update_profiles(names, active)

    def _build_menu(self):
        from PyQt6.QtGui import QActionGroup
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        act_conn = file_menu.addAction("Connection Settings…")
        act_conn.triggered.connect(self._on_connection_settings)
        file_menu.addSeparator()
        act_edit_dev = file_menu.addAction("Edit Devices File…")
        act_edit_dev.triggered.connect(self._on_edit_devices)
        act_gen_sim = file_menu.addAction("Generate Sim Devices…")
        act_gen_sim.triggered.connect(self._on_generate_sim_script)
        file_menu.addSeparator()
        self._recent_menu = file_menu.addMenu("Recent Experiments")
        self._refresh_recent_menu()
        file_menu.addSeparator()
        act_open_h5 = file_menu.addAction("Open HDF5 Export…")
        act_open_h5.triggered.connect(self._on_open_hdf5)

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
        if hasattr(self, "_theme_actions"):
            for n, act in self._theme_actions.items():
                act.setChecked(n == name)
        self.setStyleSheet(build_stylesheet(name))
        QApplication.instance().setPalette(build_palette(name))
        self.re_bar.apply_theme(name)
        save_theme(name)
        self.status_bar.showMessage(f"Theme: {name}", 2000)

    def _setup_worker(self):
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.re_manager_started.connect(self._on_re_manager_started)

        self.worker.status_updated.connect(self.re_bar.update_status)
        self.worker.queue_updated.connect(
            lambda items: self.re_bar.update_queue_count(len(items))
        )

        self.worker.queue_updated.connect(self.queue_mgr.update_queue)
        self.worker.history_updated.connect(self.queue_mgr.update_history)

        self.worker.history_updated.connect(self.experiments_tab.update_history)
        self.worker.queue_updated.connect(self.experiments_tab.update_compact_queue)

        self.worker.plans_updated.connect(self.devices_plans_tab.update_plans)
        self.worker.devices_updated.connect(self.devices_plans_tab.update_devices)

        self.worker.console_updated.connect(self.re_console.append)
        self.worker.connected.connect(self.re_console.on_connected)
        self.worker.disconnected.connect(self.re_console.on_disconnected)
        self.re_console.diagnose_requested.connect(self._on_console_diagnose)

        self.worker.plans_updated.connect(self.experiments_tab.set_plans)
        self.worker.devices_updated.connect(self.experiments_tab.set_devices)

        self.worker.plans_updated.connect(self._on_plans_updated)
        self.worker.devices_updated.connect(self._on_devices_updated)

        self.re_bar.start_requested.connect(self._on_start_requested)
        self.re_bar.pause_requested.connect(self._on_pause_requested)
        self.re_bar.resume_requested.connect(self._on_resume_requested)
        self.re_bar.abort_requested.connect(self._on_abort_requested)
        self.re_bar.stop_requested.connect(self._on_stop_requested)
        self.re_bar.open_env_requested.connect(self._on_open_env_requested)
        self.re_bar.close_env_requested.connect(self._on_close_env_requested)
        self.re_bar.start_manager_requested.connect(self._on_start_manager_requested)
        self.re_bar.stop_manager_requested.connect(self._on_stop_manager_requested)
        self.re_bar.reconnect_requested.connect(self._on_reconnect_requested)
        self.re_bar.profile_changed.connect(self._on_profile_changed)

        self.experiments_tab.experiment_changed.connect(self._on_experiment_changed)

        self.worker_thread.start()

    def _connect(self):
        poll_thread = threading.Thread(target=self.worker.poll, daemon=True)
        poll_thread.start()
        QTimer.singleShot(100, self._do_connect)

    def _do_connect(self):
        ctrl, info, _ = make_zmq_addrs(self._conn_settings)
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info)
        if not ok:
            self.re_bar.set_disconnected()

    # ── Worker signal handlers ─────────────────────────────────────────────────

    def _on_connected(self):
        self.conn_label.setText("⬤  Connected")
        self.conn_label.setStyleSheet("color: #2ca02c;")
        ctrl_addr, _, _ = make_zmq_addrs(self._conn_settings)
        self.status_bar.showMessage("Connected to RE Manager at " + ctrl_addr)
        # If this is an SSH-managed instance, tail the procServ log file so
        # that worker stdout (startup script output, plan progress) reaches the
        # RE Console regardless of whether the manager publishes to ZMQ.
        profile   = get_active_profile(self._conn_settings)
        use_local = profile.get("is_local", False) or is_local_host(self._conn_settings)
        if not use_local:
            from .ssh_manager import _instance_files
            _, log_file, _ = _instance_files(profile.get("name", "Default"))
            self.worker.start_log_tail(self._conn_settings, log_file)

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
        self.worker.stop_log_tail()

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
        settings = self._conn_settings
        profile = get_active_profile(settings)
        use_local = profile.get("is_local", False) or is_local_host(settings)
        if use_local:
            ok = self.worker.start_re_manager(profile)
            if ok:
                self._log(
                    f"[{self._ts()}] ✓ RE Manager (profile: {profile['name']}) "
                    f"starting — reconnecting in 5 s…"
                )
                QTimer.singleShot(5000, self._auto_reconnect_mode)
            else:
                self._log(f"[{self._ts()}] ✗ Start RE Manager failed")
        else:
            host = settings["host"]
            self._log(
                f"[{self._ts()}] SSH → restarting RE Manager "
                f"(profile: {profile['name']}) on {host}…"
            )
            threading.Thread(
                target=self._ssh_restart_remote,
                args=(settings,),
                daemon=True,
            ).start()

    def _on_stop_manager_requested(self):
        settings = self._conn_settings
        profile = get_active_profile(settings)
        use_local = profile.get("is_local", False) or is_local_host(settings)
        if use_local:
            self.worker.stop_re_manager()
            self.worker.disconnect()
            self._log(f"[{self._ts()}] RE Manager stopped")
        else:
            host = settings["host"]
            self._log(f"[{self._ts()}] SSH → stopping RE Manager on {host}…")
            threading.Thread(
                target=self._ssh_stop_remote,
                args=(settings, profile),
                daemon=True,
            ).start()

    def _ssh_stop_remote(self, settings: dict, profile: dict):
        from .ssh_manager import stop_re_manager
        ok, msg = stop_re_manager(settings, profile)
        self._log(f"[{self._ts()}] {'✓' if ok else '✗'} {msg}")
        if ok:
            self.worker.disconnect()

    def _ssh_restart_remote(self, settings: dict):
        from .ssh_manager import restart_re_manager, wait_for_port
        profile = get_active_profile(settings)
        ok, msg = restart_re_manager(settings, profile)
        ts = self._ts()
        if not ok:
            self._log(f"[{ts}] ✗ SSH restart failed: {msg}")
            return
        self._log(f"[{ts}] ✓ {msg} — waiting for port to open…")
        ctrl, _, _ = make_zmq_addrs(settings)
        port = int(ctrl.rsplit(":", 1)[-1])
        ready = wait_for_port(settings["host"], port, timeout=30)
        if ready:
            self._log(f"[{self._ts()}] Port {port} open — reconnecting…")
            QTimer.singleShot(500, self._auto_reconnect_mode)
        else:
            self._log(f"[{self._ts()}] ✗ RE Manager did not open port {port} within 30 s")

    def _auto_reconnect(self):
        self._log(f"[{self._ts()}] Auto-reconnecting…")
        ok = self.worker.connect()
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected")
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Still starting — click Reconnect when ready")

    def _auto_reconnect_mode(self):
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        self._log(f"[{self._ts()}] Auto-reconnecting to {ctrl}…")
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info)
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected")
            self.experiments_tab.live_viewer.restart_zmq(doc)
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Still starting — click Reconnect when ready")

    def _on_reconnect_requested(self):
        self._log(f"[{self._ts()}] Reconnecting to RE Manager…")
        ctrl, info, _ = make_zmq_addrs(self._conn_settings)
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info)
        if ok:
            self._log(f"[{self._ts()}] ✓ Reconnected")
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Reconnect failed — RE Manager may still be starting")

    def _on_profile_changed(self, name: str):
        # Block switch if another instance already holds this profile
        if self._guard and not self._guard.try_acquire(name):
            QMessageBox.warning(
                self, "Profile In Use",
                f"Profile '{name}' is already open in another window on this computer."
            )
            # Revert combo to current profile
            current = self._conn_settings.get("active_profile", "Default")
            profiles = self._conn_settings.get("profiles", [])
            names = [p.get("name", "") for p in profiles]
            self.re_bar.update_profiles(names, current)
            return

        self._conn_settings["active_profile"] = name
        save_connection(self._conn_settings)
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        self._log(f"[{self._ts()}] Switching to profile '{name}' → {ctrl}")
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info)
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected to profile '{name}'")
        else:
            self._log(
                f"[{self._ts()}] ✗ Profile '{name}' RE Manager not running at {ctrl}\n"
                f"              not running — click Start RE Mgr to start it"
            )
            self.re_bar.set_disconnected()
        self.experiments_tab.live_viewer.restart_zmq(doc)

    def _on_open_hdf5(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Open HDF5 Archive", "", "HDF5 Files (*.h5 *.hdf5)"
        )
        if not path:
            return
        self.hdf5_viewer.load_file(path)
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is self.hdf5_viewer:
                self.tabs.setCurrentIndex(i)
                break

    def _on_console_diagnose(self):
        settings = self._conn_settings
        profile  = get_active_profile(settings)
        _, info, _ = make_zmq_addrs(settings)
        use_local  = profile.get("is_local", False) or is_local_host(settings)
        self.re_console.append("[EasyBluesky] Running console diagnostics…\n")
        threading.Thread(
            target=self._run_console_diagnostics,
            args=(settings, profile, info, use_local),
            daemon=True,
        ).start()

    def _run_console_diagnostics(self, settings, profile, info_addr, use_local):
        lines = []
        info_port    = profile.get("info_port", 60625)
        profile_name = profile.get("name", "Default")

        if not use_local:
            # SSH: check running process flags, port binding, and log tail
            try:
                from .ssh_manager import _get_client, _instance_files
                _, log_file, _ = _instance_files(profile_name)
                client = _get_client(settings)
                _, stdout, _ = client.exec_command(
                    f"ps -o pid,args -p $(pgrep -f 'start-re-manager') 2>/dev/null || "
                    f"ps aux | grep 'start-re-manager' | grep -v grep",
                    timeout=10,
                )
                proc = stdout.read().decode().strip()
                _, stdout2, _ = client.exec_command(
                    f"ss -tlnp 2>/dev/null | grep ':{info_port}' || "
                    f"netstat -tlnp 2>/dev/null | grep ':{info_port}'",
                    timeout=10,
                )
                port = stdout2.read().decode().strip()
                _, stdout3, _ = client.exec_command(
                    f"tail -n 40 {log_file} 2>/dev/null", timeout=10,
                )
                log_tail = stdout3.read().decode().strip()
                client.close()

                lines.append("[Diagnose] SSH process check:\n")
                lines.append(f"  {proc or '(start-re-manager not found in process list)'}\n")
                lines.append(f"[Diagnose] Port {info_port} on remote:\n")
                lines.append(f"  {port or f'(nothing bound to port {info_port})'}\n")

                if proc and "--zmq-publish-console ON" not in proc:
                    lines.append(
                        "  ✗ --zmq-publish-console ON not found in process args.\n"
                        "    Console output will not be published.\n"
                    )
                elif proc:
                    lines.append("  ✓ --zmq-publish-console ON is present.\n")

                lines.append(f"[Diagnose] RE Manager log — last 40 lines of {log_file}:\n")
                if log_tail:
                    for ln in log_tail.split("\n"):
                        lines.append(f"  {ln}\n")
                else:
                    lines.append(f"  (log file not found or empty)\n")
            except Exception as e:
                lines.append(f"[Diagnose] SSH check failed: {e}\n")

        # ZMQ live test — open environment before clicking Diagnose for best results
        lines.append(f"[Diagnose] ZMQ live test on {info_addr} (6 s):\n")
        zmq_result = self.worker.diagnose_console(info_addr, duration=6.0)
        lines.append(zmq_result)

        self.worker.console_updated.emit("".join(lines))

    def _on_edit_devices(self):
        from .devices_editor import DevicesEditorDialog
        dlg = DevicesEditorDialog(self._conn_settings, self)
        dlg.exec()

    def _on_generate_sim_script(self):
        from easy_bluesky.worker import _get_scripts_dir
        scripts_dir  = _get_scripts_dir()
        real_script  = scripts_dir / "re_startup_mongo.py"
        sim_devices  = scripts_dir / "devices_sim.py"
        if not real_script.exists():
            QMessageBox.warning(self, "Not Found",
                f"Real startup script not found:\n{real_script}\n\n"
                "Create it first, then generate the sim devices file.")
            return
        try:
            out = generate_sim_script(real_script, sim_devices)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to generate sim devices file:\n{e}")
            return

        msg = (
            f"Simulated devices file written to:\n{out}\n\n"
            f"Review and edit as needed.\n\n"
            f"To use simulation, open Connection Settings and create or edit a profile "
            f"with 'Devices file' set to 'devices_sim.py'."
        )

        settings = self._conn_settings
        profile = get_active_profile(settings)
        if not profile.get("is_local", False) and not is_local_host(settings):
            r = QMessageBox.question(
                self, "Copy to Remote?",
                f"Copy the devices file to the remote RE Manager host?\n\n"
                f"  {settings['host']}:~/.easy_bluesky/scripts/devices_sim.py",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if r == QMessageBox.StandardButton.Yes:
                ok, sftp_msg = self._sftp_upload_sim_script(sim_devices, settings)
                msg += f"\n\n{'✓' if ok else '✗'} {sftp_msg}"

        QMessageBox.information(self, "Sim Devices Generated", msg)

    def _sftp_upload_sim_script(self, local_path, settings: dict) -> tuple:
        try:
            from .ssh_manager import _get_client
            client = _get_client(settings)
            sftp = client.open_sftp()
            remote_path = ".easy_bluesky/scripts/devices_sim.py"
            try:
                sftp.stat(".easy_bluesky/scripts")
            except FileNotFoundError:
                try:
                    sftp.stat(".easy_bluesky")
                except FileNotFoundError:
                    sftp.mkdir(".easy_bluesky")
                sftp.mkdir(".easy_bluesky/scripts")
            sftp.put(str(local_path), remote_path)
            sftp.close()
            client.close()
            return True, f"Copied to {settings['host']}:~/{remote_path}"
        except Exception as e:
            return False, f"SFTP upload failed: {e}"

    def _on_connection_settings(self):
        dlg = ConnectionDialog(self)
        if dlg.exec() != ConnectionDialog.DialogCode.Accepted:
            return
        self._conn_settings = dlg.get_settings()
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        self.status_bar.showMessage(f"Reconnecting to {self._conn_settings['host']}…")
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info)
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected to {self._conn_settings['host']}")
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Connection failed — check host and ports")
        self.experiments_tab.live_viewer.restart_zmq(doc)
        profiles = self._conn_settings.get("profiles", [])
        names = [p.get("name", "") for p in profiles]
        active = self._conn_settings.get("active_profile", "Default")
        self.re_bar.update_profiles(names, active)

    def _on_experiment_changed(self, runs_dir: str):
        self._log(f"[{self._ts()}] ✓ Active experiment changed → {runs_dir}")
        self._refresh_recent_menu()

    def closeEvent(self, event):
        self.worker.stop()
        # Stop RE Manager only if the active profile is local
        profile = get_active_profile(self._conn_settings)
        if profile.get("is_local", False):
            self.worker.stop_re_manager()
        # Release profile lock
        if self._guard:
            self._guard.release()
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
        event.accept()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EasyBluesky")
    app.setStyle("Fusion")
    app.setPalette(build_palette(load_saved_theme()))

    # Ensure scripts directory exists
    from .worker import _get_scripts_dir
    _get_scripts_dir()

    settings = load_connection()

    # Auto-create a Local Sim profile on very first run
    if not settings.get("profiles"):
        _create_first_run_profile(settings)
        save_connection(settings)

    # Remove deleted profiles older than 30 days
    purge_old_deleted(settings)

    # Show profile picker
    guard = SingleInstanceGuard()
    picker = ProfilePickerDialog(settings, guard)
    if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected_profile:
        sys.exit(0)

    selected = picker.selected_profile
    settings["active_profile"] = selected["name"]
    save_connection(settings)

    win = MainWindow(guard=guard)
    win.show()

    # Auto-start RE Manager for local profiles
    if selected.get("is_local", False):
        QTimer.singleShot(800, win._on_start_manager_requested)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
