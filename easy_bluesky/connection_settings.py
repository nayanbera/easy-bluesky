"""connection_settings.py — Persistent connection settings + dialog."""

import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
)

_SETTINGS_FILE = Path.home() / ".easy_bluesky" / "connection.json"

_DEFAULTS = {
    "host":         "localhost",
    "control_port": 60615,
    "info_port":    60625,
    "doc_port":     60630,
    # Sim-mode ports (separate RE Manager instance with sim startup script)
    "sim_control_port": 60616,
    "sim_info_port":    60626,
    "sim_doc_port":     60631,
    # SSH (used for remote RE Manager restart; never committed to git)
    "ssh_user":     "",
    "ssh_port":     22,
    "ssh_key_path": "~/.ssh/id_rsa",
    "ssh_service":  "",   # systemd service name, or "" for direct pkill+nohup
    "conda_env":    "",   # conda env name on the remote host, e.g. "bluesky"
    "conda_path":   "~/miniconda3",  # base conda install dir on the remote host
}


def load_connection() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            data = json.loads(_SETTINGS_FILE.read_text())
            return {**_DEFAULTS, **data}
        except Exception:
            pass
    # Fall back to values derived from env vars in config
    from .config import ZMQ_CONTROL, ZMQ_INFO, ZMQ_DOC_HOST, ZMQ_DOC_PORT
    try:
        ctrl_port = int(ZMQ_CONTROL.rsplit(":", 1)[-1])
        info_port = int(ZMQ_INFO.rsplit(":", 1)[-1])
    except Exception:
        ctrl_port = _DEFAULTS["control_port"]
        info_port = _DEFAULTS["info_port"]
    return {
        **_DEFAULTS,
        "host":         ZMQ_DOC_HOST,
        "control_port": ctrl_port,
        "info_port":    info_port,
        "doc_port":     ZMQ_DOC_PORT,
    }


def save_connection(settings: dict):
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def make_zmq_addrs(settings: dict) -> tuple:
    """Return (control_addr, info_addr, doc_addr) for the real-mode instance."""
    h = settings["host"]
    return (
        f"tcp://{h}:{settings['control_port']}",
        f"tcp://{h}:{settings['info_port']}",
        f"tcp://{h}:{settings['doc_port']}",
    )


def make_zmq_addrs_for_mode(settings: dict, sim: bool) -> tuple:
    """Return (control_addr, info_addr, doc_addr) for real or sim mode."""
    h = settings["host"]
    if sim:
        return (
            f"tcp://{h}:{settings['sim_control_port']}",
            f"tcp://{h}:{settings['sim_info_port']}",
            f"tcp://{h}:{settings['sim_doc_port']}",
        )
    return make_zmq_addrs(settings)


def is_local_host(settings: dict) -> bool:
    host = settings.get("host", "localhost").strip().lower()
    return host in ("localhost", "127.0.0.1", "::1", "")


class ConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(420)
        self._settings = load_connection()
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)

        note = QLabel(
            "ZMQ addresses for the Bluesky RE Manager.\n"
            "Changes take effect after clicking OK (reconnects automatically)."
        )
        note.setWordWrap(True)
        note.setObjectName("dim_text")
        lay.addWidget(note)

        # ── ZMQ section ────────────────────────────────────────────────────────
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)

        self._host = QLineEdit(self._settings["host"])
        self._host.setPlaceholderText("localhost or 192.168.1.50")
        form.addRow("Host / IP:", self._host)

        self._ctrl_port = QSpinBox()
        self._ctrl_port.setRange(1, 65535)
        self._ctrl_port.setValue(self._settings["control_port"])
        form.addRow("Control port:", self._ctrl_port)

        self._info_port = QSpinBox()
        self._info_port.setRange(1, 65535)
        self._info_port.setValue(self._settings["info_port"])
        form.addRow("Info port:", self._info_port)

        self._doc_port = QSpinBox()
        self._doc_port.setRange(1, 65535)
        self._doc_port.setValue(self._settings["doc_port"])
        form.addRow("Doc stream port:", self._doc_port)

        lay.addLayout(form)

        # ── Sim ports section ──────────────────────────────────────────────────
        sep_sim = QFrame()
        sep_sim.setFrameShape(QFrame.Shape.HLine)
        sep_sim.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep_sim)

        sim_title = QLabel("Sim Mode Ports")
        sim_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(sim_title)

        sim_note = QLabel(
            "Ports for a second RE Manager instance running the simulated startup script.\n"
            "The sim mode toggle reconnects to these ports automatically."
        )
        sim_note.setWordWrap(True)
        sim_note.setObjectName("dim_text")
        lay.addWidget(sim_note)

        sim_form = QFormLayout()
        sim_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sim_form.setHorizontalSpacing(12)

        self._sim_ctrl_port = QSpinBox()
        self._sim_ctrl_port.setRange(1, 65535)
        self._sim_ctrl_port.setValue(self._settings.get("sim_control_port", 60616))
        sim_form.addRow("Sim control port:", self._sim_ctrl_port)

        self._sim_info_port = QSpinBox()
        self._sim_info_port.setRange(1, 65535)
        self._sim_info_port.setValue(self._settings.get("sim_info_port", 60626))
        sim_form.addRow("Sim info port:", self._sim_info_port)

        self._sim_doc_port = QSpinBox()
        self._sim_doc_port.setRange(1, 65535)
        self._sim_doc_port.setValue(self._settings.get("sim_doc_port", 60631))
        sim_form.addRow("Sim doc stream port:", self._sim_doc_port)

        lay.addLayout(sim_form)

        # ── SSH section separator ──────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep)

        ssh_title = QLabel("Remote SSH Management")
        ssh_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lay.addWidget(ssh_title)

        ssh_note = QLabel(
            "Used only when Host is a remote machine.\n"
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

        # Key path row with Browse button
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

        # Test SSH button
        btn_test = QPushButton("Test SSH Connection")
        btn_test.clicked.connect(self._test_ssh)
        lay.addWidget(btn_test, alignment=Qt.AlignmentFlag.AlignLeft)

        self._ssh_result = QLabel("")
        self._ssh_result.setWordWrap(True)
        lay.addWidget(self._ssh_result)

        # ── Dialog buttons ─────────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _browse_key(self):
        start = str(Path(self._ssh_key.text()).expanduser().parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH Private Key", start, "All Files (*)"
        )
        if path:
            self._ssh_key.setText(path)

    def _test_ssh(self):
        from .ssh_manager import test_ssh_connection
        settings = self._current_fields()
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

    def _current_fields(self) -> dict:
        return {
            "host":             self._host.text().strip() or "localhost",
            "control_port":     self._ctrl_port.value(),
            "info_port":        self._info_port.value(),
            "doc_port":         self._doc_port.value(),
            "sim_control_port": self._sim_ctrl_port.value(),
            "sim_info_port":    self._sim_info_port.value(),
            "sim_doc_port":     self._sim_doc_port.value(),
            "ssh_user":         self._ssh_user.text().strip(),
            "ssh_port":         self._ssh_port.value(),
            "ssh_key_path":     self._ssh_key.text().strip() or "~/.ssh/id_rsa",
            "ssh_service":      self._ssh_service.text().strip(),
            "conda_env":        self._conda_env.text().strip(),
            "conda_path":       self._conda_path.text().strip() or "~/miniconda3",
        }

    def _on_accept(self):
        self._settings = self._current_fields()
        save_connection(self._settings)
        self.accept()

    def get_settings(self) -> dict:
        return self._settings
