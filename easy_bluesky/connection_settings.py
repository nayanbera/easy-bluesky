"""connection_settings.py — Persistent connection settings + dialog."""

import json
import re
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

_SETTINGS_FILE = Path.home() / ".easy_bluesky" / "connection.json"

_PROFILE_DEFAULTS = {
    "name": "Default",
    "devices_file": "devices.py",
    "is_local": False,
    "control_port": 60615,
    "info_port": 60625,
    "doc_port": 60630,
    "procserv_port": 60635,
}

_DEFAULTS = {
    "host": "localhost",
    "ssh_user": "",
    "ssh_port": 22,
    "ssh_key_path": "~/.ssh/id_rsa",
    "ssh_service": "",
    "conda_env": "",
    "conda_path": "~/miniconda3",
    "active_profile": "Default",
    "profiles": [_PROFILE_DEFAULTS.copy()],
    "deleted_profiles": [],
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
    """Return (control_addr, info_addr, doc_addr) for the active profile."""
    profile = get_active_profile(settings)
    # Local profiles always connect to localhost regardless of global host setting
    if profile.get("is_local", False):
        h = "localhost"
    else:
        h = settings.get("host", "localhost") or "localhost"
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
    result = []
    port = start
    while len(result) < count and port <= 65535:
        if port not in used:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("", port))
                    result.append(port)
            except OSError:
                pass
        port += 1
    return result


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

        self._ssh_port = QSpinBox()
        self._ssh_port.setRange(1, 65535)
        self._ssh_port.setValue(self._settings.get("ssh_port", 22))
        ssh_form.addRow("SSH port:", self._ssh_port)

        key_row = QHBoxLayout()
        self._ssh_key = QLineEdit(self._settings.get("ssh_key_path", "~/.ssh/id_rsa"))
        self._ssh_key.setPlaceholderText("~/.ssh/id_rsa  or  ~/.ssh/id_ed25519")
        btn_browse = QPushButton("Browse…")
        btn_browse.setMaximumWidth(70)
        btn_browse.clicked.connect(self._browse_key)
        key_row.addWidget(self._ssh_key)
        key_row.addWidget(btn_browse)
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

        self._prof_ctrl = QSpinBox()
        self._prof_ctrl.setRange(1, 65535)
        self._prof_form.addRow("Control port:", self._prof_ctrl)

        self._prof_info = QSpinBox()
        self._prof_info.setRange(1, 65535)
        self._prof_form.addRow("Info port:", self._prof_info)

        self._prof_doc = QSpinBox()
        self._prof_doc.setRange(1, 65535)
        self._prof_form.addRow("Doc stream port:", self._prof_doc)

        self._prof_procserv = QSpinBox()
        self._prof_procserv.setRange(1, 65535)
        self._prof_form.addRow("procServ port:", self._prof_procserv)

        right_lay.addLayout(self._prof_form)

        self._prof_local_note = QLabel(
            "RE Manager starts and stops automatically with the app. No SSH needed."
        )
        self._prof_local_note.setObjectName("dim_text")
        self._prof_local_note.setWordWrap(True)
        self._prof_local_note.setVisible(False)
        right_lay.addWidget(self._prof_local_note)

        btn_auto = QPushButton("Auto-assign Ports")
        btn_auto.setToolTip("Find 4 free ports and assign them to this profile")
        btn_auto.clicked.connect(self._on_auto_assign_ports)
        right_lay.addWidget(btn_auto, alignment=Qt.AlignmentFlag.AlignLeft)
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
            if self._profile_list.item(i).text().split("  ")[0] == active:
                self._profile_list.setCurrentRow(i)
                selected = True
                break
        if not selected and self._profile_list.count() > 0:
            self._profile_list.setCurrentRow(0)

    def _on_is_local_toggled(self, checked: bool):
        self._prof_local_note.setVisible(checked)
        self._prof_form.setRowVisible(self._prof_procserv, not checked)

    def _populate_profile_list(self):
        active = self._settings.get("active_profile", "Default")
        self._profile_list.blockSignals(True)
        self._profile_list.clear()
        for p in self._settings.get("profiles", []):
            name = p.get("name", "")
            label = f"{name}  [LOCAL]" if p.get("is_local") else name
            item = QListWidgetItem(label)
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
        self._prof_is_local.setChecked(p.get("is_local", False))
        self._prof_devices.setText(p.get("devices_file", "devices.py"))
        self._prof_ctrl.setValue(p.get("control_port", _PROFILE_DEFAULTS["control_port"]))
        self._prof_info.setValue(p.get("info_port", _PROFILE_DEFAULTS["info_port"]))
        self._prof_doc.setValue(p.get("doc_port", _PROFILE_DEFAULTS["doc_port"]))
        self._prof_procserv.setValue(p.get("procserv_port", _PROFILE_DEFAULTS["procserv_port"]))
        # Show/hide procServ row based on is_local
        self._on_is_local_toggled(p.get("is_local", False))

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
            "name": new_name,
            "devices_file": self._prof_devices.text().strip() or "devices.py",
            "is_local": is_local,
            "control_port": self._prof_ctrl.value(),
            "info_port": self._prof_info.value(),
            "doc_port": self._prof_doc.value(),
            "procserv_port": self._prof_procserv.value(),
        }

        if old_name == self._settings.get("active_profile") and new_name != old_name:
            self._settings["active_profile"] = new_name

        item = self._profile_list.item(row)
        if item:
            label = f"{new_name}  [LOCAL]" if is_local else new_name
            item.setText(label)
            current_active = self._settings.get("active_profile", "Default")
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
        self._profile_list.addItem(label)
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
        ports = find_free_ports(count=4, start=start, used=used)

        if len(ports) >= 4:
            self._prof_ctrl.setValue(ports[0])
            self._prof_info.setValue(ports[1])
            self._prof_doc.setValue(ports[2])
            self._prof_procserv.setValue(ports[3])

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

    def _collect_top_level(self) -> dict:
        return {
            **self._settings,
            "host":         self._host.text().strip() or "localhost",
            "ssh_user":     self._ssh_user.text().strip(),
            "ssh_port":     self._ssh_port.value(),
            "ssh_key_path": self._ssh_key.text().strip() or "~/.ssh/id_rsa",
            "ssh_service":  self._ssh_service.text().strip(),
            "conda_env":    self._conda_env.text().strip(),
            "conda_path":   self._conda_path.text().strip() or "~/miniconda3",
        }

    def _on_accept(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_editor()
        self._settings.update(self._collect_top_level())
        save_connection(self._settings)
        self.accept()

    def get_settings(self) -> dict:
        return self._settings
