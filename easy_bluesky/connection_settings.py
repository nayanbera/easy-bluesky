"""connection_settings.py — Persistent connection settings + dialog."""

import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QSpinBox,
    QVBoxLayout,
)

_SETTINGS_FILE = Path.home() / ".easy_bluesky" / "connection.json"

_DEFAULTS = {
    "host":         "localhost",
    "control_port": 60615,
    "info_port":    60625,
    "doc_port":     60630,
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
        "host":         ZMQ_DOC_HOST,
        "control_port": ctrl_port,
        "info_port":    info_port,
        "doc_port":     ZMQ_DOC_PORT,
    }


def save_connection(settings: dict):
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def make_zmq_addrs(settings: dict) -> tuple:
    """Return (control_addr, info_addr, doc_addr) strings."""
    h = settings["host"]
    return (
        f"tcp://{h}:{settings['control_port']}",
        f"tcp://{h}:{settings['info_port']}",
        f"tcp://{h}:{settings['doc_port']}",
    )


class ConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(380)
        self._settings = load_connection()
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)

        note = QLabel("ZMQ addresses for the Bluesky RE Manager.\n"
                      "Changes take effect after clicking OK (reconnects automatically).")
        note.setWordWrap(True)
        note.setObjectName("dim_text")
        lay.addWidget(note)

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

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_accept(self):
        self._settings = {
            "host":         self._host.text().strip() or "localhost",
            "control_port": self._ctrl_port.value(),
            "info_port":    self._info_port.value(),
            "doc_port":     self._doc_port.value(),
        }
        save_connection(self._settings)
        self.accept()

    def get_settings(self) -> dict:
        return self._settings
