"""main.py — MainWindow and application entry point."""

import sys
import threading
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QStatusBar, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from .config import APP_NAME, ACCENT
from .connection_settings import (
    load_connection, save_connection, make_zmq_addrs,
    get_active_profile, ConnectionDialog, is_local_host,
    profile_slug, delete_profile, restore_profile,
    purge_old_deleted, find_free_ports, _all_used_ports,
    apply_epics_env,
)
from .sim_generator import generate_sim_script
from .themes import (
    build_stylesheet, build_palette, load_saved_theme, save_theme,
    theme_names, THEMES,
)
from .worker import ZMQWorker
from .registry import fetch_registry, merge_into_profiles, probe_all_instances
from .re_control_bar import REControlBar
from .queue_manager import QueueManager
from .plan_builder import PlanBuilder
from .experiments_tab import ExperimentsTab
from .devices_plans_tab import DevicesPlansTab
from .pv_watchdog import PVWatchdogTab
from .mongo_browser import MongoDataBrowserTab
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


# ── Help dialog ───────────────────────────────────────────────────────────────

_HELP_CSS = """
body  { font-family: -apple-system, sans-serif; font-size: 13px;
        margin: 12px 16px; line-height: 1.55; }
h2    { color: #1f77b4; margin-top: 0; border-bottom: 1px solid #444;
        padding-bottom: 4px; }
h3    { color: #aaa; margin-bottom: 4px; margin-top: 14px; }
code  { background: #2d2d2d; color: #e8c07d; padding: 1px 5px;
        border-radius: 3px; font-size: 12px; }
pre   { background: #2d2d2d; color: #ccc; padding: 8px 10px;
        border-radius: 4px; font-size: 12px; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
th    { background: #1f77b4; color: white; padding: 5px 8px;
        text-align: left; font-weight: bold; }
td    { padding: 4px 8px; border-bottom: 1px solid #333; vertical-align: top; }
tr:nth-child(even) td { background: #252525; }
.tip  { background: #1a2e1a; border-left: 3px solid #2ca02c;
        padding: 6px 10px; border-radius: 3px; margin: 8px 0; }
.warn { background: #2e1a1a; border-left: 3px solid #d62728;
        padding: 6px 10px; border-radius: 3px; margin: 8px 0; }
"""

_QUICK_START_HTML = """
<h2>Getting Started — New User Tutorial</h2>

<p>EasyBluesky is a desktop GUI for controlling a Bluesky/ophyd beamline via the
bluesky-queueserver (RE Manager).  Your client machine (Windows or Mac) talks to a
Linux beamline computer over SSH and ZMQ.  This tutorial walks you through
first-time setup to running your first experiment.</p>

<h3>Step 1 — Configure the connection</h3>
<p>Open <b>File → Connection Settings</b>.</p>
<table>
<tr><th>Field</th><th>What to enter</th></tr>
<tr><td><b>Host</b></td><td>IP address or hostname of the beamline computer
    (e.g. <code>164.54.169.92</code>)</td></tr>
<tr><td><b>SSH user</b></td><td>Your Linux account on the beamline computer
    (e.g. <code>chem_epics</code>)</td></tr>
<tr><td><b>SSH key path</b></td><td>Leave as default (<code>~/.ssh/id_ed25519</code>)
    or click <b>Setup SSH Key…</b> to generate and install one automatically</td></tr>
<tr><td><b>Conda environment</b></td><td>Name of the conda env that contains
    <code>start-re-manager</code>, e.g. <code>easy-bluesky</code></td></tr>
<tr><td><b>Conda path</b></td><td>Path to conda on the remote machine,
    e.g. <code>~/anaconda3</code></td></tr>
</table>
<div class="tip"><b>SSH Key Setup (one-time):</b> Click <b>Setup SSH Key…</b>,
enter your Linux password when prompted — it is used once and never stored.
The key is installed on the remote machine automatically.
Then click <b>Test SSH Connection</b> to confirm it works.</div>

<h3>Step 2 — Configure a profile</h3>
<p>In the <b>Profiles</b> section of Connection Settings, select or create a profile
for your beamline.  Each profile has its own ports and devices file.</p>
<table>
<tr><th>Field</th><th>Typical value</th></tr>
<tr><td><b>Name</b></td><td>A short label, e.g. <code>ASWAXS</code></td></tr>
<tr><td><b>Devices file</b></td><td>e.g. <code>devices_ASWAXS.py</code></td></tr>
<tr><td><b>Control port</b></td><td><code>60615</code> (default)</td></tr>
<tr><td><b>Info port</b></td><td><code>60625</code> (default)</td></tr>
</table>
<p>Click <b>Auto-assign Ports</b> if you are not sure which ports are free.
Click <b>OK</b> to save.</p>
<div class="warn"><b>Common mistake:</b> Leaving the Info port at a wrong value
(e.g. 60616 instead of 60625) means the RE Console never receives output
and the app keeps thinking the manager is unresponsive.</div>

<h3>Step 3 — Start the RE Manager</h3>
<p>Select your profile in the toolbar dropdown, then click the
<b>Restart RE Manager</b> button (recycle icon).  The app will:</p>
<ol>
<li>SSH into the beamline computer</li>
<li>Kill any existing RE Manager process</li>
<li>Upload the latest startup scripts</li>
<li>Start a fresh RE Manager instance</li>
</ol>
<p>Watch the <b>RE Console</b> tab — you should see the manager starting up
and loading your devices file within 10–20 seconds.</p>
<div class="tip">If you see <code>start-re-manager: not found</code>, the
Conda environment or path is wrong.  Double-check Step 1.</div>

<h3>Step 4 — Connect</h3>
<p>Once the RE Manager is running, click <b>Connect</b> in the toolbar.
The status indicator turns green and shows <b>disconnected → idle</b>.</p>
<p>Then click <b>Open Env</b>.  This loads your devices into the RE environment.
Wait for the status to show <b>idle</b> — devices are now ready.</p>
<div class="tip">Open the <b>Devices &amp; Plans</b> tab to confirm your motors
and detectors appear with live values.  If the list is empty, check the
RE Console for import errors in your devices file.</div>

<h3>Step 5 — Create an experiment</h3>
<p>Go to the <b>Experiments</b> tab and click <b>New Experiment</b>.
Give it a name and choose a save folder.  All run data from this session
is stored there as JSONL files.</p>

<h3>Step 6 — Add plans to the queue</h3>
<p>Go to the <b>Queue Manager</b> tab:</p>
<ol>
<li>Choose a plan from the dropdown (e.g. <code>count</code>, <code>scan</code>)</li>
<li>Fill in the parameters — detectors, motors, positions, number of points</li>
<li>Click <b>Add to Queue</b></li>
<li>Repeat to build up a sequence of plans</li>
</ol>
<div class="tip">Use the <b>Plan Builder</b> tab for more complex sequences.
The Visual Composer lets you drag blocks (Move, Scan, Sleep…) into a sequence
and generates the Python code automatically.</div>

<h3>Step 7 — Run the queue</h3>
<p>Click <b>▶ Start</b> in the toolbar.  Plans execute one by one.
The RE Console shows live output including the scan table from
BestEffortCallback.</p>
<table>
<tr><th>Button</th><th>Action</th></tr>
<tr><td><b>⏸ Pause</b></td><td>Pause after the current point finishes</td></tr>
<tr><td><b>▶▶ Resume</b></td><td>Continue from where it paused</td></tr>
<tr><td><b>■ Stop</b></td><td>Stop cleanly after the current plan</td></tr>
<tr><td><b>✕ Abort</b></td><td>Abort immediately (devices may be left mid-move)</td></tr>
</table>

<h3>Step 8 — View your data</h3>
<p>In the <b>Experiments</b> tab, completed runs appear in the history panel
on the right.  Click a run to see its metadata and detector readings.
Data files are saved to your experiment folder and can be opened with any
JSONL-aware tool or the built-in <b>HDF5 Viewer</b> tab (for area detector data).</p>

<h3>Troubleshooting</h3>
<table>
<tr><th>Symptom</th><th>Likely cause</th><th>Fix</th></tr>
<tr><td>Status stuck at "closed"</td><td>Devices file has a Python error</td>
    <td>Check RE Console, fix the error, click Restart RE Manager</td></tr>
<tr><td>Devices list empty</td><td>Wrong devices file or import error</td>
    <td>Check File → Edit Devices File; fix import errors</td></tr>
<tr><td>Connection fails immediately</td><td>RE Manager not running or wrong ports</td>
    <td>Click Restart RE Manager; verify ports in Connection Settings</td></tr>
<tr><td>RE Console blank</td><td>Wrong Info port</td>
    <td>Set Info port to 60625 in Connection Settings</td></tr>
<tr><td>SSH restart fails</td><td>SSH key not installed or wrong user</td>
    <td>Re-run Setup SSH Key… in Connection Settings</td></tr>
</table>
"""

_COMPOSER_HTML = """
<h2>Visual Composer</h2>
<p>Drag blocks from the <b>Block Palette</b> onto the <b>Main Sequence</b>
or <b>Per-Step Sequence</b>.  Edit parameters in <b>Block Properties</b>
on the right.  The generated plan code updates live.
Click <b>→ Send to Code Editor</b> to review and upload.</p>

<h3>Typical scan workflow</h3>
<pre>Main:      Set Exposure → Set AD File → Scan
Per-step:  Open Shutter → Trigger &amp; Read → Close Shutter → Sleep</pre>

<h3>All scan block types</h3>
<table>
<tr><th>Block</th><th>Bluesky call</th><th>Notes</th></tr>
<tr><td><b>Scan</b></td><td><code>bp.scan</code></td>
    <td>Single motor: parametric. Multi-motor: comma-separate motors and
    start/stop — each motor gets its own range.</td></tr>
<tr><td><b>Relative Scan</b></td><td><code>bp.rel_scan</code></td>
    <td>Same as Scan but positions are relative to current motor position.</td></tr>
<tr><td><b>Grid Scan</b></td><td><code>bp.grid_scan</code></td>
    <td>2-D (or N-D) grid. Comma-separate motors/starts/stops/nums.
    Optional <i>Energy inner axis</i> field.</td></tr>
<tr><td><b>List Scan</b></td><td><code>bp.list_scan</code></td>
    <td>Explicit position list. Optional <i>Energy inner loop</i>
    (nested for-loop: spatial outer, energy scan inner).</td></tr>
<tr><td><b>Adaptive Scan</b></td><td><code>bp.adaptive_scan</code></td>
    <td>Intelligent step sizing. Set <i>target_field</i> to the detector
    reading name, e.g. <code>Pil300K_stats1_total</code>.</td></tr>
<tr><td><b>Fly Scan</b></td><td><code>bp.fly</code></td>
    <td>Continuous acquisition. If motor is set, prepends velocity config
    and move-to-start before the fly call.</td></tr>
<tr><td><b>Count</b></td><td><code>bp.count</code></td>
    <td>Fixed-position acquisition.</td></tr>
</table>

<h3>Flow blocks</h3>
<table>
<tr><th>Block</th><th>What it generates</th></tr>
<tr><td><b>Repeat N Times</b></td>
    <td>Wraps the entire Main Sequence in <code>for _i in range(n):</code>.
    Composable with other Flow blocks.</td></tr>
<tr><td><b>For Each Position</b></td>
    <td>Self-contained loop: move motor to each position, then count.
    Includes its own detector/num/delay fields.</td></tr>
<tr><td><b>Custom Python</b></td>
    <td>Injects raw Python at that position in the sequence.
    Edit in the multiline code editor in Block Properties.</td></tr>
</table>

<h3>Multi-motor start/stop</h3>
<p>Enter comma-separated values in the <b>Start</b> and <b>Stop</b>
fields — one value per motor.  If you enter fewer values than motors,
the last value is repeated for remaining motors.</p>
<pre>Motors : coll_x, coll_y
Start  : 0.0, 10.0
Stop   : 5.0, 20.0   →   bp.scan(dets, coll_x, 0.0, 5.0, coll_y, 10.0, 20.0, 11)</pre>

<h3>Energy inner loop</h3>
<p>In a <b>Grid Scan</b> or <b>List Scan</b> block, set the
<i>Energy motor</i>, <i>Energy start/stop/num</i> fields.
Leave <i>Energy motor</i> blank to skip the energy loop.</p>
<ul>
<li><b>Grid Scan + energy</b> — appended as the innermost (fastest-varying)
    <code>bp.grid_scan</code> axis.</li>
<li><b>List Scan + energy</b> — nested for-loop (spatial outer,
    <code>bp.scan</code> over energy at each position).</li>
</ul>
"""

_SHORTCUTS_HTML = """
<h2>Keyboard Shortcuts</h2>

<h3>Visual Composer — sequence lists</h3>
<table>
<tr><th>Key / Action</th><th>Effect</th></tr>
<tr><td><code>Del</code></td><td>Remove selected block</td></tr>
<tr><td>Drag row</td><td>Reorder blocks within a list</td></tr>
<tr><td>Drag from palette</td><td>Insert block at drop position</td></tr>
<tr><td>Double-click palette item</td><td>Append to last active sequence</td></tr>
<tr><td><b>Add to Main ↑</b> button</td><td>Append selected palette block to Main</td></tr>
<tr><td><b>Add to Per-Step ↓</b> button</td><td>Append selected palette block to Per-Step</td></tr>
</table>

<h3>Code Editor</h3>
<table>
<tr><th>Key</th><th>Effect</th></tr>
<tr><td><code>Ctrl+Space</code></td><td>Trigger autocomplete</td></tr>
<tr><td><code>Tab</code></td><td>Insert 4 spaces (smart tab stop)</td></tr>
<tr><td><code>Shift+Tab</code></td><td>Remove one indent level</td></tr>
<tr><td><code>Backspace</code></td><td>Smart backspace — removes a full 4-space block</td></tr>
<tr><td><code>Enter</code> after <code>:</code></td><td>Auto-indent next line</td></tr>
</table>

<h3>Queue Manager</h3>
<table>
<tr><th>Key / Action</th><th>Effect</th></tr>
<tr><td>Double-click queue item</td><td>View plan details</td></tr>
<tr><td><b>▶ Start</b></td><td>Start the queue (requires an open experiment)</td></tr>
<tr><td><b>✕ Abort</b></td><td>Abort running plan (prompts confirmation)</td></tr>
</table>

<h3>Global</h3>
<table>
<tr><th>Menu / Action</th><th>Effect</th></tr>
<tr><td><b>File → Connection Settings</b></td><td>Configure host, profiles, SSH key</td></tr>
<tr><td><b>File → Edit Devices File</b></td><td>Edit ophyd device definitions in-app</td></tr>
<tr><td><b>View → Theme</b></td><td>Switch between Dark / Light / Midnight themes</td></tr>
<tr><td><b>Help → Quick Start</b></td><td>This help dialog</td></tr>
</table>
"""

_MULTI_HOST_HTML = """
<h2>Multi-Host Registry</h2>

<p>The multi-host registry lets several RE Managers — potentially running on different
Linux machines — be discovered and managed from any EasyBluesky client on the network.
No server process is required: the registry is a single JSON file stored on a
designated host, read and written over SSH.</p>

<h3>How it works</h3>
<table>
<tr><th>Component</th><th>What it does</th></tr>
<tr><td><b>Registry file</b></td>
    <td>A <code>~/.easy_bluesky/registry.json</code> on one designated host
    (the "registry host") lists every RE Manager instance — its name, host,
    ports, and devices file.</td></tr>
<tr><td><b>Auto-discovery</b></td>
    <td>At startup the app SSH-reads the registry, TCP-probes each instance in
    parallel, and auto-connects to the last-used profile if it is running.
    The whole operation happens in the background — the UI stays responsive.</td></tr>
<tr><td><b>Profile merge</b></td>
    <td>Instances in the registry are merged into your local profile list.
    Existing profiles matched by name have their host and ports updated;
    new instances are added as new profiles.</td></tr>
<tr><td><b>Registry Admin</b></td>
    <td>Open <b>File → Registry Admin…</b> to add, edit, or remove instances.
    The dialog is password-protected; the password hash is stored in the registry
    file — never transmitted over the wire.</td></tr>
</table>

<h3>Step 1 — Open Registry Admin</h3>
<p>Click <b>File → Registry Admin…</b>.  No prior configuration is needed.</p>
<p>If the registry host has not been set up yet, the dialog opens a
<b>Setup page</b> that asks for:</p>
<table>
<tr><th>Field</th><th>Example</th><th>Notes</th></tr>
<tr><td><b>Registry host / IP</b></td><td><code>164.54.169.92</code></td>
    <td>The machine that stores <code>registry.json</code>.  Usually the same
    machine that runs the RE Managers, but any SSH-reachable host works.</td></tr>
<tr><td><b>SSH user</b></td><td><code>chem_epics</code></td>
    <td>The Linux username on that machine.  Must have key-based SSH access.</td></tr>
<tr><td><b>SSH key path</b></td><td><code>~/.ssh/id_ed25519</code></td>
    <td>Your private key.  The matching public key must be in
    <code>~/.ssh/authorized_keys</code> on the registry host.</td></tr>
</table>
<p>Click <b>Connect →</b>.  These settings are saved to your local profile so
future sessions go straight to the loading step.</p>
<div class="tip">You can also set the registry host later in
<b>File → Connection Settings → Registry</b>, or change it directly inside
the Registry Admin editor page.</div>

<h3>Step 2 — Create the registry and set a password</h3>
<ol>
<li>After a successful SSH connection, if no registry exists you will be
    asked to create an admin password — this is stored as a hash in
    <code>registry.json</code> and never leaves the registry host.</li>
<li>If a registry already exists you are prompted for the existing password.</li>
</ol>
<div class="warn">The password is hashed with PBKDF2-SHA256 (200&thinsp;000 iterations)
and stored in <code>registry.json</code>.  EasyBluesky never stores or transmits the
plaintext password.</div>

<h3>Step 3 — Add RE Manager instances</h3>
<p>Inside the Registry Admin window click <b>+ Add Instance</b> and fill in:</p>
<table>
<tr><th>Field</th><th>Notes</th></tr>
<tr><td><b>Name</b></td>
    <td>Short identifier, e.g. <code>ASWAXS</code>.  Must be unique.
    Profiles are matched to registry instances by name.</td></tr>
<tr><td><b>Host</b></td>
    <td>Hostname or IP of the machine that will run this RE Manager.
    Can be different from the registry host.</td></tr>
<tr><td><b>Control port / Info port</b></td>
    <td>ZMQ ports for the RE Manager.  Click <b>Auto-assign Ports</b> to pick
    free ports via SSH automatically.</td></tr>
<tr><td><b>Devices file</b></td>
    <td>Bare filename (e.g. <code>devices_ASWAXS.py</code>) relative to
    <code>~/.easy_bluesky/scripts/</code> on the remote host, or an absolute path.</td></tr>
<tr><td><b>Conda env / path</b></td>
    <td>The conda environment that contains <code>start-re-manager</code>.
    Leave blank if it is on <code>$PATH</code>.</td></tr>
</table>
<p>Click <b>Save Registry</b> to write the updated file to the registry host over SFTP.</p>

<h3>Step 4 — Connect from any client machine</h3>
<ol>
<li>Open EasyBluesky on any machine on the network.</li>
<li>Set the same <b>Registry host</b>, <b>SSH user</b>, and <b>SSH key path</b>
    in Connection Settings.</li>
<li>Restart the app (or it auto-discovers at startup).  Available instances appear
    in the profile dropdown with a live running/stopped indicator.</li>
<li>Select the profile and click <b>Connect</b>.</li>
</ol>
<div class="tip">SSH key auth is used for all operations — no passwords are typed
or stored.  Each client machine needs its public key installed on the registry host
(and on any separate RE Manager hosts).<br>
Use <b>Setup SSH Key…</b> in Connection Settings to generate and install a key
in one step.</div>

<h3>Per-profile host override</h3>
<p>In Connection Settings, each profile has an optional <b>Host override</b> field.
Set this when a profile's RE Manager runs on a <i>different</i> machine than the
global SSH host.  The override is used for:</p>
<ul>
<li>ZMQ connection addresses</li>
<li>SSH restart / stop commands</li>
<li>Log tail (RE Console)</li>
<li>Port auto-assignment via SSH</li>
</ul>
<p>Leave it blank to inherit the global <b>Host</b> setting.</p>

<h3>Architecture diagram</h3>
<pre>
  Client A (Windows/Mac)             Linux beamline network
  ──────────────────────             ──────────────────────
  EasyBluesky                        registry-host
    File → Registry Admin  ─── SSH ─→  ~/.easy_bluesky/registry.json
    startup auto-discovery ─── SSH ─→  (reads registry, probes ports)

  Client B (Windows/Mac)             re-host-1
  ──────────────────────             ─────────
  EasyBluesky                        RE Manager (profile: ASWAXS)
    profile: ASWAXS  ──── ZMQ ────→   ctrl port 60615
                     ──── ZMQ ────→   info port 60625

                                    re-host-2
                                    ─────────
                                    RE Manager (profile: SAXS)
                                      ctrl port 60715
                                      info port 60725
</pre>

<h3>registry.json format</h3>
<pre>{
  "version": 1,
  "admin_password_hash": "pbkdf2:sha256:&lt;salt&gt;:&lt;hash&gt;",
  "instances": [
    {
      "name":          "ASWAXS",
      "host":          "beamline-pc.example.org",
      "control_port":  60615,
      "info_port":     60625,
      "procserv_port": 60635,
      "devices_file":  "devices_ASWAXS.py",
      "conda_env":     "easy-bluesky",
      "conda_path":    "~/anaconda3",
      "description":   "Wide-angle SAXS beamline"
    }
  ]
}</pre>
"""

_ABOUT_HTML = """
<h2>EasyBluesky</h2>
<p>A PyQt6 desktop GUI for controlling Bluesky/ophyd beamlines via the
<b>bluesky-queueserver</b> (ZMQ transport).</p>

<table>
<tr><td><b>Version</b></td><td>0.1.0</td></tr>
<tr><td><b>Python</b></td><td>≥ 3.10</td></tr>
<tr><td><b>UI toolkit</b></td><td>PyQt6</td></tr>
<tr><td><b>License</b></td><td>BSD 3-Clause</td></tr>
<tr><td><b>Source</b></td>
    <td><a href="https://github.com/nayanbera/easy-bluesky">
    github.com/nayanbera/easy-bluesky</a></td></tr>
</table>

<h3>Key dependencies</h3>
<table>
<tr><th>Package</th><th>Purpose</th></tr>
<tr><td><code>bluesky-queueserver-api</code></td><td>ZMQ client API</td></tr>
<tr><td><code>bluesky</code></td><td>Plan protocols and helpers</td></tr>
<tr><td><code>ophyd</code></td><td>Device abstractions</td></tr>
<tr><td><code>paramiko</code></td><td>SSH management of remote RE Manager</td></tr>
<tr><td><code>pyqtgraph</code></td><td>Live Viewer plots</td></tr>
<tr><td><code>suitcase-jsonl</code></td><td>JSONL run file I/O</td></tr>
</table>

<h3>Acknowledgements</h3>
<p>EasyBluesky is developed at <b>NSF's ChemMatCARS, Sector&nbsp;15</b> at the
Advanced Photon Source (APS), Argonne National Laboratory (ANL).</p>
<p>NSF's ChemMatCARS is supported by the Divisions of Chemistry (CHE) and
Materials Research (DMR), National Science Foundation, under grant number
<b>NSF/CHE-2335833</b>.</p>
<p style="color:#888; margin-top:8px;">
Developed with assistance from
<a href="https://claude.ai">Claude</a> (Anthropic).</p>
"""


class _HelpDialog(QDialog):
    """Tabbed help / documentation dialog.  Non-modal so it stays open."""

    _TABS = [
        ("Quick Start",        _QUICK_START_HTML),
        ("Visual Composer",    _COMPOSER_HTML),
        ("Keyboard Shortcuts", _SHORTCUTS_HTML),
        ("Multi-Host Registry",_MULTI_HOST_HTML),
        ("About",              _ABOUT_HTML),
    ]

    def __init__(self, parent=None, start_tab: int = 0):
        super().__init__(parent)
        self.setWindowTitle("EasyBluesky Help")
        self.setMinimumSize(700, 540)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)

        self._tabs = QTabWidget()
        for title, html in self._TABS:
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            browser.document().setDefaultStyleSheet(_HELP_CSS)
            browser.setHtml(html)
            self._tabs.addTab(browser, title)
        lay.addWidget(self._tabs, 1)

        btn_close = QPushButton("Close")
        btn_close.setFixedWidth(90)
        btn_close.clicked.connect(self.hide)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn_close)
        row.setContentsMargins(0, 0, 12, 0)
        lay.addLayout(row)

        self._tabs.setCurrentIndex(start_tab)

    def show_tab(self, index: int):
        self._tabs.setCurrentIndex(index)
        self.show()
        self.raise_()
        self.activateWindow()


# ── Startup discovery worker ───────────────────────────────────────────────────

class _DiscoveryWorker(QThread):
    """Background thread: fetch registry via SSH then TCP-probe all instances."""
    done   = pyqtSignal(list)   # list of instance dicts with extra 'running' key
    failed = pyqtSignal(str)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings

    def run(self):
        try:
            reg       = fetch_registry(self._settings)
            instances = reg.get("instances", [])
            running   = probe_all_instances(instances)
            result    = [{**inst, "running": running.get(inst.get("name", ""), False)}
                         for inst in instances]
            self.done.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    # Emitted on the main thread; queued delivery runs connect() on worker_thread.
    _connect_requested = pyqtSignal(str, str)  # ctrl_addr, info_addr

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
        self._start_discovery_or_connect()
        self.apply_theme(self._current_theme)

    def _setup_ui(self):
        self.setStyleSheet(build_stylesheet(self._current_theme))

        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        self.experiments_tab    = ExperimentsTab(self.worker)
        self.queue_mgr          = QueueManager(self.worker)
        self.plan_builder       = PlanBuilder(self.worker)
        self.devices_plans_tab  = DevicesPlansTab()
        self.watchdog_tab       = PVWatchdogTab()
        self.mongo_browser      = MongoDataBrowserTab(self._conn_settings)
        self.hdf5_viewer        = HDF5Viewer()
        self.re_console         = REConsoleWidget()

        self.tabs.addTab(self.experiments_tab,   "🧪  Experiments")
        self.tabs.addTab(self.queue_mgr,         "⚙  Queue Manager")
        self.tabs.addTab(self.plan_builder,      "🔧  Plan Builder")
        self.tabs.addTab(self.devices_plans_tab, "🔬  Devices & Plans")
        self.tabs.addTab(self.watchdog_tab,      "🔭  PV Watchdog")
        self.tabs.addTab(self.mongo_browser,     "📊  MongoDB Browser")
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
        act_reg = file_menu.addAction("Registry Admin…")
        act_reg.triggered.connect(self._on_registry_admin)
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

        self._help_dialog = None

        help_menu = menubar.addMenu("Help")
        act_qs    = help_menu.addAction("Quick Start…")
        act_vc    = help_menu.addAction("Visual Composer Guide…")
        act_kb    = help_menu.addAction("Keyboard Shortcuts…")
        act_mh    = help_menu.addAction("Multi-Host Registry…")
        help_menu.addSeparator()
        act_about = help_menu.addAction("About EasyBluesky")

        act_qs.triggered.connect(lambda: self._open_help(0))
        act_vc.triggered.connect(lambda: self._open_help(1))
        act_kb.triggered.connect(lambda: self._open_help(2))
        act_mh.triggered.connect(lambda: self._open_help(3))
        act_about.triggered.connect(lambda: self._open_help(4))

    def _open_help(self, tab: int = 0):
        if self._help_dialog is None:
            self._help_dialog = _HelpDialog(self)
        self._help_dialog.show_tab(tab)

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
        self.worker.pv_names_ready.connect(self.devices_plans_tab.setup_epics_monitors)
        self.worker.pv_names_error.connect(self.devices_plans_tab.on_pv_names_error)
        self.devices_plans_tab.fetch_pvnames_requested.connect(self.worker.fetch_device_pvnames)
        self.devices_plans_tab.poll_sim_values_requested.connect(self.worker.read_devices_status)
        self.devices_plans_tab.set_sim_device_requested.connect(self.worker.set_sim_device)
        self.worker.device_readings_updated.connect(self.devices_plans_tab.update_sim_values)

        self.worker.console_updated.connect(self._on_console_line)
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

        self._connect_requested.connect(self.worker.connect)

        # PV Watchdog
        self.worker.status_updated.connect(self.watchdog_tab.on_status_updated)
        self.worker.connected.connect(self.watchdog_tab.on_connected)
        self.worker.disconnected.connect(self.watchdog_tab.on_disconnected)
        self.watchdog_tab.pause_requested.connect(self._on_watchdog_pause)
        self.watchdog_tab.resume_requested.connect(self._on_watchdog_resume)
        self.watchdog_tab.log_message.connect(self._on_console_line)

        self.worker_thread.start()

    def _connect(self):
        # Only start poll thread here when discovery didn't already start it.
        # (_start_discovery_or_connect starts the thread before discovery runs.)
        if not getattr(self, "_discovery", None):
            poll_thread = threading.Thread(target=self.worker.poll, daemon=True)
            poll_thread.start()
        QTimer.singleShot(100, self._do_connect)

    def _do_connect(self):
        ctrl, info, _ = make_zmq_addrs(self._conn_settings)
        # Emit signal — Qt queues delivery to worker_thread's event loop so
        # the TCP pre-check + ZMQ status() call runs there, not on the main
        # thread, keeping the UI responsive.
        self._connect_requested.emit(ctrl, info)

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

    def _on_console_line(self, text: str):
        """Forward console text to the RE Console widget and inject diagnostic hints."""
        self.re_console.append(text)
        # Detect the most common mis-configuration: start-re-manager not on PATH
        # because conda_env / conda_path are not set in Connection Settings.
        if "start-re-manager" in text and "not found" in text:
            self.re_console.append(
                "[EasyBluesky] ⚠ start-re-manager not found on the remote host.\n"
                "  → Open File → Connection Settings and set:\n"
                "      Conda Environment  (e.g. easy-bluesky)\n"
                "      Conda Path         (e.g. ~/anaconda3 or ~/miniconda3)\n"
                "  Then click ‘Restart RE Manager’ to try again.\n"
            )

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

    def _on_watchdog_pause(self):
        ok, msg = self.worker.re_pause(option="immediate")
        self._log(f"[{self._ts()}] [Watchdog] {'✓' if ok else '✗'} Pause immediate: {msg}")
        if not ok:
            # RE not running (between plans) — stop the queue so next plan doesn't start
            ok2, msg2 = self.worker.queue_stop()
            self._log(f"[{self._ts()}] [Watchdog] {'✓' if ok2 else '✗'} Queue stop: {msg2}")

    def _on_watchdog_resume(self):
        ok, msg = self.worker.re_resume()
        self._log(f"[{self._ts()}] [Watchdog] {'✓' if ok else '✗'} Resume: {msg}")

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
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info, zmq_doc=doc)
        if ok:
            self.experiments_tab.live_viewer.restart_zmq(doc)
            self._log(f"[{self._ts()}] ✓ Connected")
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Still starting — click Reconnect when ready")

    def _auto_reconnect_mode(self):
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        self._log(f"[{self._ts()}] Auto-reconnecting to {ctrl}…")
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info, zmq_doc=doc)
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected")
            self.experiments_tab.live_viewer.restart_zmq(doc)
        else:
            self.re_bar.set_disconnected()
            self._log(f"[{self._ts()}] ✗ Still starting — click Reconnect when ready")

    def _on_reconnect_requested(self):
        self._log(f"[{self._ts()}] Reconnecting to RE Manager…")
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info, zmq_doc=doc)
        if ok:
            self.experiments_tab.live_viewer.restart_zmq(doc)
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
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info, zmq_doc=doc)
        if ok:
            self._log(f"[{self._ts()}] ✓ Connected to profile '{name}'")
        else:
            self._log(
                f"[{self._ts()}] ✗ Profile '{name}' RE Manager not running at {ctrl}\n"
                f"              not running — click Start RE Mgr to start it"
            )
            self.re_bar.set_disconnected()
        self.experiments_tab.live_viewer.restart_zmq(doc)
        self.mongo_browser.update_settings(self._conn_settings)

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
        sim_devices  = scripts_dir / "devices_sim.py"

        # Pick the real hardware devices file as source.  If the active profile
        # already points at devices_sim.py (circular), search other profiles and
        # the local scripts dir for any non-sim devices file first.
        profile       = get_active_profile(self._conn_settings)
        devices_fname = profile.get("devices_file", "devices.py")

        _SIM_NAMES = {"devices_sim.py", "devices_sim"}
        if devices_fname in _SIM_NAMES or not (scripts_dir / devices_fname).exists():
            # Try other profiles
            devices_fname = ""
            for p in self._conn_settings.get("profiles", []):
                fn = p.get("devices_file", "")
                if fn and fn not in _SIM_NAMES and (scripts_dir / fn).exists():
                    devices_fname = fn
                    break
            if not devices_fname:
                # Scan scripts dir for any devices_*.py that isn't the sim file
                for candidate in sorted(scripts_dir.glob("devices_*.py")):
                    if candidate.name not in _SIM_NAMES:
                        devices_fname = candidate.name
                        break

        real_script = scripts_dir / devices_fname if devices_fname else None
        if not real_script or not real_script.exists():
            real_script = scripts_dir / "re_startup_mongo.py"
        if not real_script.exists():
            QMessageBox.warning(self, "Not Found",
                f"No hardware devices file found in:\n{scripts_dir}\n\n"
                "Open 'Edit Devices File' for a real-hardware profile to cache "
                "the remote devices file locally, then try again.")
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

    # ── registry admin ─────────────────────────────────────────────────────────

    def _on_registry_admin(self):
        from .registry_admin import RegistryAdminWindow
        dlg = RegistryAdminWindow(self._conn_settings, parent=self)
        dlg.exec()

    # ── startup discovery ──────────────────────────────────────────────────────

    def _start_discovery_or_connect(self):
        """Run registry discovery then auto-connect; fall back to direct connect."""
        registry_host = (
            self._conn_settings.get("registry_host", "").strip()
            or self._conn_settings.get("host", "")
        )
        ssh_ready = bool(
            registry_host
            and self._conn_settings.get("ssh_user", "")
            and self._conn_settings.get("ssh_key_path", "")
        )
        if not ssh_ready:
            # No registry configured — connect immediately as before
            self._connect()
            return

        self.status_bar.showMessage(
            f"Discovering instances on {registry_host}…"
        )
        self._discovery = _DiscoveryWorker(self._conn_settings, parent=self)
        self._discovery.done.connect(self._on_discovery_done)
        self._discovery.failed.connect(self._on_discovery_failed)
        self._discovery.start()
        # Start the poll loop now so the UI is live while discovery runs
        poll_thread = threading.Thread(target=self.worker.poll, daemon=True)
        poll_thread.start()

    def _on_discovery_done(self, instances: list):
        if not instances:
            self.status_bar.showMessage("Registry returned no instances.")
            QTimer.singleShot(100, self._do_connect)
            return

        # Merge discovered instances into local profiles
        profiles = self._conn_settings.get("profiles", [])
        profiles, added, updated = merge_into_profiles(profiles, instances)
        self._conn_settings["profiles"] = profiles
        save_connection(self._conn_settings)

        # Update the profile combo
        names  = [p.get("name", "") for p in profiles]
        active = self._conn_settings.get("active_profile", "Default")
        self.re_bar.update_profiles(names, active)

        running_names = [i["name"] for i in instances if i.get("running")]
        n_run = len(running_names)
        n_tot = len(instances)
        self.status_bar.showMessage(
            f"Registry: {n_tot} instance(s) found, {n_run} running"
            + (f"  |  merged {added} new, {updated} updated" if added or updated else "")
        )

        # Auto-connect if the active profile is running
        active_running = any(
            i["name"] == active and i.get("running") for i in instances
        )
        if active_running:
            QTimer.singleShot(100, self._do_connect)
        else:
            # Active profile not running — show status but don't block the UI
            if active not in [i["name"] for i in instances]:
                note = f"Profile '{active}' not in registry"
            elif running_names:
                note = (f"Profile '{active}' not running — "
                        f"running: {', '.join(running_names)}")
            else:
                note = f"Profile '{active}' is not running"
            self._log(f"[EasyBluesky] {note}")
            self.conn_label.setText("⬤  Not running")
            self.conn_label.setStyleSheet("color: #ff7f0e;")
            self.re_bar.set_disconnected()

    def _on_discovery_failed(self, msg: str):
        self._log(f"[EasyBluesky] Registry discovery failed: {msg}")
        self.status_bar.showMessage("Registry unavailable — connecting directly")
        QTimer.singleShot(100, self._do_connect)

    def _on_connection_settings(self):
        dlg = ConnectionDialog(self)
        if dlg.exec() != ConnectionDialog.DialogCode.Accepted:
            return
        self._conn_settings = dlg.get_settings()
        apply_epics_env(self._conn_settings)
        ctrl, info, doc = make_zmq_addrs(self._conn_settings)
        self.status_bar.showMessage(f"Reconnecting to {self._conn_settings['host']}…")
        ok = self.worker.connect(zmq_control=ctrl, zmq_info=info, zmq_doc=doc)
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
        self.mongo_browser.update_settings(self._conn_settings)

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
    apply_epics_env(settings)   # must run before pyepics initialises libca

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
