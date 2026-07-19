"""experiments_tab.py — Experiments tab: manage experiments, devices, and plans."""

import json
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QPlainTextEdit, QInputDialog, QFileDialog, QMessageBox,
    QAbstractItemView,
)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QColor

from .config import SUCCESS, DANGER, WARNING, ACCENT, EXPERIMENTS_DIR, ACTIVE_EXPERIMENT_FILE


class ExperimentsTab(QWidget):
    """Three-panel tab: experiment manager | devices | plans."""

    experiment_changed = pyqtSignal(str)   # emits runs_dir path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_exp_path = ""
        self._logged_uids: set = set()
        self._exp_created_at: float = 0.0   # Unix timestamp of experiment creation
        self._plans: dict = {}
        self._build()
        self._load_active_experiment()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_middle())
        splitter.addWidget(self._build_right())
        splitter.setSizes([340, 300, 360])

        lay.addWidget(splitter)

    def _build_left(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        # Active experiment section
        lbl_active = QLabel("ACTIVE EXPERIMENT")
        lbl_active.setObjectName("section_title")
        vlay.addWidget(lbl_active)

        self.exp_name_label = QLabel("—")
        self.exp_name_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #d4d4d4;")
        vlay.addWidget(self.exp_name_label)

        self.exp_path_label = QLabel("")
        self.exp_path_label.setStyleSheet("font-size: 10px; color: #888;")
        self.exp_path_label.setWordWrap(True)
        vlay.addWidget(self.exp_path_label)

        self.exp_date_label = QLabel("")
        self.exp_date_label.setStyleSheet("font-size: 10px; color: #666;")
        vlay.addWidget(self.exp_date_label)

        btn_row = QHBoxLayout()
        btn_new = QPushButton("New Experiment")
        btn_new.setObjectName("btn_primary")
        btn_new.clicked.connect(self.new_experiment)
        btn_open = QPushButton("Open Experiment")
        btn_open.clicked.connect(self.open_experiment)
        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_open)
        vlay.addLayout(btn_row)

        # Recent experiments
        lbl_recent = QLabel("RECENT EXPERIMENTS")
        lbl_recent.setObjectName("section_title")
        vlay.addWidget(lbl_recent)

        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(140)
        self.recent_list.itemClicked.connect(self._on_recent_clicked)
        vlay.addWidget(self.recent_list)

        # Plan log
        lbl_log = QLabel("PLAN LOG")
        lbl_log.setObjectName("section_title")
        vlay.addWidget(lbl_log)

        self.plan_log_list = QListWidget()
        self.plan_log_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        vlay.addWidget(self.plan_log_list, 1)

        return w

    def _build_middle(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        lbl = QLabel("AVAILABLE DEVICES")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

        self.devices_tree = QTreeWidget()
        self.devices_tree.setHeaderLabels(["Name", "Kind", "Module"])
        self.devices_tree.setRootIsDecorated(False)
        self.devices_tree.setAlternatingRowColors(True)
        vlay.addWidget(self.devices_tree, 1)

        return w

    def _build_right(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        lbl = QLabel("AVAILABLE PLANS")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

        self.plans_list = QListWidget()
        self.plans_list.setMaximumHeight(180)
        self.plans_list.currentItemChanged.connect(self._on_plan_selected)
        vlay.addWidget(self.plans_list)

        lbl2 = QLabel("PARAMETERS")
        lbl2.setObjectName("section_title")
        vlay.addWidget(lbl2)

        self.plan_detail = QPlainTextEdit()
        self.plan_detail.setReadOnly(True)
        self.plan_detail.setPlaceholderText("Select a plan to view its parameters...")
        vlay.addWidget(self.plan_detail, 1)

        return w

    # ── Experiment management ──────────────────────────────────────────────────

    def new_experiment(self):
        name, ok = QInputDialog.getText(self, "New Experiment", "Experiment name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        ts = datetime.now()
        sanitized = re.sub(r"[^\w\-]", "_", name)
        folder_name = ts.strftime("%Y%m%d_%H%M%S_") + sanitized
        exp_dir  = Path(EXPERIMENTS_DIR) / folder_name
        runs_dir = exp_dir / "runs"
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not create experiment folder:\n{e}")
            return

        exp_info = {"name": name, "created": ts.isoformat(), "description": ""}
        (exp_dir / "experiment.json").write_text(json.dumps(exp_info, indent=2))

        active_info = {"name": name, "path": str(exp_dir), "created": ts.isoformat()}
        self._write_active_experiment(active_info)
        self._set_active_experiment(str(exp_dir), active_info)
        self.experiment_changed.emit(str(runs_dir))

    def open_experiment(self):
        path = QFileDialog.getExistingDirectory(self, "Open Experiment Folder")
        if not path:
            return
        exp_json = Path(path) / "experiment.json"
        if not exp_json.exists():
            QMessageBox.warning(self, "Invalid Folder",
                                "The selected folder does not contain experiment.json.")
            return
        try:
            info = json.loads(exp_json.read_text())
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read experiment.json:\n{e}")
            return

        active_info = {
            "name":    info.get("name", Path(path).name),
            "path":    path,
            "created": info.get("created", ""),
        }
        self._write_active_experiment(active_info)
        self._set_active_experiment(path, active_info)
        runs_dir = Path(path) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_changed.emit(str(runs_dir))

    def _write_active_experiment(self, info: dict):
        active_file = Path(ACTIVE_EXPERIMENT_FILE)
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(json.dumps(info, indent=2))

    def _set_active_experiment(self, path: str, info: dict):
        self._active_exp_path = path
        self._logged_uids = set()
        created = info.get("created", "")
        try:
            self._exp_created_at = datetime.fromisoformat(created).timestamp()
        except Exception:
            self._exp_created_at = 0.0
        name    = info.get("name", Path(path).name)
        self.exp_name_label.setText(name)
        # Truncate long paths from the left
        display_path = path if len(path) <= 60 else "…" + path[-59:]
        self.exp_path_label.setText(display_path)
        self.exp_date_label.setText(f"Created: {created[:10]}" if created else "")
        self._load_plan_log(path)
        self._load_recent_experiments()

    def _load_active_experiment(self):
        active_file = Path(ACTIVE_EXPERIMENT_FILE)
        if not active_file.exists():
            return
        try:
            info = json.loads(active_file.read_text())
            path = info.get("path", "")
            if path and Path(path).exists():
                self._set_active_experiment(path, info)
        except Exception:
            pass

    def _load_recent_experiments(self):
        exps_dir = Path(EXPERIMENTS_DIR)
        if not exps_dir.exists():
            return
        entries = []
        for d in exps_dir.iterdir():
            if not d.is_dir():
                continue
            exp_json = d / "experiment.json"
            if not exp_json.exists():
                continue
            try:
                info = json.loads(exp_json.read_text())
                entries.append((d.stat().st_mtime, str(d), info))
            except Exception:
                pass
        entries.sort(key=lambda x: x[0], reverse=True)
        self.recent_list.clear()
        for _, path, info in entries[:20]:
            name    = info.get("name", Path(path).name)
            created = info.get("created", "")[:10]
            li = QListWidgetItem(f"{name}  ({created})")
            li.setData(Qt.ItemDataRole.UserRole,     path)
            li.setData(Qt.ItemDataRole.UserRole + 1, info)
            # Highlight active experiment
            if path == self._active_exp_path:
                li.setForeground(QColor(ACCENT))
            self.recent_list.addItem(li)

    @staticmethod
    def _plan_summary(name: str, kwargs: dict) -> str:
        parts = []
        motor = kwargs.get("motor")
        motors = kwargs.get("motors")
        if not motor and isinstance(motors, list) and motors:
            motor = motors[0]
        if motor:
            start = kwargs.get("start")
            stop  = kwargs.get("stop")
            num   = kwargs.get("num")
            s = str(motor)
            if start is not None and stop is not None:
                s += f": {start}→{stop}"
            if num is not None:
                s += f"  {num}pts"
            parts.append(s)
        dets = kwargs.get("detectors") or kwargs.get("detector_list", [])
        if isinstance(dets, str):
            dets = [dets]
        if dets:
            parts.append(", ".join(str(d) for d in dets[:3]))
        if not parts:
            num = kwargs.get("num")
            if num is not None:
                parts.append(f"{num}pts")
        return "  |  " + "  ".join(parts) if parts else ""

    def _load_plan_log(self, exp_path: str):
        log_file = Path(exp_path) / "plans_log.jsonl"
        self.plan_log_list.clear()
        self._logged_uids = set()
        if not log_file.exists():
            return
        try:
            entries = []
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                        uid = entry.get("uid", "")
                        if uid:
                            self._logged_uids.add(uid)
                    except Exception:
                        pass
            for entry in reversed(entries):
                status  = entry.get("exit_status", "")
                ok      = status in ("completed", "success")
                icon    = "✓" if ok else "✗"
                color   = SUCCESS if ok else DANGER
                ts      = entry.get("timestamp", "")
                t_str   = ts[11:19] if len(ts) >= 19 else ts[:19]
                name    = entry.get("name", "?")
                kwargs  = entry.get("kwargs", {}) or {}
                dur     = entry.get("duration_s")
                summary = self._plan_summary(name, kwargs)
                dur_str = f"  ({dur:.1f}s)" if dur is not None else ""
                li      = QListWidgetItem(f"{icon}  {t_str}  {name}{summary}{dur_str}")
                li.setForeground(QColor(color))
                self.plan_log_list.addItem(li)
        except Exception:
            pass

    # ── Public slots ───────────────────────────────────────────────────────────

    def update_history(self, items: list):
        if not self._active_exp_path:
            return
        log_file = Path(self._active_exp_path) / "plans_log.jsonl"
        changed  = False
        for item in items:
            uid = item.get("item_uid", "")
            if not uid or uid in self._logged_uids:
                continue
            result = item.get("result") or {}
            exit_status = result.get("exit_status", "")
            if not exit_status:
                continue  # not yet completed
            t_stop  = result.get("time_stop",  0)
            t_start = result.get("time_start", 0)
            # Skip plans that completed before this experiment was created
            if t_stop and self._exp_created_at and t_stop < self._exp_created_at:
                self._logged_uids.add(uid)  # mark seen; don't re-check
                continue
            timestamp = (
                datetime.fromtimestamp(t_stop).isoformat()
                if t_stop else datetime.now().isoformat()
            )
            dur = (t_stop - t_start) if (t_stop and t_start) else None
            entry = {
                "timestamp":   timestamp,
                "uid":         uid,
                "name":        item.get("name", ""),
                "kwargs":      item.get("kwargs", {}) or {},
                "exit_status": exit_status,
                "duration_s":  round(dur, 2) if dur else None,
            }
            try:
                with open(log_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                self._logged_uids.add(uid)
                changed = True
            except Exception:
                pass
        if changed:
            self._load_plan_log(self._active_exp_path)

    def update_devices(self, devices: dict):
        self.devices_tree.clear()
        for name, info in sorted(devices.items()):
            item = QTreeWidgetItem([
                name,
                info.get("kind", ""),
                info.get("module", ""),
            ])
            self.devices_tree.addTopLevelItem(item)
        self.devices_tree.resizeColumnToContents(0)

    def update_plans(self, plans: dict):
        self._plans = plans
        current = self.plans_list.currentItem()
        current_name = current.text() if current else None

        self.plans_list.clear()
        for name in sorted(plans.keys()):
            self.plans_list.addItem(name)

        # Restore selection
        if current_name:
            for i in range(self.plans_list.count()):
                if self.plans_list.item(i).text() == current_name:
                    self.plans_list.setCurrentRow(i)
                    break

    # ── Internal slots ─────────────────────────────────────────────────────────

    def _on_recent_clicked(self, li: QListWidgetItem):
        path = li.data(Qt.ItemDataRole.UserRole)
        info = li.data(Qt.ItemDataRole.UserRole + 1)
        if not path or not info:
            return
        active_info = {
            "name":    info.get("name", ""),
            "path":    path,
            "created": info.get("created", ""),
        }
        self._write_active_experiment(active_info)
        self._set_active_experiment(path, active_info)
        runs_dir = Path(path) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_changed.emit(str(runs_dir))

    def _on_plan_selected(self, current: QListWidgetItem, previous: QListWidgetItem):
        if not current:
            self.plan_detail.clear()
            return
        name = current.text()
        info = self._plans.get(name, {})
        params = info.get("parameters", [])
        lines  = [f"Plan: {name}", ""]
        if params:
            lines.append("Parameters:")
            for p in params:
                pname = p.get("name", "")
                annotation = p.get("annotation", {})
                default    = p.get("default", "<required>")
                if isinstance(annotation, dict):
                    ptype = annotation.get("type", "")
                else:
                    ptype = str(annotation)
                lines.append(f"  {pname}: {ptype}  (default: {default})")
        else:
            lines.append("No parameters.")
        self.plan_detail.setPlainText("\n".join(lines))
