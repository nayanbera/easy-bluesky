"""connection_settings.py — Persistent connection settings + dialog."""

import json
import os
import re
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)
from .widgets import NoScrollSpinBox


# ── Registry helper: background SSH fetch ─────────────────────────────────────

class _RegistryFetchWorker(QThread):
    """Fetch registry.json via SSH and probe all instances in background."""
    done  = pyqtSignal(dict, dict)   # (registry_dict, {name: running_bool})
    error = pyqtSignal(str)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings

    def run(self):
        try:
            from .registry import fetch_registry, probe_all_instances
            reg     = fetch_registry(self._settings)
            running = probe_all_instances(reg.get("instances", []))
            self.done.emit(reg, running)
        except Exception as e:
            self.error.emit(str(e))


# ── Import from Registry dialog ────────────────────────────────────────────────

class _ImportFromRegistryDialog(QDialog):
    """Show registry instances as a checklist; caller imports the selection."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings   = settings
        self._instances  = []
        self._checkboxes = []
        self.setWindowTitle("Import from Registry")
        self.setMinimumWidth(440)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        host = settings.get("registry_host") or settings.get("host", "")
        host_lbl = QLabel(f"Registry on:  <b>{host}</b>")
        v.addWidget(host_lbl)

        self._status = QLabel("Connecting…")
        self._status.setObjectName("dim_text")
        v.addWidget(self._status)

        # Checkbox list (hidden until fetch completes)
        self._list_w = QWidget()
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(4)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._list_w)
        self._scroll.setMinimumHeight(150)
        self._scroll.setVisible(False)
        v.addWidget(self._scroll)

        btn_row = QHBoxLayout()
        self._btn_import = QPushButton("Import Selected")
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self._btn_import)
        v.addLayout(btn_row)

        self._worker = _RegistryFetchWorker(settings, parent=self)
        self._worker.done.connect(self._on_fetched)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_fetched(self, registry: dict, running: dict):
        instances = registry.get("instances", [])
        if not instances:
            self._status.setText("Registry is empty — no instances to import.")
            return
        self._instances = instances
        self._status.setText(
            f"Found {len(instances)} instance(s).  Select which to add as local profiles:"
        )
        for inst in instances:
            name = inst.get("name", "(unnamed)")
            host = inst.get("host", "")
            ctrl = inst.get("control_port", "?")
            cb = QCheckBox(f"{name}   ({host}:{ctrl})")
            cb.setChecked(True)
            self._checkboxes.append(cb)
            self._list_w.layout().addWidget(cb)
        self._list_w.layout().addStretch()
        self._scroll.setVisible(True)
        self._btn_import.setEnabled(True)

    def _on_error(self, msg: str):
        self._status.setText(f"✗  {msg}")
        self._status.setStyleSheet("color: #d62728;")

    def selected_instances(self) -> list:
        return [
            inst for inst, cb in zip(self._instances, self._checkboxes)
            if cb.isChecked()
        ]


# ── Publish to Registry dialog ─────────────────────────────────────────────────

class _PublishToRegistryDialog(QDialog):
    """Authenticate against the registry on the profile's host, then push the profile."""

    def __init__(self, settings: dict, profile: dict, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._profile  = profile
        self._registry = {}
        self.setWindowTitle("Publish to Registry")
        self.setMinimumWidth(420)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        host = settings.get("registry_host") or settings.get("host", "")
        v.addWidget(QLabel(f"Registry on:  <b>{host}</b>"))

        name = profile.get("name", "")
        ctrl = profile.get("control_port", "?")
        info_lbl = QLabel(
            f"Publishing profile <b>{name}</b> (control port {ctrl}) to the registry."
        )
        info_lbl.setWordWrap(True)
        v.addWidget(info_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(sep)

        self._pw_title = QLabel("Fetching registry…")
        self._pw_title.setStyleSheet("font-weight: bold;")
        v.addWidget(self._pw_title)

        self._pw_sub = QLabel("")
        self._pw_sub.setWordWrap(True)
        self._pw_sub.setObjectName("dim_text")
        v.addWidget(self._pw_sub)

        self._pw_form = QFormLayout()
        self._pw_form.setHorizontalSpacing(12)
        self._pw_entry = QLineEdit()
        self._pw_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_entry.setPlaceholderText("Password")
        self._pw_entry.returnPressed.connect(self._on_publish)
        self._pw_form.addRow("Password:", self._pw_entry)
        self._pw_confirm = QLineEdit()
        self._pw_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_confirm.setPlaceholderText("Confirm password")
        self._pw_confirm.returnPressed.connect(self._on_publish)
        self._pw_form.addRow("Confirm:", self._pw_confirm)
        v.addLayout(self._pw_form)

        self._pw_error = QLabel("")
        self._pw_error.setStyleSheet("color: #d62728;")
        self._pw_error.setWordWrap(True)
        v.addWidget(self._pw_error)

        self._pub_status = QLabel("")
        self._pub_status.setObjectName("dim_text")
        v.addWidget(self._pub_status)

        btn_row = QHBoxLayout()
        self._btn_publish = QPushButton("Publish")
        self._btn_publish.setDefault(True)
        self._btn_publish.setEnabled(False)
        self._btn_publish.clicked.connect(self._on_publish)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self._btn_publish)
        v.addLayout(btn_row)

        self._worker = _RegistryFetchWorker(settings, parent=self)
        self._worker.done.connect(self._on_fetched)
        self._worker.error.connect(self._on_fetch_error)
        self._worker.start()

    def _on_fetched(self, registry: dict, _running: dict):
        self._registry = registry
        has_hash = bool(registry.get("admin_password_hash", ""))
        if has_hash:
            self._pw_title.setText("Admin Password")
            self._pw_sub.setText(
                "Enter the admin password to publish to this registry."
            )
            self._pw_form.setRowVisible(self._pw_confirm, False)
        else:
            self._pw_title.setText("Create Admin Password")
            self._pw_sub.setText(
                "No admin password is set yet. "
                "Create one to protect the registry from unauthorised changes."
            )
            self._pw_form.setRowVisible(self._pw_confirm, True)
        self._btn_publish.setEnabled(True)
        self._pw_entry.setFocus()

    def _on_fetch_error(self, msg: str):
        self._pw_title.setText("Connection failed")
        self._pw_sub.setText(f"✗  {msg}")
        self._pw_sub.setStyleSheet("color: #d62728;")

    def _on_publish(self):
        from .registry import hash_password, verify_password, save_registry
        password = self._pw_entry.text()
        stored   = self._registry.get("admin_password_hash", "")
        if not stored:
            confirm = self._pw_confirm.text()
            if not password:
                self._pw_error.setText("Password cannot be empty.")
                return
            if password != confirm:
                self._pw_error.setText("Passwords do not match.")
                return
            self._registry["admin_password_hash"] = hash_password(password)
        else:
            if not verify_password(password, stored):
                self._pw_error.setText("Incorrect password.")
                self._pw_entry.clear()
                self._pw_entry.setFocus()
                return

        # Add or update this profile as a registry instance
        instances = self._registry.setdefault("instances", [])
        name      = self._profile.get("name", "")
        existing  = next((i for i in instances if i.get("name") == name), None)
        inst = {
            "name":         name,
            "host":         self._profile.get("host", ""),
            "description":  self._profile.get("description", ""),
            "control_port": self._profile.get("control_port", 60615),
            "info_port":    self._profile.get("info_port",    60625),
            "doc_port":     self._profile.get("doc_port",     60630),
            "procserv_port":self._profile.get("procserv_port",60635),
            "devices_file": self._profile.get("devices_file", "devices.py"),
            "conda_env":    self._profile.get("conda_env",    ""),
            "conda_path":   self._profile.get("conda_path",   "~/miniconda3"),
        }
        if existing:
            existing.update(inst)
        else:
            instances.append(inst)

        self._btn_publish.setEnabled(False)
        self._pub_status.setText("Saving…")
        self._pw_error.setText("")
        try:
            save_registry(self._settings, self._registry)
            self._pub_status.setText("✓  Published successfully.")
            self._pub_status.setStyleSheet("color: #2ca02c;")
            QTimer.singleShot(1200, self.accept)
        except Exception as e:
            self._pub_status.setText(f"✗  {e}")
            self._pub_status.setStyleSheet("color: #d62728;")
            self._btn_publish.setEnabled(True)


_SETTINGS_FILE = Path.home() / ".easy_bluesky" / "connection.json"

_PROFILE_DEFAULTS = {
    "name": "Default",
    "host": "",           # per-profile host override; empty = use global host
    "devices_file": "devices.py",
    "is_local": False,
    "control_port": 60615,
    "info_port": 60625,
    "doc_port": 60630,
    "procserv_port": 60635,
    # MongoDB / databroker — empty mongo_db disables subscription
    "mongo_db":   "",
    "mongo_host": "",      # empty → localhost on the RE machine
    "mongo_port": 27017,
    # Local experiment root — overrides ~/.easy_bluesky/experiments/ (e.g. NFS mount).
    "local_data_root": "",
    # Remote data directory — base path on the RE machine for detector files.
    # Injected as 'remote_exp_dir' in every plan's md kwargs.
    "remote_data_root": "",
}

_DEFAULTS = {
    "host": "localhost",
    "ssh_user": "",
    "ssh_port": 22,
    "ssh_key_path": "~/.ssh/id_rsa",
    "ssh_service": "",
    "conda_env": "",
    "conda_path": "~/miniconda3",
    "registry_host": "",   # host where registry.json lives; empty = use global host
    "registry_path": "",   # path on registry host; empty = ~/.easy_bluesky/registry.json
    "active_profile": "Default",
    "profiles": [_PROFILE_DEFAULTS.copy()],
    "deleted_profiles": [],
    "epics_ca_addr_list": "",
    "epics_ca_auto_addr_list": True,
    # ESAF server — global (not per-profile)
    "esaf_server_url": "",   # e.g. http://beamline-host:8765
    "esaf_api_key":    "",   # shared API key for write operations
}


def profile_slug(name: str) -> str:
    """Convert a profile name to a safe filename slug."""
    slug = name.lower().replace(" ", "_")
    slug = re.sub(r'[^a-z0-9_]', '', slug)
    return slug or "profile"


def _ensure_profile_defaults(profile: dict) -> dict:
    """Return profile with all required keys filled in from defaults."""
    result = _PROFILE_DEFAULTS.copy()
    result.update(profile)
    return result


def _migrate(data: dict) -> dict:
    """Convert old flat format (control_port, sim_control_port, etc.) to profiles list."""
    if "profiles" in data:
        # Backfill is_local on existing profiles that predate this field
        for p in data["profiles"]:
            p.setdefault("is_local", False)
        return data

    profiles = []
    default_profile = {
        "name": "Default",
        "devices_file": "devices.py",
        "is_local": False,
        "control_port": data.get("control_port", _PROFILE_DEFAULTS["control_port"]),
        "info_port": data.get("info_port", _PROFILE_DEFAULTS["info_port"]),
        "doc_port": data.get("doc_port", _PROFILE_DEFAULTS["doc_port"]),
        "procserv_port": data.get("procserv_port", _PROFILE_DEFAULTS["procserv_port"]),
    }
    profiles.append(default_profile)

    if any(k in data for k in ("sim_control_port", "sim_info_port", "sim_doc_port")):
        sim_profile = {
            "name": "Sim",
            "devices_file": "devices_sim.py",
            "is_local": False,
            "control_port": data.get("sim_control_port", 60616),
            "info_port": data.get("sim_info_port", 60626),
            "doc_port": data.get("sim_doc_port", 60631),
            "procserv_port": data.get("sim_procserv_port", 60636),
        }
        profiles.append(sim_profile)

    return {
        "host": data.get("host", _DEFAULTS["host"]),
        "ssh_user": data.get("ssh_user", _DEFAULTS["ssh_user"]),
        "ssh_port": data.get("ssh_port", _DEFAULTS["ssh_port"]),
        "ssh_key_path": data.get("ssh_key_path", _DEFAULTS["ssh_key_path"]),
        "ssh_service": data.get("ssh_service", _DEFAULTS["ssh_service"]),
        "conda_env": data.get("conda_env", _DEFAULTS["conda_env"]),
        "conda_path": data.get("conda_path", _DEFAULTS["conda_path"]),
        "active_profile": "Default",
        "profiles": profiles,
        "deleted_profiles": [],
    }


def load_connection() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            data = json.loads(_SETTINGS_FILE.read_text())
            data = _migrate(data)
            result = dict(_DEFAULTS)
            result.update(data)
            if not result.get("profiles"):
                result["profiles"] = [_PROFILE_DEFAULTS.copy()]
            result.setdefault("deleted_profiles", [])
            # Auto-fix any port conflicts silently; persist if changed
            if _fix_port_conflicts(result):
                _SETTINGS_FILE.write_text(json.dumps(result, indent=2))
            return result
        except Exception:
            pass
    from .config import ZMQ_CONTROL, ZMQ_INFO, ZMQ_DOC_HOST, ZMQ_DOC_PORT
    try:
        ctrl_port = int(ZMQ_CONTROL.rsplit(":", 1)[-1])
        info_port = int(ZMQ_INFO.rsplit(":", 1)[-1])
    except Exception:
        ctrl_port = _PROFILE_DEFAULTS["control_port"]
        info_port = _PROFILE_DEFAULTS["info_port"]
    result = dict(_DEFAULTS)
    result["host"] = ZMQ_DOC_HOST
    result["profiles"] = [{
        **_PROFILE_DEFAULTS,
        "control_port": ctrl_port,
        "info_port": info_port,
        "doc_port": ZMQ_DOC_PORT,
    }]
    return result


def save_connection(settings: dict):
    _fix_port_conflicts(settings)  # resolve conflicts before persisting
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def apply_epics_env(settings: dict):
    """Set EPICS_CA_* environment variables from settings.

    Must be called before pyepics initialises libca (i.e. before the first
    epics.PV() call).  Changes to EPICS_CA_ADDR_LIST take full effect only
    on the next app start once libca is already running.
    """
    addr_list = settings.get("epics_ca_addr_list", "").strip()
    auto = settings.get("epics_ca_auto_addr_list", True)
    if addr_list:
        os.environ["EPICS_CA_ADDR_LIST"] = addr_list
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "YES" if auto else "NO"


def get_active_profile(settings: dict) -> dict:
    active_name = settings.get("active_profile", "Default")
    profiles = settings.get("profiles", [])
    for p in profiles:
        if p.get("name") == active_name:
            return _ensure_profile_defaults(p)
    if profiles:
        return _ensure_profile_defaults(profiles[0])
    return _PROFILE_DEFAULTS.copy()


def make_zmq_addrs(settings: dict) -> tuple:
    """Return (control_addr, info_addr, doc_addr) for the active profile.

    Per-profile host (profile["host"]) overrides the global host so that
    instances running on different machines in the network can each be
    addressed correctly from a single client machine.
    """
    profile = get_active_profile(settings)
    if profile.get("is_local", False):
        h = "localhost"
    else:
        # Profile-specific host takes priority over the global host
        h = (profile.get("host", "").strip()
             or settings.get("host", "localhost")
             or "localhost")
    return (
        f"tcp://{h}:{profile['control_port']}",
        f"tcp://{h}:{profile['info_port']}",
        f"tcp://{h}:{profile['doc_port']}",
    )


def is_local_host(settings: dict) -> bool:
    host = settings.get("host", "localhost").strip().lower()
    return host in ("localhost", "127.0.0.1", "::1", "")


def _all_used_ports(settings: dict) -> set:
    used = set()
    for p in settings.get("profiles", []):
        for key in ("control_port", "info_port", "doc_port", "procserv_port"):
            val = p.get(key)
            if isinstance(val, int):
                used.add(val)
    return used


def find_free_ports(count: int = 4, start: int = 60615, used: set = None) -> list:
    if used is None:
        used = set()
    # SO_REUSEADDR on Windows allows binding to an in-use port (different from Unix),
    # causing false "free" results.  SO_EXCLUSIVEADDRUSE enforces exclusive binding on
    # Windows; SO_REUSEADDR is the correct probe option on Unix (handles TIME_WAIT).
    _excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    result = []
    port = start
    while len(result) < count and port <= 65535:
        if port not in used:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    if _excl is not None:
                        s.setsockopt(socket.SOL_SOCKET, _excl, 1)
                    else:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("0.0.0.0", port))
                    result.append(port)
            except OSError:
                pass
        port += 1
    return result


def find_free_ports_remote(settings: dict, count: int = 4, start: int = 60615,
                           used: set = None) -> tuple:
    """Find free ports on the remote host by querying it over SSH.

    Returns (ports, note) where note describes what happened — SSH success,
    SSH failure with fallback to local check, or SSH not configured.
    One SSH round-trip replaces per-port TCP probing (which times out on
    firewalled ports and can't see ports bound by other apps on the remote).
    """
    from .ssh_manager import get_used_ports_ssh
    if used is None:
        used = set()

    remote_used = get_used_ports_ssh(settings)
    if remote_used:
        all_used = used | remote_used
        ports = find_free_ports(count, start, all_used)
        return ports, f"checked {len(remote_used)} ports in use on remote"
    else:
        ports = find_free_ports(count, start, used)
        return ports, "SSH check failed — used local port scan as fallback"


def _fix_port_conflicts(settings: dict) -> bool:
    """
    Scan all profiles for duplicate port numbers and reassign duplicates.

    Iterates profiles in order — earlier profiles keep their ports, later
    profiles that conflict get new ports assigned above the current maximum.
    Returns True if any ports were changed.
    """
    profiles = settings.get("profiles", [])
    if len(profiles) <= 1:
        return False

    _PORT_FIELDS = ("control_port", "info_port", "doc_port", "procserv_port")
    seen: dict = {}   # port -> (profile_idx, field) — first owner wins
    changed = False

    for i, p in enumerate(profiles):
        for field in _PORT_FIELDS:
            port = p.get(field)
            if not isinstance(port, int):
                continue
            if port in seen:
                # Conflict — reassign this duplicate to the next free port
                all_used = set(seen.keys())
                new = find_free_ports(1, max(all_used) + 1, all_used)
                if new:
                    p[field] = new[0]
                    seen[new[0]] = (i, field)
                    changed = True
            else:
                seen[port] = (i, field)

    return changed


# ── Profile lifecycle helpers ──────────────────────────────────────────────────

def delete_profile(settings: dict, name: str) -> bool:
    """Move a profile to deleted_profiles. Returns True if found."""
    profiles = settings.get("profiles", [])
    for i, p in enumerate(profiles):
        if p.get("name") == name:
            entry = dict(p)
            entry["_deleted_at"] = datetime.now(timezone.utc).isoformat()
            settings.setdefault("deleted_profiles", []).append(entry)
            profiles.pop(i)
            settings["profiles"] = profiles
            if settings.get("active_profile") == name:
                settings["active_profile"] = profiles[0]["name"] if profiles else ""
            return True
    return False


def restore_profile(settings: dict, deleted_entry: dict) -> bool:
    """Move an entry from deleted_profiles back to profiles."""
    entry = {k: v for k, v in deleted_entry.items() if k != "_deleted_at"}
    # Reassign ports if any conflict with existing profiles
    used = _all_used_ports(settings)
    if any(entry.get(k) in used for k in ("control_port", "info_port", "doc_port", "procserv_port")):
        start = (max(used) + 1) if used else 60615
        new_ports = find_free_ports(4, start, used)
        if len(new_ports) >= 4:
            entry["control_port"] = new_ports[0]
            entry["info_port"]    = new_ports[1]
            entry["doc_port"]     = new_ports[2]
            entry["procserv_port"]= new_ports[3]
    settings.setdefault("profiles", []).append(entry)
    # Remove from deleted list (match by name + timestamp)
    deleted = settings.get("deleted_profiles", [])
    ts = deleted_entry.get("_deleted_at", "")
    name = deleted_entry.get("name", "")
    settings["deleted_profiles"] = [
        d for d in deleted
        if not (d.get("name") == name and d.get("_deleted_at") == ts)
    ]
    return True


def purge_old_deleted(settings: dict, days: int = 30):
    """Remove deleted profiles older than days; keep at most 20."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for entry in settings.get("deleted_profiles", []):
        try:
            dt = datetime.fromisoformat(entry.get("_deleted_at", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > cutoff:
                kept.append(entry)
        except Exception:
            kept.append(entry)
    settings["deleted_profiles"] = kept[-20:]


class _SshKeyInstaller(QThread):
    """Generate an Ed25519 key (if needed) and install it on the remote host.

    The SSH password is used only during this one-time setup and is never
    stored anywhere.
    """
    progress = pyqtSignal(str)        # intermediate status line
    finished = pyqtSignal(bool, str)  # (success, final_message)

    def __init__(self, host, port, user, password, key_path, parent=None):
        super().__init__(parent)
        self._host     = host
        self._port     = int(port)
        self._user     = user
        self._password = password
        self._key_path = Path(key_path).expanduser()

    def run(self):
        try:
            self._do_setup()
        except Exception as exc:
            self.finished.emit(False, f"Unexpected error: {exc}")
        finally:
            self._password = ""   # wipe password from memory

    def _do_setup(self):
        key_path = self._key_path
        pub_path  = Path(str(key_path) + ".pub")

        # ── 1. Generate key if absent ──────────────────────────────────────────
        if key_path.exists():
            self.progress.emit("Key file found — reading public key…")
            pub_str = self._read_public_key(key_path, pub_path)
        else:
            self.progress.emit(f"Generating Ed25519 key → {key_path} …")
            pub_str = self._generate_key(key_path, pub_path)

        # ── 2. Connect with password and install public key ────────────────────
        self.progress.emit(f"Connecting to {self._user}@{self._host}:{self._port}…")
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self._host, port=self._port,
                username=self._user, password=self._password,
                look_for_keys=False, allow_agent=False, timeout=15,
            )
        except paramiko.AuthenticationException:
            self.finished.emit(False, "Authentication failed — wrong password?")
            return
        except Exception as exc:
            self.finished.emit(False, f"Connection error: {exc}")
            return

        self.progress.emit("Installing public key in ~/.ssh/authorized_keys…")
        entry = pub_str.split()[:2]   # drop any trailing comment
        safe  = " ".join(entry) + " easybluesky"
        marker = entry[1] if len(entry) > 1 else entry[0]

        cmds = [
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
            f"grep -qF -- '{marker}' ~/.ssh/authorized_keys 2>/dev/null "
            f"|| printf '%s\\n' '{safe}' >> ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]
        for cmd in cmds:
            _, stdout, stderr = client.exec_command(cmd)
            stdout.channel.recv_exit_status()
        client.close()

        self.finished.emit(
            True,
            f"✓ SSH key installed on {self._user}@{self._host}.\n"
            f"  Private key : {key_path}\n"
            f"  Public key  : {pub_path}\n"
            f"Click 'Test SSH Connection' to verify.",
        )

    # ── helpers ────────────────────────────────────────────────────────────────

    def _generate_key(self, key_path: Path, pub_path: Path) -> str:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives import serialization
            priv = Ed25519PrivateKey.generate()
            key_path.write_bytes(
                priv.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
            )
            key_path.chmod(0o600)
            pub_str = priv.public_key().public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH,
            ).decode()
        except Exception:
            # Fallback: RSA via paramiko (always available)
            import paramiko, io
            rsa = paramiko.RSAKey.generate(bits=4096)
            buf = io.StringIO()
            rsa.write_private_key(buf)
            key_path.write_text(buf.getvalue())
            key_path.chmod(0o600)
            pub_str = f"ssh-rsa {rsa.get_base64()}"
        pub_path.write_text(pub_str + " easybluesky\n")
        return pub_str

    def _read_public_key(self, key_path: Path, pub_path: Path) -> str:
        if pub_path.exists():
            return pub_path.read_text().strip()
        import paramiko
        for cls in [paramiko.Ed25519Key, paramiko.RSAKey,
                    paramiko.ECDSAKey, paramiko.DSSKey]:
            try:
                k = cls(filename=str(key_path))
                return f"{k.get_name()} {k.get_base64()}"
            except Exception:
                continue
        raise ValueError(f"Cannot read public key from {key_path}")


# ── MongoDB setup checker ─────────────────────────────────────────────────────

class _MongoCheckWorker(QThread):
    """SSH to the RE machine and run a MongoDB + Python package diagnostic."""
    line_ready = pyqtSignal(str, str)   # (text, color)  '#2ca02c'=OK '#d62728'=fail etc.
    finished_ok = pyqtSignal(bool)      # True if all checks passed

    # One-liner Python script executed on the remote machine.
    # Uses only stdlib + packages we want to verify — no bluesky imports needed.
    _DIAG = """\
python3 - <<'PYEOF'
import sys, subprocess, importlib

# ── 1. mongod running ────────────────────────────────────────────────────────
r = subprocess.run(['pgrep', '-x', 'mongod'], capture_output=True)
if r.returncode == 0:
    print('OK:mongod is running (pid', r.stdout.decode().strip(), ')')
else:
    # Try systemctl as fallback
    r2 = subprocess.run(['systemctl', 'is-active', 'mongod'],
                        capture_output=True, text=True)
    if r2.stdout.strip() == 'active':
        print('OK:mongod service is active')
    else:
        print('FAIL:mongod is NOT running')
        print('CMD:sudo systemctl start mongod')
        print('CMD:sudo systemctl enable mongod')

# ── 2. pymongo ───────────────────────────────────────────────────────────────
try:
    import pymongo
    print('OK:pymongo', pymongo.version, 'installed')
    # Try connecting
    try:
        c = pymongo.MongoClient('MONGO_HOST', MONGO_PORT,
                                serverSelectionTimeoutMS=3000)
        c.admin.command('ping')
        c.close()
        print('OK:MongoDB connection to MONGO_HOST:MONGO_PORT succeeded')
    except Exception as e:
        print('FAIL:MongoDB connection to MONGO_HOST:MONGO_PORT failed -', str(e)[:120])
except ImportError:
    print('FAIL:pymongo not installed')
    print('CMD:pip install pymongo')

PYEOF"""

    def __init__(self, settings: dict, mongo_host: str, mongo_port: int,
                 conda_env: str, conda_path: str, parent=None):
        super().__init__(parent)
        self._settings    = settings
        self._mongo_host  = mongo_host
        self._mongo_port  = mongo_port
        self._conda_env   = conda_env
        self._conda_path  = conda_path

    def run(self):
        script = (
            self._DIAG
            .replace("MONGO_HOST", self._mongo_host)
            .replace("MONGO_PORT", str(self._mongo_port))
        )
        # Wrap in conda activation if needed
        if self._conda_env:
            base = (self._conda_path or "~/miniconda3").replace("~", "$HOME")
            prefix = (
                f"source {base}/etc/profile.d/conda.sh 2>/dev/null; "
                f"conda activate {self._conda_env} 2>/dev/null; "
            )
            script = prefix + script

        all_ok = True
        try:
            from .ssh_manager import _get_client
            client = _get_client(self._settings)
            _, stdout, stderr = client.exec_command(script, timeout=20)
            for raw_line in stdout:
                line = raw_line.rstrip()
                if line.startswith("OK:"):
                    self.line_ready.emit("✓  " + line[3:], "#2ca02c")
                elif line.startswith("FAIL:"):
                    self.line_ready.emit("✗  " + line[5:], "#d62728")
                    all_ok = False
                elif line.startswith("CMD:"):
                    self.line_ready.emit("   → " + line[4:], "#ff7f0e")
                elif line.strip():
                    self.line_ready.emit("   " + line, "#888888")
            err = stderr.read().decode(errors="replace").strip()
            if err:
                for el in err.splitlines():
                    if el.strip():
                        self.line_ready.emit("   " + el, "#888888")
            client.close()
        except Exception as exc:
            self.line_ready.emit(f"✗  SSH error: {exc}", "#d62728")
            all_ok = False
        self.finished_ok.emit(all_ok)


class _MongoCheckDialog(QDialog):
    """Show the MongoDB diagnostic output in a small scrollable log."""

    def __init__(self, settings: dict, db_name: str,
                 mongo_host: str, mongo_port: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MongoDB Setup Check")
        self.setMinimumSize(520, 340)

        conda_env  = settings.get("conda_env",  "").strip()
        conda_path = settings.get("conda_path", "~/miniconda3").strip()

        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        info = QLabel(
            f"Checking MongoDB on  <b>{settings.get('host', 'localhost')}</b>"
            f"  (database: <b>{db_name}</b>)"
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        from PyQt6.QtWidgets import QPlainTextEdit
        from PyQt6.QtGui import QFont, QTextCharFormat, QColor, QTextCursor
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        f = QFont("Menlo")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(11)
        self._log.setFont(f)
        self._log.setMaximumBlockCount(200)
        lay.addWidget(self._log, 1)

        self._summary = QLabel("Running checks…")
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)

        # Install-help section (hidden until a failure is found)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        self._help_label = QLabel(
            "<b>Manual installation on the RE machine:</b><br>"
            "<b>1. MongoDB server</b> (run as root / sudo):<br>"
            "<code style='color:#ff7f0e'>  # Ubuntu / Debian<br>"
            "  sudo apt-get install -y mongodb-org<br>"
            "  sudo systemctl enable --now mongod</code><br><br>"
            "<b>2. Python packages</b> (in the conda env):<br>"
            "<code style='color:#ff7f0e'>  pip install pymongo</code>"
        )
        self._help_label.setTextFormat(Qt.TextFormat.RichText)
        self._help_label.setWordWrap(True)
        self._help_label.setVisible(False)
        lay.addWidget(self._help_label)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        lay.addWidget(btn_close, 0, Qt.AlignmentFlag.AlignRight)

        self._worker = _MongoCheckWorker(
            settings, mongo_host, mongo_port,
            conda_env, conda_path, parent=self,
        )
        self._worker.line_ready.connect(self._append_line)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.start()

    def _append_line(self, text: str, color: str):
        from PyQt6.QtGui import QTextCharFormat, QColor, QTextCursor
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(text + "\n", fmt)
        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()

    def _on_done(self, all_ok: bool):
        if all_ok:
            self._summary.setText("✓  All checks passed — MongoDB is ready.")
            self._summary.setStyleSheet("color: #2ca02c; font-weight: bold;")
        else:
            self._summary.setText(
                "✗  One or more checks failed.  "
                "Run the commands shown in orange on the RE machine."
            )
            self._summary.setStyleSheet("color: #d62728; font-weight: bold;")
            self._help_label.setVisible(True)


# ── Remote path browser ───────────────────────────────────────────────────────

class _SFTPConnectWorker(QThread):
    """Open an SFTP channel to the remote host in a background thread."""
    connected = pyqtSignal(object)   # emits (sftp, home_dir, ssh_client)
    error     = pyqtSignal(str)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings

    def run(self):
        try:
            from .ssh_manager import _get_client
            client = _get_client(self._settings)
            sftp = client.open_sftp()
            _, stdout, _ = client.exec_command("echo $HOME")
            home = stdout.read().decode().strip() or "/home"
            self.connected.emit((sftp, home, client))
        except Exception as exc:
            self.error.emit(str(exc))


class RemotePathBrowser(QDialog):
    """Browse the remote Linux filesystem over SSH/SFTP and select a directory.

    Uses the active connection profile's SSH credentials.  The caller reads
    ``selected_path`` after the dialog is accepted.
    """

    def __init__(self, settings: dict, initial_path: str = "~", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Browse Remote Path")
        self.setMinimumSize(520, 400)
        self._settings      = settings
        self._initial_path  = initial_path
        self._sftp          = None
        self._client        = None
        self._home          = ""
        self._current_path  = ""
        self.selected_path  = ""
        self._build()
        self._start_connect()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        self._status_lbl = QLabel("Connecting via SSH…")
        self._status_lbl.setObjectName("dim_text")
        lay.addWidget(self._status_lbl)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("/path/on/remote/machine")
        self._path_edit.returnPressed.connect(self._on_path_entered)
        btn_go = QPushButton("Go")
        btn_go.setMaximumWidth(40)
        btn_go.clicked.connect(self._on_path_entered)
        btn_up = QPushButton("↑ Up")
        btn_up.setMaximumWidth(60)
        btn_up.clicked.connect(self._go_up)
        path_row.addWidget(self._path_edit)
        path_row.addWidget(btn_go)
        path_row.addWidget(btn_up)
        lay.addLayout(path_row)

        self._dir_list = QListWidget()
        self._dir_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._dir_list.setEnabled(False)
        lay.addWidget(self._dir_list, 1)

        btn_row = QHBoxLayout()
        self._btn_new = QPushButton("New Folder…")
        self._btn_new.setEnabled(False)
        self._btn_new.clicked.connect(self._on_new_folder)
        self._btn_select = QPushButton("Select This Directory")
        self._btn_select.setDefault(True)
        self._btn_select.setEnabled(False)
        self._btn_select.clicked.connect(self._on_select)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_new)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self._btn_select)
        lay.addLayout(btn_row)

    def _start_connect(self):
        self._worker = _SFTPConnectWorker(self._settings, parent=self)
        self._worker.connected.connect(self._on_connected)
        self._worker.error.connect(self._on_connect_error)
        self._worker.start()

    def _on_connected(self, args):
        self._sftp, self._home, self._client = args
        start = self._initial_path or self._home
        start = start.replace("~", self._home)
        self._navigate(start)

    def _on_connect_error(self, msg: str):
        self._status_lbl.setText(f"✗  SSH error: {msg}")
        self._status_lbl.setStyleSheet("color: #d62728;")

    def _navigate(self, path: str):
        if not self._sftp:
            return
        import stat as _stat
        try:
            attrs = self._sftp.listdir_attr(path)
        except Exception as exc:
            self._status_lbl.setText(f"Cannot open {path}: {exc}")
            self._status_lbl.setStyleSheet("color: #d62728;")
            return
        dirs = sorted(
            [a.filename for a in attrs
             if _stat.S_ISDIR(a.st_mode) and not a.filename.startswith(".")],
            key=str.lower,
        )
        self._current_path = path
        self._path_edit.setText(path)
        self._status_lbl.setText(path)
        self._status_lbl.setStyleSheet("")
        self._dir_list.clear()
        self._dir_list.setEnabled(True)
        self._btn_select.setEnabled(True)
        self._btn_new.setEnabled(True)
        for d in dirs:
            self._dir_list.addItem(f"📁  {d}")

    def _on_item_double_clicked(self, item):
        name = item.text().replace("📁  ", "")
        self._navigate(self._current_path.rstrip("/") + "/" + name)

    def _on_path_entered(self):
        path = self._path_edit.text().strip()
        if path:
            self._navigate(path.replace("~", self._home))

    def _go_up(self):
        if self._current_path:
            parent = self._current_path.rsplit("/", 1)[0] or "/"
            self._navigate(parent)

    def _on_new_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        new_path = self._current_path.rstrip("/") + "/" + name.strip()
        try:
            self._sftp.mkdir(new_path)
            self._navigate(self._current_path)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not create folder:\n{exc}")

    def _on_select(self):
        self.selected_path = self._current_path
        self.accept()

    def closeEvent(self, event):
        if hasattr(self, "_worker") and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        event.accept()


# ── Connection dialog ──────────────────────────────────────────────────────────

class ConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(640)
        self.setMinimumHeight(560)
        self._settings = load_connection()
        self._current_row = None
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setSpacing(8)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        note = QLabel(
            "Connection settings for the Bluesky RE Manager.\n"
            "Changes take effect after clicking OK (reconnects automatically)."
        )
        note.setWordWrap(True)
        note.setObjectName("dim_text")
        lay.addWidget(note)

        # ── Host / IP ──────────────────────────────────────────────────────────
        host_form = QFormLayout()
        host_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        host_form.setHorizontalSpacing(12)
        self._host = QLineEdit(self._settings.get("host", "localhost"))
        self._host.setPlaceholderText("localhost or 192.168.1.50")
        self._host.textChanged.connect(self._update_zmq_label)
        host_form.addRow("Host / IP:", self._host)
        lay.addLayout(host_form)

        # ── SSH section ────────────────────────────────────────────────────────
        sep_ssh = QFrame()
        sep_ssh.setFrameShape(QFrame.Shape.HLine)
        sep_ssh.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep_ssh)

        ssh_title = QLabel("Remote SSH Management")
        ssh_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(ssh_title)

        ssh_note = QLabel(
            "Used only when Host is a remote machine and the profile is not Local.\n"
            "SSH key authentication — no passwords stored or committed to git.\n"
            "Settings saved to ~/.easy_bluesky/connection.json (local only)."
        )
        ssh_note.setWordWrap(True)
        ssh_note.setObjectName("dim_text")
        lay.addWidget(ssh_note)

        ssh_form = QFormLayout()
        ssh_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        ssh_form.setHorizontalSpacing(12)

        self._ssh_user = QLineEdit(self._settings.get("ssh_user", ""))
        self._ssh_user.setPlaceholderText("username on the remote machine")
        ssh_form.addRow("SSH user:", self._ssh_user)

        self._ssh_port = NoScrollSpinBox()
        self._ssh_port.setRange(1, 65535)
        self._ssh_port.setValue(self._settings.get("ssh_port", 22))
        ssh_form.addRow("SSH port:", self._ssh_port)

        key_row = QHBoxLayout()
        self._ssh_key = QLineEdit(self._settings.get("ssh_key_path", "~/.ssh/id_ed25519"))
        self._ssh_key.setPlaceholderText("~/.ssh/id_ed25519")
        btn_browse = QPushButton("Browse…")
        btn_browse.setMaximumWidth(70)
        btn_browse.clicked.connect(self._browse_key)
        btn_setup_key = QPushButton("Setup SSH Key…")
        btn_setup_key.setToolTip(
            "Generate an Ed25519 key (if absent) and install it on the remote machine.\n"
            "You will be prompted for your SSH password once — it is never stored."
        )
        btn_setup_key.clicked.connect(self._setup_ssh_key)
        key_row.addWidget(self._ssh_key)
        key_row.addWidget(btn_browse)
        key_row.addWidget(btn_setup_key)
        ssh_form.addRow("Private key:", key_row)

        self._ssh_service = QLineEdit(self._settings.get("ssh_service", ""))
        self._ssh_service.setPlaceholderText("systemd service, or empty for direct restart")
        ssh_form.addRow("Service name:", self._ssh_service)

        self._conda_env = QLineEdit(self._settings.get("conda_env", ""))
        self._conda_env.setPlaceholderText("bluesky  (leave empty if not using conda)")
        ssh_form.addRow("Conda env:", self._conda_env)

        self._conda_path = QLineEdit(self._settings.get("conda_path", "~/miniconda3"))
        self._conda_path.setPlaceholderText("~/miniconda3  or  ~/miniforge3")
        ssh_form.addRow("Conda path:", self._conda_path)

        lay.addLayout(ssh_form)

        btn_test = QPushButton("Test SSH Connection")
        btn_test.clicked.connect(self._test_ssh)
        lay.addWidget(btn_test, alignment=Qt.AlignmentFlag.AlignLeft)

        self._ssh_result = QLabel("")
        self._ssh_result.setWordWrap(True)
        lay.addWidget(self._ssh_result)

        # ── EPICS CA section ───────────────────────────────────────────────────
        sep_ca = QFrame()
        sep_ca.setFrameShape(QFrame.Shape.HLine)
        sep_ca.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep_ca)

        ca_title = QLabel("EPICS CA Network")
        ca_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(ca_title)

        ca_note = QLabel(
            "Sets EPICS_CA_ADDR_LIST and EPICS_CA_AUTO_ADDR_LIST for pyepics.\n"
            "Changes to the address list take full effect on the next app restart."
        )
        ca_note.setWordWrap(True)
        ca_note.setObjectName("dim_text")
        lay.addWidget(ca_note)

        ca_form = QFormLayout()
        ca_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        ca_form.setHorizontalSpacing(12)

        self._ca_addr_list = QLineEdit(
            self._settings.get("epics_ca_addr_list", "")
        )
        self._ca_addr_list.setPlaceholderText(
            "e.g. 10.0.0.255 192.168.1.100  (leave empty to use system default)"
        )
        ca_form.addRow("CA addr list:", self._ca_addr_list)

        self._ca_auto = QCheckBox("Auto addr list  (broadcast)")
        self._ca_auto.setChecked(self._settings.get("epics_ca_auto_addr_list", True))
        self._ca_auto.setToolTip(
            "EPICS_CA_AUTO_ADDR_LIST=YES — CA also broadcasts on all local subnets.\n"
            "Set to NO when you only want to reach the addresses listed above."
        )
        ca_form.addRow("", self._ca_auto)

        lay.addLayout(ca_form)

        # ── Registry section ───────────────────────────────────────────────────
        sep_reg = QFrame()
        sep_reg.setFrameShape(QFrame.Shape.HLine)
        sep_reg.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep_reg)

        reg_title = QLabel("Instance Registry")
        reg_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(reg_title)

        reg_note = QLabel(
            "The registry is a shared list of RE Manager instances stored on the "
            "beamline host (registry.json).  Use Import to pull instances from the "
            "host into your local profiles, or Publish to push a profile up to the registry."
        )
        reg_note.setWordWrap(True)
        reg_note.setObjectName("dim_text")
        lay.addWidget(reg_note)

        reg_btn_row = QHBoxLayout()
        btn_import = QPushButton("⬇  Import from Registry…")
        btn_import.setToolTip(
            "SSH to the active profile's host, read registry.json, and\n"
            "add the instances you select as local profiles."
        )
        btn_import.clicked.connect(self._on_import_from_registry)
        reg_btn_row.addWidget(btn_import, alignment=Qt.AlignmentFlag.AlignLeft)
        lay.addLayout(reg_btn_row)

        self._reg_status = QLabel("")
        self._reg_status.setWordWrap(True)
        lay.addWidget(self._reg_status)

        # ── ESAF Server section ────────────────────────────────────────────────
        sep_esaf = QFrame()
        sep_esaf.setFrameShape(QFrame.Shape.HLine)
        sep_esaf.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep_esaf)

        esaf_title = QLabel("ESAF Server")
        esaf_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(esaf_title)

        esaf_note = QLabel(
            "Optional service that stores ESAF records and PI groups for the beamline.\n"
            "Run  uvicorn esaf_server.main:app  on the beamline machine or a lab server.\n"
            "Leave URL empty to use local PDF parsing and cached records only."
        )
        esaf_note.setWordWrap(True)
        esaf_note.setObjectName("dim_text")
        lay.addWidget(esaf_note)

        esaf_form = QFormLayout()
        esaf_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        esaf_form.setHorizontalSpacing(12)

        self._esaf_url = QLineEdit(self._settings.get("esaf_server_url", ""))
        self._esaf_url.setPlaceholderText("http://beamline-host:8765  (leave empty to disable)")
        esaf_form.addRow("Server URL:", self._esaf_url)

        self._esaf_key = QLineEdit(self._settings.get("esaf_api_key", ""))
        self._esaf_key.setPlaceholderText("API key for write operations (optional)")
        self._esaf_key.setEchoMode(QLineEdit.EchoMode.Password)
        esaf_form.addRow("API key:", self._esaf_key)

        lay.addLayout(esaf_form)

        esaf_btn_row = QHBoxLayout()
        btn_test_esaf = QPushButton("Test ESAF Server")
        btn_test_esaf.clicked.connect(self._test_esaf_server)
        btn_start_esaf = QPushButton("Start ESAF Server via SSH…")
        btn_start_esaf.setToolTip(
            "SSH to the beamline machine and start the ESAF server\n"
            "using uvicorn in the background."
        )
        btn_start_esaf.clicked.connect(self._start_esaf_server)
        esaf_btn_row.addWidget(btn_test_esaf)
        esaf_btn_row.addWidget(btn_start_esaf)
        esaf_btn_row.addStretch()
        lay.addLayout(esaf_btn_row)

        self._esaf_status = QLabel("")
        self._esaf_status.setWordWrap(True)
        lay.addWidget(self._esaf_status)

        # ── Profiles section ───────────────────────────────────────────────────
        sep_prof = QFrame()
        sep_prof.setFrameShape(QFrame.Shape.HLine)
        sep_prof.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep_prof)

        prof_title = QLabel("Profiles")
        prof_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(prof_title)

        prof_note = QLabel(
            "Each profile connects to a separate RE Manager instance "
            "with its own ports and devices file."
        )
        prof_note.setWordWrap(True)
        prof_note.setObjectName("dim_text")
        lay.addWidget(prof_note)

        prof_h = QHBoxLayout()
        prof_h.setSpacing(8)

        # Left: list
        left_w = QWidget()
        left_w.setMaximumWidth(170)
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)

        self._profile_list = QListWidget()
        self._profile_list.currentRowChanged.connect(self._on_profile_selected)
        left_lay.addWidget(self._profile_list)

        btn_row = QHBoxLayout()
        self._btn_add = QPushButton("＋ Add")
        self._btn_remove = QPushButton("✕ Remove")
        self._btn_add.clicked.connect(self._on_add_profile)
        self._btn_remove.clicked.connect(self._on_remove_profile)
        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(self._btn_remove)
        left_lay.addLayout(btn_row)

        prof_h.addWidget(left_w)

        # Right: profile editor form
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)

        self._prof_form = QFormLayout()
        self._prof_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._prof_form.setHorizontalSpacing(12)

        self._prof_name = QLineEdit()
        self._prof_name.setPlaceholderText("Profile name (e.g. ASWAXS, SURF)")
        self._prof_form.addRow("Name:", self._prof_name)

        self._prof_host = QLineEdit()
        self._prof_host.setPlaceholderText(
            "hostname or IP  (leave empty to use the global Host above)"
        )
        self._prof_host.textChanged.connect(self._update_zmq_label)
        self._prof_form.addRow("Host override:", self._prof_host)

        self._prof_is_local = QCheckBox("Local (runs on this computer)")
        self._prof_is_local.setToolTip(
            "RE Manager runs as a local subprocess.\n"
            "Starts and stops automatically with the app."
        )
        self._prof_is_local.toggled.connect(self._on_is_local_toggled)
        self._prof_form.addRow("", self._prof_is_local)

        self._prof_devices = QLineEdit()
        self._prof_devices.setPlaceholderText("devices.py")
        self._prof_form.addRow("Devices file:", self._prof_devices)

        self._prof_ctrl = NoScrollSpinBox()
        self._prof_ctrl.setRange(1, 65535)
        self._prof_form.addRow("Control port:", self._prof_ctrl)

        self._prof_info = NoScrollSpinBox()
        self._prof_info.setRange(1, 65535)
        self._prof_form.addRow("Info port:", self._prof_info)

        self._prof_doc = NoScrollSpinBox()
        self._prof_doc.setRange(1, 65535)
        self._prof_form.addRow("Doc stream port:", self._prof_doc)

        self._prof_procserv = NoScrollSpinBox()
        self._prof_procserv.setRange(1, 65535)
        self._prof_form.addRow("procServ port:", self._prof_procserv)

        # ── MongoDB / databroker section ───────────────────────────────────────
        _sep_mongo = QFrame()
        _sep_mongo.setFrameShape(QFrame.Shape.HLine)
        _sep_mongo.setFrameShadow(QFrame.Shadow.Sunken)
        self._prof_form.addRow(_sep_mongo)

        _mongo_title = QLabel("MongoDB (Databroker)")
        _mongo_title.setStyleSheet("font-weight: bold;")
        self._prof_form.addRow(_mongo_title)

        _mongo_note = QLabel(
            "Leave Database empty to disable MongoDB subscription for this profile."
        )
        _mongo_note.setWordWrap(True)
        _mongo_note.setObjectName("dim_text")
        self._prof_form.addRow(_mongo_note)

        self._prof_mongo_db = QLineEdit()
        self._prof_mongo_db.setPlaceholderText(
            "e.g. aswaxs_real  (empty = disabled)"
        )
        self._prof_mongo_db.setToolTip(
            "Each profile writes to its own MongoDB database.\n"
            "The RE manager subscribes suitcase.mongo_normalized on startup."
        )
        self._prof_form.addRow("Database:", self._prof_mongo_db)

        self._prof_mongo_host = QLineEdit()
        self._prof_mongo_host.setPlaceholderText("localhost  (default)")
        self._prof_mongo_host.setToolTip(
            "MongoDB server hostname or IP as seen from the RE machine.\n"
            "Leave empty to use localhost on the RE machine."
        )
        self._prof_form.addRow("Mongo host:", self._prof_mongo_host)

        self._prof_mongo_port = NoScrollSpinBox()
        self._prof_mongo_port.setRange(1, 65535)
        self._prof_mongo_port.setValue(27017)
        self._prof_form.addRow("Mongo port:", self._prof_mongo_port)

        btn_test_mongo = QPushButton("Test MongoDB Setup…")
        btn_test_mongo.setToolTip(
            "SSH to the RE machine and check whether MongoDB is running\n"
            "and the required Python packages are installed."
        )
        btn_test_mongo.clicked.connect(self._on_test_mongo)
        self._prof_form.addRow("", btn_test_mongo)

        self._mongo_result = QLabel("")
        self._mongo_result.setWordWrap(True)
        self._prof_form.addRow("", self._mongo_result)

        # ── Local data directory section ───────────────────────────────────────
        _sep_local_data = QFrame()
        _sep_local_data.setFrameShape(QFrame.Shape.HLine)
        _sep_local_data.setFrameShadow(QFrame.Shadow.Sunken)
        self._prof_form.addRow(_sep_local_data)

        _local_data_title = QLabel("Local Experiment Root")
        _local_data_title.setStyleSheet("font-weight: bold;")
        self._prof_form.addRow(_local_data_title)

        _local_data_note = QLabel(
            "Root folder where experiment subfolders are created on this machine.\n"
            "Leave empty to use the default  ~/.easy_bluesky/experiments/.\n"
            "Set this to an NFS/shared network mount if the same drive is accessible\n"
            "from both this machine and the RE machine."
        )
        _local_data_note.setWordWrap(True)
        _local_data_note.setObjectName("dim_text")
        self._prof_form.addRow(_local_data_note)

        _local_path_row = QHBoxLayout()
        self._prof_local_data_root = QLineEdit()
        self._prof_local_data_root.setPlaceholderText(
            "~/.easy_bluesky/experiments  (default, leave empty)"
        )
        _btn_browse_local_root = QPushButton("Browse…")
        _btn_browse_local_root.setMaximumWidth(70)
        _btn_browse_local_root.clicked.connect(self._browse_local_data_root)
        _local_path_row.addWidget(self._prof_local_data_root)
        _local_path_row.addWidget(_btn_browse_local_root)
        self._prof_form.addRow("Local root:", _local_path_row)

        # ── Remote data directory section ──────────────────────────────────────
        _sep_remote = QFrame()
        _sep_remote.setFrameShape(QFrame.Shape.HLine)
        _sep_remote.setFrameShadow(QFrame.Shadow.Sunken)
        self._prof_form.addRow(_sep_remote)

        _remote_title = QLabel("Remote Data Directory")
        _remote_title.setStyleSheet("font-weight: bold;")
        self._prof_form.addRow(_remote_title)

        _remote_note = QLabel(
            "Base directory on the RE machine where detector data is saved.\n"
            "EasyBluesky appends the experiment name and injects the result\n"
            "as  remote_exp_dir  in every plan's metadata."
        )
        _remote_note.setWordWrap(True)
        _remote_note.setObjectName("dim_text")
        self._prof_form.addRow(_remote_note)

        _remote_path_row = QHBoxLayout()
        self._prof_remote_data_root = QLineEdit()
        self._prof_remote_data_root.setPlaceholderText(
            "/home/chem_epics/data  or  ~/data  (leave empty to disable)"
        )
        _btn_browse_remote_root = QPushButton("Browse…")
        _btn_browse_remote_root.setMaximumWidth(70)
        _btn_browse_remote_root.clicked.connect(self._browse_remote_data_root)
        _remote_path_row.addWidget(self._prof_remote_data_root)
        _remote_path_row.addWidget(_btn_browse_remote_root)
        self._prof_form.addRow("Data root:", _remote_path_row)

        for _sb in (self._prof_ctrl, self._prof_info):
            _sb.valueChanged.connect(self._update_zmq_label)

        right_lay.addLayout(self._prof_form)

        self._prof_local_note = QLabel(
            "RE Manager starts and stops automatically with the app. No SSH needed."
        )
        self._prof_local_note.setObjectName("dim_text")
        self._prof_local_note.setWordWrap(True)
        self._prof_local_note.setVisible(False)
        right_lay.addWidget(self._prof_local_note)

        prof_btn_row = QHBoxLayout()
        btn_auto = QPushButton("Auto-assign Ports")
        btn_auto.setToolTip("Find 4 free ports and assign them to this profile")
        btn_auto.clicked.connect(self._on_auto_assign_ports)
        prof_btn_row.addWidget(btn_auto)
        btn_publish = QPushButton("⬆  Publish to Registry…")
        btn_publish.setToolTip(
            "Push this profile to the registry stored on its host.\n"
            "Other clients can then import it."
        )
        btn_publish.clicked.connect(self._on_publish_to_registry)
        prof_btn_row.addWidget(btn_publish)
        prof_btn_row.addStretch()
        right_lay.addLayout(prof_btn_row)

        self._auto_assign_note = QLabel("")
        self._auto_assign_note.setObjectName("dim_text")
        right_lay.addWidget(self._auto_assign_note)

        self._zmq_addr_label = QLabel("")
        self._zmq_addr_label.setObjectName("dim_text")
        self._zmq_addr_label.setWordWrap(True)
        right_lay.addWidget(self._zmq_addr_label)
        right_lay.addStretch()

        prof_h.addWidget(right_w, 1)
        lay.addLayout(prof_h)

        # ── Dialog buttons ─────────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self._populate_profile_list()
        active = self._settings.get("active_profile", "Default")
        selected = False
        for i in range(self._profile_list.count()):
            if self._profile_list.item(i).data(Qt.ItemDataRole.UserRole) == active:
                self._profile_list.setCurrentRow(i)
                selected = True
                break
        if not selected and self._profile_list.count() > 0:
            self._profile_list.setCurrentRow(0)

    def _on_is_local_toggled(self, checked: bool):
        self._prof_local_note.setVisible(checked)
        self._prof_form.setRowVisible(self._prof_procserv, not checked)
        self._update_zmq_label()

    def _populate_profile_list(self):
        active = self._settings.get("active_profile", "Default")
        self._profile_list.blockSignals(True)
        self._profile_list.clear()
        for p in self._settings.get("profiles", []):
            name = p.get("name", "")
            label = f"{name}  [LOCAL]" if p.get("is_local") else name
            if name == active:
                label += "  [active]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            if name == active:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self._profile_list.addItem(item)
        self._profile_list.blockSignals(False)

    def _on_profile_selected(self, row: int):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_editor()
        self._current_row = row
        if row < 0:
            return
        profiles = self._settings.get("profiles", [])
        if row >= len(profiles):
            return
        p = profiles[row]
        self._prof_name.setText(p.get("name", ""))
        self._prof_host.setText(p.get("host", ""))
        self._prof_is_local.setChecked(p.get("is_local", False))
        self._prof_devices.setText(p.get("devices_file", "devices.py"))
        self._prof_ctrl.setValue(p.get("control_port", _PROFILE_DEFAULTS["control_port"]))
        self._prof_info.setValue(p.get("info_port", _PROFILE_DEFAULTS["info_port"]))
        self._prof_doc.setValue(p.get("doc_port", _PROFILE_DEFAULTS["doc_port"]))
        self._prof_procserv.setValue(p.get("procserv_port", _PROFILE_DEFAULTS["procserv_port"]))
        self._prof_mongo_db.setText(p.get("mongo_db", ""))
        self._prof_mongo_host.setText(p.get("mongo_host", ""))
        self._prof_mongo_port.setValue(p.get("mongo_port", 27017))
        self._prof_local_data_root.setText(p.get("local_data_root", ""))
        self._prof_remote_data_root.setText(p.get("remote_data_root", ""))
        # Show/hide procServ row based on is_local
        self._on_is_local_toggled(p.get("is_local", False))
        self._auto_assign_note.setText("")
        self._update_zmq_label()

    def _save_current_editor(self):
        row = self._current_row
        if row is None or row < 0:
            return
        profiles = self._settings.get("profiles", [])
        if row >= len(profiles):
            return

        new_name = self._prof_name.text().strip() or f"Profile {row + 1}"
        old_name = profiles[row].get("name", "")
        is_local = self._prof_is_local.isChecked()

        profiles[row] = {
            "name":             new_name,
            "host":             self._prof_host.text().strip(),
            "devices_file":     self._prof_devices.text().strip() or "devices.py",
            "is_local":         is_local,
            "control_port":     self._prof_ctrl.value(),
            "info_port":        self._prof_info.value(),
            "doc_port":         self._prof_doc.value(),
            "procserv_port":    self._prof_procserv.value(),
            "mongo_db":         self._prof_mongo_db.text().strip(),
            "mongo_host":       self._prof_mongo_host.text().strip(),
            "mongo_port":       self._prof_mongo_port.value(),
            "local_data_root":  self._prof_local_data_root.text().strip(),
            "remote_data_root": self._prof_remote_data_root.text().strip(),
        }

        if old_name == self._settings.get("active_profile") and new_name != old_name:
            self._settings["active_profile"] = new_name

        item = self._profile_list.item(row)
        if item:
            current_active = self._settings.get("active_profile", "Default")
            label = f"{new_name}  [LOCAL]" if is_local else new_name
            if new_name == current_active:
                label += "  [active]"
            item.setText(label)
            item.setData(Qt.ItemDataRole.UserRole, new_name)
            font = item.font()
            font.setBold(new_name == current_active)
            item.setFont(font)

    def _on_add_profile(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_editor()

        profiles = self._settings.get("profiles", [])
        used = _all_used_ports(self._settings)
        start = (max(used) + 1) if used else 60615
        ports = find_free_ports(count=4, start=start, used=used)

        n = len(profiles) + 1
        new_profile = {
            "name": f"Profile {n}",
            "devices_file": "devices.py",
            "is_local": False,
            "control_port": ports[0] if len(ports) > 0 else 60700,
            "info_port":    ports[1] if len(ports) > 1 else 60701,
            "doc_port":     ports[2] if len(ports) > 2 else 60702,
            "procserv_port": ports[3] if len(ports) > 3 else 60703,
        }
        profiles.append(new_profile)
        self._settings["profiles"] = profiles

        self._current_row = None
        self._profile_list.blockSignals(True)
        label = f"{new_profile['name']}  [LOCAL]" if new_profile["is_local"] else new_profile["name"]
        _new_item = QListWidgetItem(label)
        _new_item.setData(Qt.ItemDataRole.UserRole, new_profile["name"])
        self._profile_list.addItem(_new_item)
        self._profile_list.blockSignals(False)

        new_row = len(profiles) - 1
        self._profile_list.setCurrentRow(new_row)

    def _on_remove_profile(self):
        row = self._profile_list.currentRow()
        if row < 0:
            return
        profiles = self._settings.get("profiles", [])
        if len(profiles) <= 1:
            return

        removed_name = profiles[row].get("name", "")
        profiles.pop(row)
        self._settings["profiles"] = profiles

        if self._settings.get("active_profile") == removed_name:
            self._settings["active_profile"] = profiles[0]["name"] if profiles else "Default"

        self._current_row = None
        self._profile_list.blockSignals(True)
        self._profile_list.takeItem(row)
        self._profile_list.blockSignals(False)

        new_row = min(row, self._profile_list.count() - 1)
        if new_row >= 0:
            self._profile_list.setCurrentRow(new_row)
        else:
            self._current_row = None

    def _on_auto_assign_ports(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_editor()

        row = self._profile_list.currentRow()
        profiles = self._settings.get("profiles", [])

        used = set()
        for i, p in enumerate(profiles):
            if i != row:
                for key in ("control_port", "info_port", "doc_port", "procserv_port"):
                    val = p.get(key)
                    if isinstance(val, int):
                        used.add(val)

        start = (max(used) + 1) if used else 60615

        # For remote profiles, ask the remote host directly via SSH which
        # ports are already in use — one round-trip, no per-port TCP timeout.
        host = self._host.text().strip() or "localhost"
        is_local = self._prof_is_local.isChecked()
        if not is_local and host.lower() not in ("localhost", "127.0.0.1", "::1", ""):
            self._auto_assign_note.setText(f"Checking ports on {host} via SSH…")
            from PyQt6.QtWidgets import QApplication
            QApplication.processEvents()
            settings = self._collect_top_level()
            ports, note = find_free_ports_remote(settings, count=4, start=start, used=used)
        else:
            ports = find_free_ports(count=4, start=start, used=used)
            note = ""

        if len(ports) >= 4:
            self._prof_ctrl.setValue(ports[0])
            self._prof_info.setValue(ports[1])
            self._prof_doc.setValue(ports[2])
            self._prof_procserv.setValue(ports[3])
            suffix = f"  ({note})" if note else ""
            self._auto_assign_note.setText(
                f"Assigned: ctrl={ports[0]}  info={ports[1]}"
                f"  doc={ports[2]}  procServ={ports[3]}{suffix}"
            )
        else:
            self._auto_assign_note.setText(
                f"Could not find enough free ports.  ({note})" if note
                else "Could not find enough free ports."
            )

    def _update_zmq_label(self):
        is_local = self._prof_is_local.isChecked()
        if is_local:
            h = "localhost"
        else:
            # Per-profile host takes priority over global host
            h = (self._prof_host.text().strip()
                 or self._host.text().strip()
                 or "localhost")
        ctrl = self._prof_ctrl.value()
        info = self._prof_info.value()
        self._zmq_addr_label.setText(
            f"Will connect to:  tcp://{h}:{ctrl}  (control)"
            f"  ·  tcp://{h}:{info}  (info)"
        )

    def _browse_key(self):
        start = str(Path(self._ssh_key.text()).expanduser().parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH Private Key", start, "All Files (*)"
        )
        if path:
            self._ssh_key.setText(path)

    def _test_ssh(self):
        from .ssh_manager import test_ssh_connection
        settings = self._collect_top_level()
        if is_local_host(settings):
            self._ssh_result.setText("Host is localhost — SSH not needed.")
            self._ssh_result.setStyleSheet("color: #888;")
            return
        self._ssh_result.setText("Testing…")
        ok, msg = test_ssh_connection(settings)
        self._ssh_result.setText(msg)
        self._ssh_result.setStyleSheet(
            "color: #2ca02c;" if ok else "color: #d62728;"
        )

    def _on_test_mongo(self):
        """SSH to the RE machine and verify MongoDB + Python package setup."""
        self._save_current_editor()
        profile  = self._current_mongo_profile()
        settings = self._collect_top_level()
        db       = profile.get("mongo_db", "").strip()
        host     = profile.get("mongo_host", "").strip() or "localhost"
        port     = profile.get("mongo_port", 27017)

        if not db:
            self._mongo_result.setText("Set a Database name first.")
            self._mongo_result.setStyleSheet("color: #d62728;")
            return

        self._mongo_result.setText("Checking…")
        self._mongo_result.setStyleSheet("color: #ff7f0e;")

        dlg = _MongoCheckDialog(settings, db, host, int(port), parent=self)
        dlg.exec()
        self._mongo_result.setText("")

    def _browse_local_data_root(self):
        """Open a local folder browser to select the local experiment root."""
        current = self._prof_local_data_root.text().strip()
        start = str(Path(current).expanduser()) if current else str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select Local Experiment Root", start)
        if path:
            self._prof_local_data_root.setText(path)

    def _browse_remote_data_root(self):
        """Open a file browser (local or remote SSH) to select the data root."""
        settings = self._collect_top_level()
        current  = self._prof_remote_data_root.text().strip() or "~"
        if is_local_host(settings):
            path = QFileDialog.getExistingDirectory(
                self, "Select Remote Data Root",
                str(Path(current).expanduser()) if current != "~" else str(Path.home()),
            )
            if path:
                self._prof_remote_data_root.setText(path)
            return
        host = settings.get("host", "").strip()
        if not host:
            QMessageBox.information(
                self, "No Host",
                "Set a host in the global SSH settings first, then use Browse."
            )
            return
        dlg = RemotePathBrowser(settings, initial_path=current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_path:
            self._prof_remote_data_root.setText(dlg.selected_path)

    def _current_mongo_profile(self) -> dict:
        row = self._current_row if self._current_row is not None else 0
        profiles = self._settings.get("profiles", [])
        return profiles[row] if 0 <= row < len(profiles) else {}

    def _setup_ssh_key(self):
        settings = self._collect_top_level()
        host = settings.get("host", "").strip()
        user = settings.get("ssh_user", "").strip()
        port = settings.get("ssh_port", 22)
        key_path = self._ssh_key.text().strip() or "~/.ssh/id_ed25519"

        if is_local_host(settings):
            self._ssh_result.setText("Host is localhost — SSH key setup not needed.")
            self._ssh_result.setStyleSheet("color: #888;")
            return
        if not host:
            self._ssh_result.setText("Enter a host address first.")
            self._ssh_result.setStyleSheet("color: #d62728;")
            return
        if not user:
            self._ssh_result.setText("Enter an SSH username first.")
            self._ssh_result.setStyleSheet("color: #d62728;")
            return

        password, ok = QInputDialog.getText(
            self, "SSH Password — One-Time Setup",
            f"Enter the SSH password for {user}@{host}\n"
            "(used only to install the key; never stored):",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not password:
            return

        self._ssh_result.setText("Setting up SSH key…")
        self._ssh_result.setStyleSheet("color: #888;")

        self._key_installer = _SshKeyInstaller(
            host, port, user, password, key_path, parent=self
        )
        self._key_installer.progress.connect(
            lambda msg: self._ssh_result.setText(msg)
        )
        self._key_installer.finished.connect(self._on_key_setup_done)
        self._key_installer.start()

    def _on_key_setup_done(self, success: bool, message: str):
        self._ssh_result.setText(message)
        self._ssh_result.setStyleSheet(
            "color: #2ca02c;" if success else "color: #d62728;"
        )
        if success:
            # Update the key path field to whatever was used
            installed_path = self._key_installer._key_path
            self._ssh_key.setText(str(installed_path))

    def _on_import_from_registry(self):
        settings = self._collect_top_level()
        # Use active profile's host as registry host
        profile = get_active_profile(settings)
        host = (profile.get("host", "").strip()
                or settings.get("host", "").strip())
        if not host:
            self._reg_status.setText(
                "✗  No host configured. Set a host in the active profile first."
            )
            self._reg_status.setStyleSheet("color: #d62728;")
            return
        reg_settings = dict(settings)
        reg_settings["registry_host"] = host
        dlg = _ImportFromRegistryDialog(reg_settings, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        instances = dlg.selected_instances()
        if not instances:
            return
        from .registry import merge_into_profiles
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_editor()
        profiles = self._settings.get("profiles", [])
        profiles, added, updated = merge_into_profiles(profiles, instances)
        self._settings["profiles"] = profiles
        # Remember the registry host for Registry Admin use
        self._settings["registry_host"] = host
        self._current_row = None
        self._populate_profile_list()
        active = self._settings.get("active_profile", "Default")
        for i in range(self._profile_list.count()):
            if self._profile_list.item(i).data(Qt.ItemDataRole.UserRole) == active:
                self._profile_list.setCurrentRow(i)
                break
        self._reg_status.setText(
            f"✓  Added {added} new profile(s), updated {updated} existing."
        )
        self._reg_status.setStyleSheet("color: #2ca02c;")

    def _on_publish_to_registry(self):
        if self._current_row is None or self._current_row < 0:
            return
        self._save_current_editor()
        settings = self._collect_top_level()
        profiles = self._settings.get("profiles", [])
        if self._current_row >= len(profiles):
            return
        profile = profiles[self._current_row]
        host = (profile.get("host", "").strip()
                or settings.get("host", "").strip())
        if not host:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "No host",
                "Set a host (Host override or global Host) in this profile first."
            )
            return
        reg_settings = dict(settings)
        reg_settings["registry_host"] = host
        dlg = _PublishToRegistryDialog(reg_settings, profile, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Remember the registry host so Registry Admin can reach it
            self._settings["registry_host"] = host

    def _test_esaf_server(self):
        url = self._esaf_url.text().strip()
        if not url:
            self._esaf_status.setText("No server URL configured.")
            self._esaf_status.setStyleSheet("color: #888;")
            return
        self._esaf_status.setText("Testing…")
        self._esaf_status.setStyleSheet("color: #888;")
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            import urllib.request as _ur
            import json as _json
            req = _ur.Request(f"{url.rstrip('/')}/api/esafs?limit=1", method="GET")
            with _ur.urlopen(req, timeout=5) as resp:
                _json.loads(resp.read())
            self._esaf_status.setText("✓  aps-esaf-fetcher server reachable")
            self._esaf_status.setStyleSheet("color: #2ca02c;")
        except Exception as exc:
            self._esaf_status.setText(f"✗  {exc}")
            self._esaf_status.setStyleSheet("color: #d62728;")

    def _start_esaf_server(self):
        settings = self._collect_top_level()
        url = self._esaf_url.text().strip()
        port = 8088
        try:
            port = int(url.rsplit(":", 1)[-1].split("/")[0])
        except Exception:
            pass
        if is_local_host(settings):
            self._esaf_status.setText(
                f"Host is localhost — start aps-esaf-fetcher manually:\n"
                f"  cd <aps-esaf-fetcher dir> && bash launch.sh"
            )
            self._esaf_status.setStyleSheet("color: #888;")
            return
        host = settings.get("host", "localhost")
        conda_env  = settings.get("conda_env", "").strip()
        conda_path = settings.get("conda_path", "~/miniconda3").strip()

        self._esaf_status.setText("Starting aps-esaf-fetcher via SSH…")
        self._esaf_status.setStyleSheet("color: #888;")
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            from .ssh_manager import _get_client
            client = _get_client(settings)
            cmd = ""
            if conda_env:
                base = conda_path.replace("~", "$HOME")
                cmd = (f"source {base}/etc/profile.d/conda.sh 2>/dev/null; "
                       f"conda activate {conda_env} 2>/dev/null; ")
            cmd += (f"cd ~/aps-esaf-fetcher && "
                    f"nohup uvicorn app.main:app "
                    f"--host 0.0.0.0 --port {port} "
                    f">> ~/.easy_bluesky/esaf_server.log 2>&1 &")
            _, stdout, _ = client.exec_command(cmd, timeout=10)
            stdout.channel.recv_exit_status()
            client.close()
            self._esaf_status.setText(
                f"✓  aps-esaf-fetcher start command sent to {host}:{port}.\n"
                f"   Wait a few seconds then click Test."
            )
            self._esaf_status.setStyleSheet("color: #2ca02c;")
        except Exception as exc:
            self._esaf_status.setText(f"✗  SSH error: {exc}")
            self._esaf_status.setStyleSheet("color: #d62728;")

    def _collect_top_level(self) -> dict:
        return {
            **self._settings,
            "host":                     self._host.text().strip() or "localhost",
            "ssh_user":                 self._ssh_user.text().strip(),
            "ssh_port":                 self._ssh_port.value(),
            "ssh_key_path":             self._ssh_key.text().strip() or "~/.ssh/id_rsa",
            "ssh_service":              self._ssh_service.text().strip(),
            "conda_env":                self._conda_env.text().strip(),
            "conda_path":               self._conda_path.text().strip() or "~/miniconda3",
            "registry_host":            self._settings.get("registry_host", ""),
            "registry_path":            self._settings.get("registry_path", ""),
            "epics_ca_addr_list":       self._ca_addr_list.text().strip(),
            "epics_ca_auto_addr_list":  self._ca_auto.isChecked(),
            "esaf_server_url":          self._esaf_url.text().strip(),
            "esaf_api_key":             self._esaf_key.text().strip(),
        }

    def _on_accept(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_editor()
        self._settings.update(self._collect_top_level())
        save_connection(self._settings)
        self.accept()

    def get_settings(self) -> dict:
        return self._settings
