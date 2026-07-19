"""experiments_tab.py — Experiments tab: experiment manager, queue, live/history plots."""

import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import pyqtgraph as pg
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QInputDialog, QFileDialog, QMessageBox,
    QAbstractItemView, QTabWidget, QComboBox, QPlainTextEdit, QDialog,
    QMainWindow,
)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont

from .config import (
    SUCCESS, DANGER, ACCENT,
    EXPERIMENTS_DIR, ACTIVE_EXPERIMENT_FILE, PLOT_COLORS, DATA_RUNS_DIR,
)
from .live_viewer import LiveViewer
from .widgets import PlanDialog
from .queue_manager import RunDetailDialog


# ── Embedded single-run history plot ──────────────────────────────────────────

class ExperimentHistoryWidget(QWidget):
    """Plots one run's data loaded directly from a JSONL file.
    No run list — the plan log in ExperimentsTab serves as the selector.
    """

    COLORS = PLOT_COLORS

    def __init__(self, parent=None):
        super().__init__(parent)
        self._df = None
        self._build()

    def _build(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)

        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(130)
        axis_row.addWidget(self.x_combo)

        axis_row.addWidget(QLabel("Y:"))
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMaximumHeight(56)
        self.y_list.setMaximumWidth(220)
        axis_row.addWidget(self.y_list)

        btn_plot = QPushButton("Plot")
        btn_plot.setObjectName("btn_primary")
        btn_plot.clicked.connect(self._plot)
        axis_row.addWidget(btn_plot)

        self.run_label = QLabel("← Click a plan in the log to view its data")
        self.run_label.setObjectName("dim_text")
        self.run_label.setStyleSheet("font-size: 12px; padding: 0 8px;")
        axis_row.addWidget(self.run_label)
        axis_row.addStretch()
        main.addLayout(axis_row)

        if PG_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            main.addWidget(self.plot_widget, 1)
        else:
            main.addWidget(
                QLabel("pyqtgraph not available — pip install pyqtgraph"), 1)

        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim_text")
        main.addWidget(self.stats_label)

    def load_jsonl_file(self, filepath: str):
        """Parse a suitcase-jsonl file and auto-plot it."""
        events: list = []
        start_doc: dict = {}
        try:
            with open(filepath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    name, doc = json.loads(line)
                    if name == "start":
                        start_doc = doc
                    elif name == "event":
                        row = {"seq_num": doc.get("seq_num"),
                               "time":    doc.get("time")}
                        row.update(doc.get("data", {}))
                        events.append(row)
                    elif name == "event_page":
                        times    = doc.get("time", [])
                        seqs     = doc.get("seq_num", [])
                        data_col = doc.get("data", {})
                        for i in range(len(times)):
                            row = {"seq_num": seqs[i] if i < len(seqs) else None,
                                   "time":    times[i]}
                            row.update({k: v[i] for k, v in data_col.items()
                                        if i < len(v)})
                            events.append(row)
        except Exception as e:
            self.run_label.setText(f"Load error: {e}")
            return

        if not events:
            self.run_label.setText("No events found in this run file")
            return

        try:
            import pandas as pd
            self._df = pd.DataFrame(events)
        except ImportError:
            self.run_label.setText("pandas not available — pip install pandas")
            return

        plan = start_doc.get("plan_name", "?")
        uid8 = start_doc.get("uid", filepath)[:8]
        self.run_label.setText(
            f"{plan}  [{uid8}]  —  {len(events)} events")

        cols = [c for c in self._df.columns
                if self._df[c].dtype.kind in ("f", "i", "u")]
        self.x_combo.clear()
        self.x_combo.addItems(cols)
        self.y_list.clear()
        for c in cols:
            self.y_list.addItem(QListWidgetItem(c))

        motor_cols = [c for c in cols
                      if any(w in c.lower()
                             for w in ("motor", "pos", "stage", "enc"))]
        det_cols   = [c for c in cols
                      if c not in motor_cols and c not in ("seq_num", "time")]
        x_default  = motor_cols[0] if motor_cols else (cols[0] if cols else "")
        if x_default:
            self.x_combo.setCurrentText(x_default)
        for i in range(self.y_list.count()):
            sig = self.y_list.item(i).text()
            self.y_list.item(i).setSelected(
                sig in det_cols or (not det_cols and sig != x_default))
        self._plot()

    def _plot(self):
        if self._df is None or not PG_AVAILABLE:
            return
        self.plot_widget.clear()
        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs or xc not in self._df.columns:
            return

        x = self._df[xc].values.astype(float)
        stats = []
        for idx, yc in enumerate(ycs):
            if yc not in self._df.columns:
                continue
            y    = self._df[yc].values.astype(float)
            mask = np.isfinite(x) & np.isfinite(y)
            x_, y_ = x[mask], y[mask]
            if not len(x_):
                continue
            color = self.COLORS[idx % len(self.COLORS)]
            pen   = pg.mkPen(color=color, width=2)
            self.plot_widget.plot(x_, y_, pen=pen, name=yc,
                                  symbol="o", symbolSize=5,
                                  symbolBrush=color, symbolPen=None)
            stats.append(
                f"{yc}: min={y_.min():.4g}  max={y_.max():.4g}"
                f"  mean={y_.mean():.4g}")
        self.plot_widget.setLabel("bottom", xc)
        self.plot_widget.setLabel("left",   ", ".join(ycs))
        self.stats_label.setText("   ".join(stats))


# ── Main experiments tab ───────────────────────────────────────────────────────

class ExperimentsTab(QWidget):
    """Three-panel layout:
      Left  — experiment info, recent list, plan log
      Middle — compact queue with add/remove/clear + console output
      Right  — Live / History plot tabs (detachable)
    """

    experiment_changed = pyqtSignal(str)   # emits runs_dir path

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker            = worker
        self._plans: dict      = {}
        self._devices: dict    = {}
        self._active_exp_path  = ""
        self._logged_uids: set = set()
        self._exp_created_at: float = 0.0
        self._detached_win     = None
        self._plot_placeholder = None
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
        splitter.setSizes([220, 260, 720])
        lay.addWidget(splitter)

    # ── Left panel: experiment info + recent + plan log ────────────────────────

    def _build_left(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 4, 8)
        vlay.setSpacing(6)

        lbl_active = QLabel("ACTIVE EXPERIMENT")
        lbl_active.setObjectName("section_title")
        vlay.addWidget(lbl_active)

        self.exp_name_label = QLabel("—")
        self.exp_name_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        vlay.addWidget(self.exp_name_label)

        self.exp_path_label = QLabel("")
        self.exp_path_label.setObjectName("dim_text")
        self.exp_path_label.setStyleSheet("font-size: 10px;")
        self.exp_path_label.setWordWrap(True)
        vlay.addWidget(self.exp_path_label)

        self.exp_date_label = QLabel("")
        self.exp_date_label.setObjectName("dim_text")
        self.exp_date_label.setStyleSheet("font-size: 10px;")
        vlay.addWidget(self.exp_date_label)

        btn_row = QHBoxLayout()
        btn_new  = QPushButton("New Experiment")
        btn_new.setObjectName("btn_primary")
        btn_new.clicked.connect(self.new_experiment)
        btn_open = QPushButton("Open Experiment")
        btn_open.clicked.connect(self.open_experiment)
        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_open)
        vlay.addLayout(btn_row)

        lbl_recent = QLabel("RECENT EXPERIMENTS")
        lbl_recent.setObjectName("section_title")
        vlay.addWidget(lbl_recent)

        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(130)
        self.recent_list.itemClicked.connect(self._on_recent_clicked)
        vlay.addWidget(self.recent_list)

        lbl_log = QLabel("PLAN LOG  (click to plot in History)")
        lbl_log.setObjectName("section_title")
        vlay.addWidget(lbl_log)

        self.plan_log_list = QListWidget()
        self.plan_log_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.plan_log_list.itemClicked.connect(self._on_plan_log_clicked)
        self.plan_log_list.itemDoubleClicked.connect(self._on_plan_log_double_clicked)
        vlay.addWidget(self.plan_log_list, 1)

        return w

    # ── Middle panel: queue + buttons + console ────────────────────────────────

    def _build_middle(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 8, 4, 8)
        vlay.setSpacing(4)

        # Queue header
        q_hdr = QHBoxLayout()
        lbl_q = QLabel("QUEUE")
        lbl_q.setObjectName("section_title")
        q_hdr.addWidget(lbl_q)
        q_hdr.addStretch()
        self.queue_count_label = QLabel("0 items")
        self.queue_count_label.setObjectName("dim_text")
        q_hdr.addWidget(self.queue_count_label)
        vlay.addLayout(q_hdr)

        # Queue list (single-click opens editor)
        self.queue_compact = QListWidget()
        self.queue_compact.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_compact.setToolTip("Click to edit plan")
        self.queue_compact.itemClicked.connect(self._on_queue_item_clicked)
        vlay.addWidget(self.queue_compact, 1)

        # Queue action buttons
        q_btns = QHBoxLayout()
        q_btns.setSpacing(4)
        btn_add = QPushButton("＋ Add")
        btn_add.setObjectName("btn_primary")
        btn_add.clicked.connect(self._add_plan)
        btn_rem = QPushButton("Remove")
        btn_rem.clicked.connect(self._remove_plan)
        btn_clr = QPushButton("Clear")
        btn_clr.setObjectName("btn_danger")
        btn_clr.clicked.connect(self._clear_queue)
        q_btns.addWidget(btn_add)
        q_btns.addWidget(btn_rem)
        q_btns.addStretch()
        q_btns.addWidget(btn_clr)
        vlay.addLayout(q_btns)

        # Console
        lbl_con = QLabel("CONSOLE")
        lbl_con.setObjectName("section_title")
        vlay.addWidget(lbl_con)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Courier New", 10))
        self.console.setPlaceholderText("RE manager output…")
        self.console.setMaximumHeight(160)
        vlay.addWidget(self.console)

        return w

    # ── Right panel: detachable plot tabs ──────────────────────────────────────

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 8, 8)
        lay.setSpacing(0)

        # Stable container so we can swap plot_tabs in/out without touching `w`
        self._plot_container = QWidget()
        self._plot_container_lay = QVBoxLayout(self._plot_container)
        self._plot_container_lay.setContentsMargins(0, 0, 0, 0)
        self._plot_container_lay.setSpacing(0)

        self.plot_tabs = QTabWidget()
        self.live_viewer    = LiveViewer()
        self.history_widget = ExperimentHistoryWidget()
        self.plot_tabs.addTab(self.live_viewer,    "📡  Live")
        self.plot_tabs.addTab(self.history_widget, "📂  History")

        # Detach button in top-right corner of the tab bar
        self._detach_btn = QPushButton("⊔  Detach")
        self._detach_btn.setFixedHeight(22)
        self._detach_btn.setStyleSheet("font-size: 11px; padding: 0 8px; margin: 1px;")
        self._detach_btn.setToolTip("Detach plots into a floating window")
        self._detach_btn.clicked.connect(self._toggle_detach)
        self.plot_tabs.setCornerWidget(self._detach_btn, Qt.Corner.TopRightCorner)

        self._plot_container_lay.addWidget(self.plot_tabs)
        lay.addWidget(self._plot_container, 1)
        return w

    # ── Plot detach / reattach ─────────────────────────────────────────────────

    def _toggle_detach(self):
        if self._detached_win is None:
            self._do_detach()
        else:
            self._do_reattach()

    def _do_detach(self):
        # Remove plot_tabs from container and add placeholder
        self._plot_container_lay.removeWidget(self.plot_tabs)

        self._plot_placeholder = QLabel(
            "Plots are in a floating window.\nClose it to re-attach.")
        self._plot_placeholder.setObjectName("dim_text")
        self._plot_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plot_placeholder.setStyleSheet("font-size: 14px;")
        self._plot_container_lay.addWidget(self._plot_placeholder)

        # Floating window
        win = QMainWindow()
        win.setWindowTitle("EasyBluesky — Plots")
        win.setMinimumSize(900, 600)
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        cw  = QWidget()
        cl  = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        self.plot_tabs.setParent(cw)
        cl.addWidget(self.plot_tabs)
        win.setCentralWidget(cw)

        def _close(event):
            self._do_reattach()
            event.accept()
        win.closeEvent = _close

        self._detached_win = win
        win.show()
        self._detach_btn.setText("⊓  Re-attach")

    def _do_reattach(self):
        if not self._detached_win:
            return

        # Remove placeholder
        if self._plot_placeholder:
            self._plot_container_lay.removeWidget(self._plot_placeholder)
            self._plot_placeholder.deleteLater()
            self._plot_placeholder = None

        # Move plot_tabs back into container
        self.plot_tabs.setParent(self._plot_container)
        self._plot_container_lay.addWidget(self.plot_tabs)

        self._detached_win.hide()
        self._detached_win = None
        self._detach_btn.setText("⊔  Detach")

    # ── Queue operations ───────────────────────────────────────────────────────

    def _add_plan(self):
        if not self.worker:
            return
        dlg = PlanDialog(self._plans, self._devices, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            ok, msg = self.worker.add_item(dlg.result_item)
            self._log(f"{'✓' if ok else '✗'} Add plan: {msg}")

    def _remove_plan(self):
        if not self.worker:
            return
        li = self.queue_compact.currentItem()
        if not li:
            return
        uid = li.data(Qt.ItemDataRole.UserRole)
        if uid:
            ok, msg = self.worker.remove_item(uid)
            self._log(f"{'✓' if ok else '✗'} Remove: {msg}")

    def _clear_queue(self):
        if not self.worker:
            return
        r = QMessageBox.question(self, "Clear Queue",
                                 "Remove all items from the queue?")
        if r == QMessageBox.StandardButton.Yes:
            ok, msg = self.worker.clear_queue()
            self._log(f"{'✓' if ok else '✗'} Clear queue: {msg}")

    def _on_queue_item_clicked(self, li: QListWidgetItem):
        if not self.worker:
            return
        item = li.data(Qt.ItemDataRole.UserRole + 1)
        if not item:
            return
        dlg = PlanDialog(self._plans, self._devices, item=item, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            ok, msg = self.worker.update_item(dlg.result_item)
            self._log(f"{'✓' if ok else '✗'} Update plan: {msg}")

    # ── Console ────────────────────────────────────────────────────────────────

    def append_console(self, text: str):
        self.console.appendPlainText(text)
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_console(f"[{ts}] {msg}")

    # ── Public setters for plans/devices (needed by PlanDialog) ────────────────

    def set_plans(self, plans: dict):
        self._plans = plans

    def set_devices(self, devices: dict):
        self._devices = devices

    # ── Experiment management ──────────────────────────────────────────────────

    def new_experiment(self):
        name, ok = QInputDialog.getText(
            self, "New Experiment", "Experiment name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        parent_dir = QFileDialog.getExistingDirectory(
            self, "Choose parent folder for experiment",
            EXPERIMENTS_DIR,
        )
        if not parent_dir:
            return

        ts          = datetime.now()
        sanitized   = re.sub(r"[^\w\-]", "_", name)
        folder_name = ts.strftime("%Y%m%d_%H%M%S_") + sanitized
        exp_dir     = Path(parent_dir) / folder_name
        runs_dir    = exp_dir / "runs"
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Could not create experiment folder:\n{e}")
            return

        exp_info    = {"name": name, "created": ts.isoformat(), "description": ""}
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
            QMessageBox.warning(
                self, "Invalid Folder",
                "Selected folder does not contain experiment.json.")
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
        self._logged_uids     = set()
        created = info.get("created", "")
        try:
            self._exp_created_at = datetime.fromisoformat(created).timestamp()
        except Exception:
            self._exp_created_at = 0.0

        name = info.get("name", Path(path).name)
        display_path = path if len(path) <= 60 else "…" + path[-59:]
        self.exp_name_label.setText(name)
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
            if path == self._active_exp_path:
                li.setForeground(QColor(ACCENT))
            self.recent_list.addItem(li)

    # ── Plan log ───────────────────────────────────────────────────────────────

    @staticmethod
    def _plan_summary(name: str, kwargs: dict) -> str:
        parts = []
        motor  = kwargs.get("motor")
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

    def _search_dirs(self) -> list:
        dirs = []
        if self._active_exp_path:
            dirs.append(Path(self._active_exp_path) / "runs")
        dirs.append(Path(DATA_RUNS_DIR))
        return dirs

    @staticmethod
    def _run_file_exists(runs_dir: Path, run_uids: list) -> bool:
        return any((runs_dir / f"{r}.jsonl").exists() for r in run_uids)

    def _find_run_file_for_entry(self, entry: dict) -> "Path | None":
        stored = entry.get("run_file", "")
        if stored and Path(stored).exists():
            return Path(stored)

        run_uids = entry.get("run_uids", [])
        for ruid in run_uids:
            for d in self._search_dirs():
                f = d / f"{ruid}.jsonl"
                if f.exists():
                    return f

        plan_name = entry.get("name", "")
        ts_str    = entry.get("timestamp", "")
        if not ts_str:
            return None
        try:
            entry_ts = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            return None

        for search_dir in self._search_dirs():
            if not search_dir.exists():
                continue
            candidates = sorted(
                search_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )[:60]
            for fpath in candidates:
                try:
                    mtime = fpath.stat().st_mtime
                    if abs(mtime - entry_ts) > 180:
                        continue
                    with open(fpath) as f:
                        first_line = f.readline()
                    _, doc = json.loads(first_line)
                    if doc.get("plan_name") == plan_name:
                        return fpath
                except Exception:
                    pass
        return None

    def _entry_belongs_here(self, entry: dict, runs_dir: Path) -> bool:
        stored = entry.get("run_file", "")
        if stored and Path(stored).exists():
            return True
        run_uids = entry.get("run_uids", [])
        if run_uids:
            for d in self._search_dirs():
                if self._run_file_exists(d, run_uids):
                    return True
            return False
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
            return ts >= self._exp_created_at
        except Exception:
            return False

    def _load_plan_log(self, exp_path: str):
        runs_dir = Path(exp_path) / "runs"
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
                        uid   = entry.get("uid", "")
                        if uid:
                            self._logged_uids.add(uid)
                        if not self._entry_belongs_here(entry, runs_dir):
                            continue
                        entries.append(entry)
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
                li = QListWidgetItem(
                    f"{icon}  {t_str}  {name}{summary}{dur_str}")
                li.setForeground(QColor(color))
                li.setData(Qt.ItemDataRole.UserRole, entry)
                self.plan_log_list.addItem(li)
        except Exception:
            pass

    # ── Public update slots ────────────────────────────────────────────────────

    def update_history(self, items: list):
        if not self._active_exp_path:
            return
        log_file = Path(self._active_exp_path) / "plans_log.jsonl"
        changed  = False

        for item in items:
            uid = item.get("item_uid", "")
            if not uid or uid in self._logged_uids:
                continue
            result      = item.get("result") or {}
            exit_status = result.get("exit_status", "")
            if not exit_status:
                continue
            t_stop   = result.get("time_stop",  0)
            t_start  = result.get("time_start", 0)
            run_uids = result.get("run_uids", [])

            # Timing is the primary gate: skip plans that finished before this experiment
            if t_stop and self._exp_created_at and t_stop < self._exp_created_at:
                self._logged_uids.add(uid)
                continue

            timestamp = (
                datetime.fromtimestamp(t_stop).isoformat()
                if t_stop else datetime.now().isoformat()
            )
            dur = (t_stop - t_start) if (t_stop and t_start) else None
            tmp_entry = {
                "timestamp":   timestamp,
                "uid":         uid,
                "run_uids":    run_uids,
                "name":        item.get("name", ""),
                "kwargs":      item.get("kwargs", {}) or {},
                "exit_status": exit_status,
                "duration_s":  round(dur, 2) if dur else None,
                "run_file":    "",
            }
            found = self._find_run_file_for_entry(tmp_entry)
            entry = {**tmp_entry, "run_file": str(found) if found else ""}
            try:
                with open(log_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                self._logged_uids.add(uid)
                changed = True
            except Exception:
                pass

        if changed:
            self._load_plan_log(self._active_exp_path)

    def update_compact_queue(self, items: list):
        self.queue_compact.clear()
        for i, item in enumerate(items):
            name    = item.get("name", "unknown")
            kwargs  = item.get("kwargs", {}) or {}
            uid     = item.get("item_uid", "")
            summary = self._plan_summary(name, kwargs)
            li = QListWidgetItem(f"{i + 1}.  {name}{summary}")
            li.setData(Qt.ItemDataRole.UserRole,     uid)
            li.setData(Qt.ItemDataRole.UserRole + 1, item)
            self.queue_compact.addItem(li)
        n = len(items)
        self.queue_count_label.setText(f"{n} item{'s' if n != 1 else ''}")

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

    def _on_plan_log_clicked(self, li: QListWidgetItem):
        entry = li.data(Qt.ItemDataRole.UserRole)
        if not entry or not self._active_exp_path:
            return
        filepath = self._find_run_file_for_entry(entry)
        if not filepath:
            return
        self.history_widget.load_jsonl_file(str(filepath))
        self.plot_tabs.setCurrentIndex(1)

    def _on_plan_log_double_clicked(self, li: QListWidgetItem):
        entry = li.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        ts_str = entry.get("timestamp", "")
        dur    = entry.get("duration_s")
        try:
            t_stop = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            t_stop = 0.0
        t_start = (t_stop - dur) if (t_stop and dur) else t_stop

        # Build the format RunDetailDialog expects
        item = {
            "name":     entry.get("name", "?"),
            "kwargs":   entry.get("kwargs", {}) or {},
            "_run_file": entry.get("run_file", ""),
            "result": {
                "exit_status": entry.get("exit_status", "?"),
                "time_start":  t_start,
                "time_stop":   t_stop,
                "run_uids":    entry.get("run_uids", []),
            },
        }
        dlg = RunDetailDialog(item, worker=self.worker, parent=self)
        dlg.exec()
