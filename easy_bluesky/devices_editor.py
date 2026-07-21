"""devices_editor.py — Edit a profile's devices file (local or remote via SFTP)."""

import threading
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)

from .connection_settings import is_local_host
from .highlighter import PythonHighlighter
from .code_editor import CodeEditor


class _Signals(QObject):
    done = pyqtSignal(bool, str, str)   # success, content, message


class DevicesEditorDialog(QDialog):
    """
    Pull, edit, and push a profile's devices file.

    Local profiles  — reads/writes ~/.easy_bluesky/scripts/<file> directly.
    Remote profiles — pulls/pushes via SFTP; also keeps a local copy so the
                      sim generator can read it offline.
    """

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Devices File")
        self.setMinimumSize(800, 600)
        self._settings = settings
        self._dirty = False
        self._closed = False
        self._current_index = -1
        self._local_path: Path | None = None
        self._is_remote = False

        self._build_ui()
        self._select_active_profile()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Profile / location row
        top = QHBoxLayout()
        top.addWidget(QLabel("Profile:"))
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._combo.blockSignals(True)
        for p in self._settings.get("profiles", []):
            name = p.get("name", "")
            label = f"{name}  [LOCAL]" if p.get("is_local") else name
            self._combo.addItem(label, userData=p)
        self._combo.blockSignals(False)
        top.addWidget(self._combo, 1)

        self._loc_label = QLabel("")
        self._loc_label.setObjectName("dim_text")
        top.addWidget(self._loc_label, 2)
        layout.addLayout(top)

        # Code editor — full-featured (auto-indent, Tab→spaces, autocomplete)
        self._editor = CodeEditor()
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setPointSize(11)
        self._editor.setFont(font)
        self._editor.textChanged.connect(self._on_text_changed)
        self._highlighter = PythonHighlighter(self._editor.document())
        # Extend autocomplete with ophyd device classes and common args
        self._extend_completions()
        layout.addWidget(self._editor, 1)

        # Status bar
        self._status = QLabel("Ready")
        self._status.setWordWrap(True)
        self._status.setMinimumHeight(20)
        layout.addWidget(self._status)

        # Button row
        btn_row = QHBoxLayout()
        self._btn_pull = QPushButton("Reload")
        self._btn_pull.clicked.connect(self._on_pull)
        btn_row.addWidget(self._btn_pull)

        self._btn_save = QPushButton("Save")
        self._btn_save.setDefault(True)
        self._btn_save.clicked.connect(self._on_save_push)
        btn_row.addWidget(self._btn_save)

        btn_row.addStretch()

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _extend_completions(self):
        """Add ophyd device classes and common kwargs to the editor's word list."""
        from PyQt6.QtCore import QStringListModel
        from .code_editor import _ALL_WORDS
        ophyd_words = [
            # imports
            "from ophyd import",
            "from ophyd.sim import",
            # common device classes
            "EpicsMotor", "EpicsSignal", "EpicsSignalRO", "EpicsSignalWithRBV",
            "Device", "Component", "FormattedComponent", "DDC_EpicsSignal",
            "PseudoPositioner", "PseudoSingle", "SoftPositioner",
            "EpicsScaler", "EpicsMotorTuple",
            "SingleTrigger", "AreaDetector", "SimDetector",
            "HDF5Plugin", "TIFFPlugin", "ImagePlugin", "StatsPlugin",
            "ROIPlugin", "TransformPlugin", "OverlayPlugin",
            # ophyd.sim
            "SynAxis", "SynGauss", "SynSignal", "motor", "det",
            # common kwargs
            "name=", "kind=", "labels=", "read_attrs=", "configuration_attrs=",
        ]
        merged = sorted(set(_ALL_WORDS + ophyd_words))
        self._editor._completer.model().setStringList(merged)

    # ── Profile selection ──────────────────────────────────────────────────────

    def _select_active_profile(self):
        active_name = self._settings.get("active_profile", "")
        target = 0
        for i in range(self._combo.count()):
            if self._combo.itemData(i).get("name") == active_name:
                target = i
                break
        # Block the signal so _on_combo_changed doesn't fire; load manually below.
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(target)
        self._combo.blockSignals(False)
        self._current_index = target
        profile = self._combo.itemData(target)
        if profile:
            self._load_profile(profile)

    def _on_combo_changed(self, index: int):
        if self._dirty and self._current_index >= 0:
            r = QMessageBox.question(
                self, "Unsaved Changes",
                "Discard unsaved changes and switch profile?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if r != QMessageBox.StandardButton.Discard:
                self._combo.blockSignals(True)
                self._combo.setCurrentIndex(self._current_index)
                self._combo.blockSignals(False)
                return

        self._current_index = index
        profile = self._combo.itemData(index)
        if profile is not None:
            self._load_profile(profile)

    def _load_profile(self, profile: dict):
        from .worker import _get_scripts_dir

        devices_file = profile.get("devices_file", "devices.py")
        is_local_profile = profile.get("is_local", False) or is_local_host(self._settings)
        self._is_remote = not is_local_profile

        scripts_dir = _get_scripts_dir()
        self._local_path = scripts_dir / devices_file

        if is_local_profile:
            self._btn_pull.setText("Reload")
            self._btn_pull.setToolTip("Re-read file from disk")
            self._btn_save.setText("Save")
            self._btn_save.setToolTip(f"Write to {self._local_path}")
            self._loc_label.setText(f"Local: {self._local_path}")
        else:
            host = self._settings.get("host", "")
            remote = f"~/.easy_bluesky/scripts/{devices_file}"
            self._btn_pull.setText("Pull from RE Machine")
            self._btn_pull.setToolTip(f"Download {remote} from {host}")
            self._btn_save.setText("Save & Push to RE Machine")
            self._btn_save.setToolTip(f"Save local copy and push to {host}:{remote}")
            self._loc_label.setText(f"Remote: {host}:{remote}")

        self._on_pull()

    # ── Pull ───────────────────────────────────────────────────────────────────

    def _make_template(self, profile: dict) -> str:
        name = profile.get("name", "")
        fname = profile.get("devices_file", "devices.py")
        return (
            f'"""\n'
            f'{fname} — Hardware device definitions for {name} profile.\n'
            f'\n'
            f'Add your ophyd/EPICS devices below.\n'
            f'Each device needs a unique Python variable name and a name= keyword.\n'
            f'"""\n'
            f'\n'
            f'from ophyd import EpicsMotor, EpicsSignal, EpicsSignalRO\n'
            f'\n'
            f'\n'
            f'# ── Motors ────────────────────────────────────────────────────────────────────\n'
            f'# sample_x = EpicsMotor("IOC:m1", name="sample_x")\n'
            f'# sample_y = EpicsMotor("IOC:m2", name="sample_y")\n'
            f'# sample_z = EpicsMotor("IOC:m3", name="sample_z")\n'
            f'\n'
            f'\n'
            f'# ── Detectors ─────────────────────────────────────────────────────────────────\n'
            f'# det = EpicsSignal("IOC:det", name="det")\n'
            f'\n'
            f'\n'
            f'# ── Read-only signals ─────────────────────────────────────────────────────────\n'
            f'# ring_current = EpicsSignalRO("RING:current", name="ring_current")\n'
        )

    def _on_pull(self):
        if not self._local_path:
            return
        profile = self._combo.itemData(self._current_index) if self._current_index >= 0 else None
        if not profile:
            return
        devices_file = profile.get("devices_file", "devices.py")

        if not self._is_remote:
            if self._local_path.exists():
                try:
                    content = self._local_path.read_text()
                    if content.strip():
                        self._set_content(content)
                        self._set_status(f"Loaded: {self._local_path}", ok=True)
                    else:
                        self._set_content(self._make_template(profile))
                        self._set_status(
                            f"File was empty — template inserted. Edit and click Save.",
                            ok=True,
                        )
                except Exception as e:
                    self._set_status(f"Read error: {e}", ok=False)
            else:
                self._set_content(self._make_template(profile))
                self._set_status(
                    f"New file — edit and click Save to create {self._local_path.name}",
                    ok=True,
                )
            return

        # Remote pull in background thread
        self._set_status("Pulling from remote…", ok=True)
        self._btn_pull.setEnabled(False)
        sig = _Signals(self)
        sig.done.connect(self._on_pull_done)
        threading.Thread(
            target=_sftp_pull,
            args=(self._settings, devices_file, sig),
            daemon=True,
        ).start()

    def _on_pull_done(self, success: bool, content: str, message: str):
        if self._closed:
            return
        self._btn_pull.setEnabled(True)
        if success:
            if content.strip():
                self._set_content(content)
            else:
                profile = self._combo.itemData(self._current_index)
                self._set_content(self._make_template(profile) if profile else content)
                message = "File was empty — template inserted. Edit and click Save & Push."
            # Cache local copy for sim generator
            try:
                self._local_path.parent.mkdir(parents=True, exist_ok=True)
                self._local_path.write_text(self._editor.toPlainText())
            except Exception:
                pass
        else:
            # File not found on remote — offer template
            profile = self._combo.itemData(self._current_index)
            if profile and "not found" in message.lower():
                self._set_content(self._make_template(profile))
                message = "New file — edit and click Save & Push to create it on the RE machine."
        self._set_status(message, ok=success or "not found" in message.lower())

    # ── Save / Push ────────────────────────────────────────────────────────────

    def _on_save_push(self):
        if not self._local_path:
            return
        profile = self._combo.itemData(self._current_index) if self._current_index >= 0 else None
        if not profile:
            return

        content = self._editor.toPlainText()
        devices_file = profile.get("devices_file", "devices.py")

        # Always save locally first
        try:
            self._local_path.parent.mkdir(parents=True, exist_ok=True)
            self._local_path.write_text(content)
        except Exception as e:
            self._set_status(f"Local save failed: {e}", ok=False)
            return

        self._dirty = False
        self._update_title()

        if not self._is_remote:
            self._set_status(f"Saved: {self._local_path}", ok=True)
            return

        self._set_status("Pushing to remote…", ok=True)
        self._btn_save.setEnabled(False)
        sig = _Signals(self)
        sig.done.connect(self._on_push_done)
        threading.Thread(
            target=_sftp_push,
            args=(self._settings, devices_file, content, sig),
            daemon=True,
        ).start()

    def _on_push_done(self, success: bool, _content: str, message: str):
        if self._closed:
            return
        self._btn_save.setEnabled(True)
        self._set_status(message, ok=success)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_content(self, text: str):
        self._editor.blockSignals(True)
        self._editor.setPlainText(text)
        self._editor.blockSignals(False)
        self._dirty = False
        self._update_title()

    def _on_text_changed(self):
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _update_title(self):
        profile = self._combo.itemData(self._current_index) if self._current_index >= 0 else None
        if not profile:
            self.setWindowTitle("Edit Devices File")
            return
        devices_file = profile.get("devices_file", "devices.py")
        name = profile.get("name", "")
        dirty = " *" if self._dirty else ""
        self.setWindowTitle(f"Edit Devices File — {devices_file}  [{name}]{dirty}")

    def _set_status(self, msg: str, ok: bool = True):
        self._status.setText(msg)
        self._status.setStyleSheet("color: #2ca02c;" if ok else "color: #d62728;")

    def closeEvent(self, event):
        if self._dirty:
            r = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Close anyway?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if r != QMessageBox.StandardButton.Discard:
                event.ignore()
                return
        self._closed = True
        event.accept()


# ── SFTP thread functions (module-level so they work as thread targets) ────────

def _sftp_pull(settings: dict, devices_file: str, signals: _Signals):
    try:
        from .ssh_manager import _get_client
        client = _get_client(settings)
        sftp = client.open_sftp()
        remote_path = f".easy_bluesky/scripts/{devices_file}"
        try:
            with sftp.open(remote_path, "r") as f:
                content = f.read().decode("utf-8", errors="replace")
            host = settings.get("host", "")
            msg = f"Pulled from {host}:~/{remote_path}"
            sftp.close()
            client.close()
            signals.done.emit(True, content, msg)
        except FileNotFoundError:
            sftp.close()
            client.close()
            signals.done.emit(
                False, "",
                f"File not found on remote: ~/.easy_bluesky/scripts/{devices_file}"
            )
    except Exception as e:
        signals.done.emit(False, "", f"SFTP pull failed: {e}")


def _sftp_push(settings: dict, devices_file: str, content: str, signals: _Signals):
    try:
        from .ssh_manager import _get_client
        client = _get_client(settings)
        sftp = client.open_sftp()
        # Ensure remote directories exist
        for part in (".easy_bluesky", ".easy_bluesky/scripts"):
            try:
                sftp.stat(part)
            except FileNotFoundError:
                sftp.mkdir(part)
        remote_path = f".easy_bluesky/scripts/{devices_file}"
        with sftp.open(remote_path, "w") as f:
            f.write(content)
        sftp.close()
        client.close()
        host = settings.get("host", "")
        signals.done.emit(True, "", f"Pushed to {host}:~/{remote_path}")
    except Exception as e:
        signals.done.emit(False, "", f"SFTP push failed: {e}")
