"""registry_admin.py — Password-protected admin window for the RE instance registry."""

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout,
    QWidget,
)

from .registry import (
    INSTANCE_DEFAULTS, fetch_registry, hash_password, probe_all_instances,
    save_registry, verify_password,
)


class _FetchWorker(QThread):
    """Background thread: SSH-fetch registry then TCP-probe all instances."""
    done  = pyqtSignal(dict, dict)   # (registry_dict, {name: running_bool})
    error = pyqtSignal(str)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self._settings = settings

    def run(self):
        try:
            reg      = fetch_registry(self._settings)
            running  = probe_all_instances(reg.get("instances", []))
            self.done.emit(reg, running)
        except Exception as e:
            self.error.emit(str(e))


class RegistryAdminWindow(QDialog):
    """
    Password-protected registry admin window.

    Open flow:
      Loading page (SSH fetch) → Password page → Editor page
    The admin password hash lives in registry.json and is verified locally —
    no password is transmitted over SSH.
    """

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Registry Admin")
        self.setMinimumSize(860, 580)
        self._settings     = settings
        self._registry     = {}
        self._running      = {}
        self._current_row  = None

        self._stack = QStackedWidget()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack)

        self._build_loading_page()   # 0
        self._build_password_page()  # 1
        self._build_editor_page()    # 2
        self._build_error_page()     # 3

        self._stack.setCurrentIndex(0)
        self._start_fetch()

    # ── page builders ──────────────────────────────────────────────────────────

    def _build_loading_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch()
        lbl = QLabel("Loading registry…")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("font-size: 14px;")
        v.addWidget(lbl)
        host = (self._settings.get("registry_host", "").strip()
                or self._settings.get("host", ""))
        sub = QLabel(f"SSH → {host}")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setObjectName("dim_text")
        v.addWidget(sub)
        v.addStretch()
        self._stack.addWidget(w)

    def _build_password_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch()

        inner = QWidget()
        inner.setMaximumWidth(380)
        iv = QVBoxLayout(inner)
        iv.setSpacing(12)

        self._pw_title = QLabel("Admin Password")
        self._pw_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        iv.addWidget(self._pw_title)

        self._pw_sub = QLabel("")
        self._pw_sub.setWordWrap(True)
        self._pw_sub.setObjectName("dim_text")
        iv.addWidget(self._pw_sub)

        self._pw_form = QFormLayout()
        self._pw_form.setHorizontalSpacing(12)

        self._pw_entry = QLineEdit()
        self._pw_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_entry.setPlaceholderText("Password")
        self._pw_entry.returnPressed.connect(self._on_password_submitted)
        self._pw_form.addRow("Password:", self._pw_entry)

        self._pw_confirm = QLineEdit()
        self._pw_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_confirm.setPlaceholderText("Confirm password")
        self._pw_confirm.returnPressed.connect(self._on_password_submitted)
        self._pw_form.addRow("Confirm:", self._pw_confirm)
        iv.addLayout(self._pw_form)

        self._pw_error = QLabel("")
        self._pw_error.setStyleSheet("color: #d62728;")
        self._pw_error.setWordWrap(True)
        iv.addWidget(self._pw_error)

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_submit = QPushButton("Continue")
        btn_submit.setDefault(True)
        btn_submit.clicked.connect(self._on_password_submitted)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_submit)
        iv.addLayout(btn_row)

        center = QHBoxLayout()
        center.addStretch()
        center.addWidget(inner)
        center.addStretch()
        v.addLayout(center)
        v.addStretch()
        self._stack.addWidget(w)

    def _build_editor_page(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(6)
        outer.setContentsMargins(8, 8, 8, 8)

        # Header: registry info + Change Password
        header = QHBoxLayout()
        host = (self._settings.get("registry_host", "").strip()
                or self._settings.get("host", ""))
        info = QLabel(f"Registry: {host}  ·  ~/.easy_bluesky/registry.json")
        info.setObjectName("dim_text")
        header.addWidget(info)
        header.addStretch()
        btn_chpw = QPushButton("Change Password…")
        btn_chpw.clicked.connect(self._on_change_password)
        header.addWidget(btn_chpw)
        outer.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep)

        # ── main area ──────────────────────────────────────────────────────────
        main_h = QHBoxLayout()

        # Left: instance list
        left_w = QWidget()
        left_w.setMaximumWidth(200)
        lv = QVBoxLayout(left_w)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)

        list_hdr = QLabel("Instances")
        list_hdr.setStyleSheet("font-weight: bold;")
        lv.addWidget(list_hdr)

        self._inst_list = QListWidget()
        self._inst_list.currentRowChanged.connect(self._on_inst_selected)
        lv.addWidget(self._inst_list)

        btn_r = QHBoxLayout()
        btn_add = QPushButton("＋ Add")
        btn_rm  = QPushButton("✕ Remove")
        btn_add.clicked.connect(self._on_add_instance)
        btn_rm.clicked.connect(self._on_remove_instance)
        btn_r.addWidget(btn_add)
        btn_r.addWidget(btn_rm)
        lv.addLayout(btn_r)
        main_h.addWidget(left_w)

        # Right: editor form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_w = QWidget()
        rv = QVBoxLayout(right_w)
        rv.setSpacing(4)
        scroll.setWidget(right_w)

        self._inst_form = QFormLayout()
        self._inst_form.setHorizontalSpacing(12)
        self._inst_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._inst_name = QLineEdit()
        self._inst_name.setPlaceholderText("ASWAXS")
        self._inst_form.addRow("Name:", self._inst_name)

        self._inst_host = QLineEdit()
        self._inst_host.setPlaceholderText("10.0.0.10  or  beamline-hostname")
        self._inst_host.textChanged.connect(self._update_zmq_label)
        self._inst_form.addRow("Host / IP:", self._inst_host)

        self._inst_desc = QLineEdit()
        self._inst_desc.setPlaceholderText("Short description")
        self._inst_form.addRow("Description:", self._inst_desc)

        self._inst_ctrl = QSpinBox(); self._inst_ctrl.setRange(1, 65535)
        self._inst_info = QSpinBox(); self._inst_info.setRange(1, 65535)
        self._inst_doc  = QSpinBox(); self._inst_doc.setRange(1, 65535)
        self._inst_ps   = QSpinBox(); self._inst_ps.setRange(1, 65535)
        self._inst_form.addRow("Control port:",    self._inst_ctrl)
        self._inst_form.addRow("Info port:",       self._inst_info)
        self._inst_form.addRow("Doc stream port:", self._inst_doc)
        self._inst_form.addRow("procServ port:",   self._inst_ps)
        for _sb in (self._inst_ctrl, self._inst_info):
            _sb.valueChanged.connect(self._update_zmq_label)

        self._inst_devices    = QLineEdit(); self._inst_devices.setPlaceholderText("devices_ASWAXS.py")
        self._inst_conda_env  = QLineEdit(); self._inst_conda_env.setPlaceholderText("bluesky")
        self._inst_conda_path = QLineEdit(); self._inst_conda_path.setPlaceholderText("~/miniconda3")
        self._inst_form.addRow("Devices file:", self._inst_devices)
        self._inst_form.addRow("Conda env:",    self._inst_conda_env)
        self._inst_form.addRow("Conda path:",   self._inst_conda_path)

        rv.addLayout(self._inst_form)

        btn_ports = QPushButton("Auto-assign Ports (check remote)")
        btn_ports.setToolTip("SSH to this instance's host and find ports not in use there")
        btn_ports.clicked.connect(self._on_auto_assign)
        rv.addWidget(btn_ports, alignment=Qt.AlignmentFlag.AlignLeft)

        self._ports_note = QLabel("")
        self._ports_note.setObjectName("dim_text")
        rv.addWidget(self._ports_note)

        self._zmq_label = QLabel("")
        self._zmq_label.setObjectName("dim_text")
        rv.addWidget(self._zmq_label)

        rv.addStretch()
        main_h.addWidget(scroll, 1)
        outer.addLayout(main_h, 1)

        # ── bottom buttons ─────────────────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        bot = QHBoxLayout()
        self._save_status = QLabel("")
        bot.addWidget(self._save_status)
        bot.addStretch()
        btn_save = QPushButton("Save to Registry")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self._on_save)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        bot.addWidget(btn_save)
        bot.addWidget(btn_close)
        outer.addLayout(bot)

        self._stack.addWidget(w)

    def _build_error_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch()
        self._err_label = QLabel("")
        self._err_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._err_label.setWordWrap(True)
        self._err_label.setStyleSheet("color: #d62728; font-size: 13px;")
        v.addWidget(self._err_label)
        btn = QPushButton("Close")
        btn.clicked.connect(self.reject)
        v.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
        v.addStretch()
        self._stack.addWidget(w)

    # ── fetch flow ─────────────────────────────────────────────────────────────

    def _start_fetch(self):
        self._fetch_worker = _FetchWorker(self._settings, parent=self)
        self._fetch_worker.done.connect(self._on_fetch_done)
        self._fetch_worker.error.connect(self._on_fetch_error)
        self._fetch_worker.start()

    def _on_fetch_done(self, registry: dict, running: dict):
        self._registry = registry
        self._running  = running
        has_hash = bool(registry.get("admin_password_hash", ""))
        if has_hash:
            self._pw_title.setText("Admin Password")
            self._pw_sub.setText(
                "Enter the admin password to manage the registry."
            )
            self._pw_form.setRowVisible(self._pw_confirm, False)
        else:
            self._pw_title.setText("Create Admin Password")
            self._pw_sub.setText(
                "No admin password is set yet.\n"
                "Create one to protect registry editing."
            )
            self._pw_form.setRowVisible(self._pw_confirm, True)
        self._pw_entry.clear()
        self._pw_confirm.clear()
        self._pw_error.setText("")
        self._stack.setCurrentIndex(1)
        self._pw_entry.setFocus()

    def _on_fetch_error(self, msg: str):
        self._err_label.setText(
            f"Could not load registry:\n\n{msg}\n\n"
            "Check Connection Settings — registry host and SSH credentials."
        )
        self._stack.setCurrentIndex(3)

    # ── password flow ──────────────────────────────────────────────────────────

    def _on_password_submitted(self):
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
        self._populate_inst_list()
        self._stack.setCurrentIndex(2)

    def _on_change_password(self):
        pw, ok = QInputDialog.getText(
            self, "New Admin Password", "New password:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not pw:
            return
        cf, ok2 = QInputDialog.getText(
            self, "Confirm Password", "Confirm new password:",
            QLineEdit.EchoMode.Password,
        )
        if not ok2:
            return
        if pw != cf:
            QMessageBox.warning(self, "Mismatch", "Passwords do not match.")
            return
        self._registry["admin_password_hash"] = hash_password(pw)
        self._save_status.setText("Password updated — click Save to apply.")
        self._save_status.setStyleSheet("color: #ff7f0e;")

    # ── instance list ──────────────────────────────────────────────────────────

    def _populate_inst_list(self):
        self._inst_list.blockSignals(True)
        self._inst_list.clear()
        for inst in self._registry.get("instances", []):
            name    = inst.get("name", "")
            running = self._running.get(name, False)
            item = QListWidgetItem(f"{'●' if running else '○'}  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setForeground(QColor("#2ca02c" if running else "#888888"))
            self._inst_list.addItem(item)
        self._inst_list.blockSignals(False)
        if self._inst_list.count() > 0:
            self._inst_list.setCurrentRow(0)

    def _on_inst_selected(self, row: int):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_inst()
        self._current_row = row
        if row < 0:
            return
        instances = self._registry.get("instances", [])
        if row >= len(instances):
            return
        inst = instances[row]
        self._inst_name.setText(inst.get("name", ""))
        self._inst_host.setText(inst.get("host", ""))
        self._inst_desc.setText(inst.get("description", ""))
        self._inst_ctrl.setValue(inst.get("control_port",  INSTANCE_DEFAULTS["control_port"]))
        self._inst_info.setValue(inst.get("info_port",     INSTANCE_DEFAULTS["info_port"]))
        self._inst_doc.setValue(inst.get("doc_port",       INSTANCE_DEFAULTS["doc_port"]))
        self._inst_ps.setValue(inst.get("procserv_port",   INSTANCE_DEFAULTS["procserv_port"]))
        self._inst_devices.setText(inst.get("devices_file",  INSTANCE_DEFAULTS["devices_file"]))
        self._inst_conda_env.setText(inst.get("conda_env",   INSTANCE_DEFAULTS["conda_env"]))
        self._inst_conda_path.setText(inst.get("conda_path", INSTANCE_DEFAULTS["conda_path"]))
        self._ports_note.setText("")
        self._update_zmq_label()

    def _save_current_inst(self):
        row = self._current_row
        if row is None or row < 0:
            return
        instances = self._registry.get("instances", [])
        if row >= len(instances):
            return
        new_name = self._inst_name.text().strip() or f"Instance {row + 1}"
        instances[row] = {
            "name":         new_name,
            "host":         self._inst_host.text().strip(),
            "description":  self._inst_desc.text().strip(),
            "control_port": self._inst_ctrl.value(),
            "info_port":    self._inst_info.value(),
            "doc_port":     self._inst_doc.value(),
            "procserv_port":self._inst_ps.value(),
            "devices_file": self._inst_devices.text().strip() or "devices.py",
            "conda_env":    self._inst_conda_env.text().strip(),
            "conda_path":   self._inst_conda_path.text().strip() or "~/miniconda3",
        }
        item = self._inst_list.item(row)
        if item:
            running = self._running.get(new_name, False)
            item.setText(f"{'●' if running else '○'}  {new_name}")
            item.setData(Qt.ItemDataRole.UserRole, new_name)

    def _on_add_instance(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_inst()
        instances = self._registry.setdefault("instances", [])
        new_inst  = dict(INSTANCE_DEFAULTS)
        new_inst["name"] = f"Instance {len(instances) + 1}"
        instances.append(new_inst)
        item = QListWidgetItem(f"○  {new_inst['name']}")
        item.setData(Qt.ItemDataRole.UserRole, new_inst["name"])
        self._inst_list.blockSignals(True)
        self._inst_list.addItem(item)
        self._inst_list.blockSignals(False)
        self._current_row = None
        self._inst_list.setCurrentRow(len(instances) - 1)

    def _on_remove_instance(self):
        row = self._inst_list.currentRow()
        if row < 0:
            return
        instances = self._registry.get("instances", [])
        if row >= len(instances):
            return
        name = instances[row].get("name", "")
        reply = QMessageBox.question(
            self, "Remove Instance",
            f"Remove '{name}' from the registry?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        instances.pop(row)
        self._current_row = None
        self._inst_list.blockSignals(True)
        self._inst_list.takeItem(row)
        self._inst_list.blockSignals(False)
        new_row = min(row, self._inst_list.count() - 1)
        if new_row >= 0:
            self._inst_list.setCurrentRow(new_row)

    def _update_zmq_label(self):
        host = self._inst_host.text().strip() or "?"
        ctrl = self._inst_ctrl.value()
        info = self._inst_info.value()
        self._zmq_label.setText(
            f"ZMQ: tcp://{host}:{ctrl}  (control)  ·  tcp://{host}:{info}  (info)"
        )

    def _on_auto_assign(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_inst()
        instances = self._registry.get("instances", [])
        row  = self._inst_list.currentRow()
        host = self._inst_host.text().strip()
        if not host:
            self._ports_note.setText("Enter the instance host first.")
            return

        used = set()
        for i, inst in enumerate(instances):
            if i != row:
                for k in ("control_port", "info_port", "doc_port", "procserv_port"):
                    v = inst.get(k)
                    if isinstance(v, int):
                        used.add(v)

        self._ports_note.setText(f"Checking {host} via SSH…")
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()

        from .connection_settings import find_free_ports_remote
        ports, note = find_free_ports_remote(
            {**self._settings, "host": host},
            count=4, start=60615, used=used,
        )
        if len(ports) >= 4:
            self._inst_ctrl.setValue(ports[0])
            self._inst_info.setValue(ports[1])
            self._inst_doc.setValue(ports[2])
            self._inst_ps.setValue(ports[3])
            self._ports_note.setText(
                f"Assigned: ctrl={ports[0]}  info={ports[1]}"
                f"  doc={ports[2]}  procServ={ports[3]}  ({note})"
            )
        else:
            self._ports_note.setText(f"Could not find free ports.  ({note})")

    # ── save ───────────────────────────────────────────────────────────────────

    def _on_save(self):
        if self._current_row is not None and self._current_row >= 0:
            self._save_current_inst()
        self._save_status.setText("Saving…")
        self._save_status.setStyleSheet("color: #888;")
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        try:
            save_registry(self._settings, self._registry)
            self._save_status.setText("✓  Saved to registry")
            self._save_status.setStyleSheet("color: #2ca02c;")
        except Exception as e:
            self._save_status.setText(f"✗  {e}")
            self._save_status.setStyleSheet("color: #d62728;")
