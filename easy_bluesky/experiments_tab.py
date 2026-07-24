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

try:
    import h5py
    H5PY_AVAILABLE = True
except ImportError:
    H5PY_AVAILABLE = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QInputDialog, QFileDialog, QMessageBox,
    QAbstractItemView, QTabWidget, QComboBox, QPlainTextEdit, QDialog,
    QMainWindow, QLineEdit, QFormLayout, QGroupBox, QMenu,
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
from .plot_tools import setup_crosshair

# Plans that never produce detector data — shown in neutral color in logs
_MOTION_PLANS = frozenset({
    "mv", "mvr", "abs_set", "rel_set", "move", "sleep", "rd", "set",
    "kickoff", "complete", "collect", "null",
})
_NEUTRAL_COLOR = "#aaaaaa"  # light grey for motion-only plans


def _is_motion_only(name: str, kwargs: dict) -> bool:
    return name.lower() in _MOTION_PLANS


# ── Embedded single-run history plot ──────────────────────────────────────────

class ExperimentHistoryWidget(QWidget):
    """Plots one or more runs' data loaded directly from JSONL files."""

    COLORS = PLOT_COLORS
    move_requested = pyqtSignal(str, float)   # (motor_name, position)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dfs: list = []    # list of (pd.DataFrame, label_str)
        self._curves: dict = {}
        self._crosshair_cleanup = None
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

            self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)
        else:
            main.addWidget(
                QLabel("pyqtgraph not available — pip install pyqtgraph"), 1)

        # Bottom bar: stats left, cursor coords right
        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim_text")
        bot.addWidget(self.stats_label, 1)

        self.coord_label = QLabel("")
        self.coord_label.setObjectName("dim_text")
        self.coord_label.setStyleSheet("font-size: 11px; padding: 2px 4px; font-family: Menlo, Monaco, Courier New, monospace;")
        bot.addWidget(self.coord_label)
        main.addLayout(bot)

        if PG_AVAILABLE:
            self._crosshair_cleanup = setup_crosshair(
                self.plot_widget, self.coord_label, lambda: self._curves
            )

    # ── Data loading ────────────────────────────────────────────────────────────

    def _parse_jsonl(self, filepath: str):
        """Parse one JSONL file. Returns (DataFrame, label) or (None, '')."""
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
                        row = {"seq_num": doc.get("seq_num"), "time": doc.get("time")}
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
            return None, f"Error: {e}"

        if not events:
            return None, "no events"

        try:
            import pandas as pd
            df = pd.DataFrame(events)
        except ImportError:
            return None, "pandas missing"

        plan  = start_doc.get("plan_name", "?")
        uid8  = start_doc.get("uid", filepath)[:8]
        label = f"{plan} [{uid8}]  {len(events)} pts"
        return df, label

    def load_jsonl_file(self, filepath: str):
        """Load a single JSONL file and plot it."""
        df, label = self._parse_jsonl(filepath)
        if df is None:
            self.run_label.setText(label)
            return
        self._dfs = [(df, label)]
        self._setup_axes([df])
        self.run_label.setText(label)

    def load_jsonl_files(self, filepaths: list):
        """Load multiple JSONL files and overlay them."""
        self._dfs = []
        for fp in filepaths[:8]:
            df, label = self._parse_jsonl(str(fp))
            if df is not None:
                self._dfs.append((df, label))

        if not self._dfs:
            self.run_label.setText("No data found in selected runs")
            return
        self._setup_axes([df for df, _ in self._dfs])
        if len(self._dfs) > 1:
            self.run_label.setText(f"{len(self._dfs)} runs overlaid")

    def _setup_axes(self, dfs: list):
        """Populate X/Y combos with columns common to all DataFrames."""
        if not dfs:
            return

        # Numeric columns in each DataFrame
        def numeric_cols(df):
            return [c for c in df.columns if df[c].dtype.kind in ("f", "i", "u")]

        # Intersection: only columns present in ALL DataFrames
        col_sets = [set(numeric_cols(df)) for df in dfs]
        common   = col_sets[0].intersection(*col_sets[1:]) if len(col_sets) > 1 else col_sets[0]
        # Preserve order from the first DataFrame
        cols = [c for c in numeric_cols(dfs[0]) if c in common]

        self.x_combo.clear()
        self.x_combo.addItems(cols)
        self.y_list.clear()
        for c in cols:
            self.y_list.addItem(QListWidgetItem(c))

        motor_cols = [c for c in cols
                      if any(w in c.lower() for w in ("motor", "pos", "stage", "enc"))]
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
        if not self._dfs or not PG_AVAILABLE:
            return
        # Remove existing curves without clearing the whole widget (which would
        # destroy the crosshair InfiniteLines added by setup_crosshair).
        for curve in self._curves.values():
            try:
                self.plot_widget.removeItem(curve)
            except Exception:
                pass
        pi = self.plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves = {}
        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return

        color_idx = 0
        stats = []
        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x = df[xc].values.astype(float)
            for yc in ycs:
                if yc not in df.columns:
                    continue
                y    = df[yc].values.astype(float)
                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_ = x[mask], y[mask]
                if not len(x_):
                    continue
                color = self.COLORS[color_idx % len(self.COLORS)]
                pen   = pg.mkPen(color=color, width=2)
                curve_name = yc if len(self._dfs) == 1 else f"{yc}  [{df_label[:20]}]"
                curve = self.plot_widget.plot(x_, y_, pen=pen, name=curve_name,
                                              symbol="o", symbolSize=5,
                                              symbolBrush=color, symbolPen=None)
                self._curves[curve_name] = curve
                color_idx += 1
                stats.append(
                    f"{yc}: min={y_.min():.4g}  max={y_.max():.4g}")

        self.plot_widget.setLabel("bottom", xc)
        self.plot_widget.setLabel("left",   ", ".join(ycs))
        self.stats_label.setText("   ".join(stats))

    # ── Double-click: move motor ───────────────────────────────────────────────

    def _on_plot_clicked(self, event):
        if not event.double():
            return
        pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return

        vb = self.plot_widget.getPlotItem().vb
        mp = vb.mapSceneToView(pos)
        x_val   = mp.x()
        x_label = self.x_combo.currentText() or ""

        motor_guess = x_label
        for suffix in ("_readback", "_setpoint", "_user_readback", "_user_setpoint"):
            if motor_guess.endswith(suffix):
                motor_guess = motor_guess[: -len(suffix)]
                break

        r = QMessageBox.question(
            self, "Move Motor",
            f"Move  '{motor_guess}'  to  {x_val:.5g} ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self.move_requested.emit(motor_guess, x_val)


# ── Main experiments tab ───────────────────────────────────────────────────────

class ExperimentsTab(QWidget):
    """Three-panel layout:
      Left  — experiment info, sample fields, plan log
      Middle — compact queue + console
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
        self._shown_error_uids: set = set()
        self._exp_created_at: float = 0.0
        self._exp_end_time: float   = 0.0
        self._next_scan_num: int    = 1
        self._detached_win     = None
        self._plot_placeholder = None
        self._sample_name: str = ""
        self._sample_description: str = ""
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
        splitter.setSizes([240, 260, 720])
        lay.addWidget(splitter)

    # ── Left panel: experiment info + sample + plan log ────────────────────────

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
        btn_open = QPushButton("Open…")
        btn_open.clicked.connect(self.open_experiment)
        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_open)
        vlay.addLayout(btn_row)

        # ── Sample fields ──────────────────────────────────────────────────────
        sample_grp = QGroupBox("Sample")
        sample_lay = QFormLayout(sample_grp)
        sample_lay.setSpacing(4)

        self.sample_name_edit = QLineEdit()
        self.sample_name_edit.setPlaceholderText("e.g. Si_wafer_01")
        self.sample_name_edit.editingFinished.connect(self._on_sample_name_commit)
        sample_lay.addRow("Name:", self.sample_name_edit)

        self.sample_desc_edit = QLineEdit()
        self.sample_desc_edit.setPlaceholderText("optional description")
        self.sample_desc_edit.editingFinished.connect(self._on_sample_desc_commit)
        sample_lay.addRow("Desc:", self.sample_desc_edit)

        self.sample_dir_label = QLabel("—")
        self.sample_dir_label.setObjectName("dim_text")
        self.sample_dir_label.setStyleSheet("font-size: 10px;")
        self.sample_dir_label.setWordWrap(True)
        sample_lay.addRow("Folder:", self.sample_dir_label)

        vlay.addWidget(sample_grp)

        lbl_log = QLabel("PLAN LOG  (click to plot · multi-select to overlay)")
        lbl_log.setObjectName("section_title")
        vlay.addWidget(lbl_log)

        self._plan_log_search = QLineEdit()
        self._plan_log_search.setPlaceholderText("🔍  Search plan log…")
        self._plan_log_search.setClearButtonEnabled(True)
        self._plan_log_search.textChanged.connect(self._filter_plan_log)
        vlay.addWidget(self._plan_log_search)

        self.plan_log_list = QListWidget()
        self.plan_log_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.plan_log_list.itemSelectionChanged.connect(self._on_plan_log_selection_changed)
        self.plan_log_list.itemDoubleClicked.connect(self._on_plan_log_double_clicked)
        self.plan_log_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plan_log_list.customContextMenuRequested.connect(self._plan_log_context_menu)
        vlay.addWidget(self.plan_log_list, 1)

        self._btn_export_h5 = QPushButton("Export HDF5…")
        self._btn_export_h5.setObjectName("btn_primary")
        self._btn_export_h5.setToolTip("Save all scan data to a single HDF5 file")
        self._btn_export_h5.clicked.connect(self._export_hdf5)
        if not H5PY_AVAILABLE:
            self._btn_export_h5.setEnabled(False)
            self._btn_export_h5.setToolTip("pip install h5py to enable HDF5 export")
        vlay.addWidget(self._btn_export_h5)

        return w

    # ── Middle panel: queue + buttons + console ────────────────────────────────

    def _build_middle(self) -> QWidget:
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 8, 4, 8)
        vlay.setSpacing(4)

        q_hdr = QHBoxLayout()
        lbl_q = QLabel("QUEUE")
        lbl_q.setObjectName("section_title")
        q_hdr.addWidget(lbl_q)
        q_hdr.addStretch()
        self.queue_count_label = QLabel("0 items")
        self.queue_count_label.setObjectName("dim_text")
        q_hdr.addWidget(self.queue_count_label)
        vlay.addLayout(q_hdr)

        self.queue_compact = QListWidget()
        self.queue_compact.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_compact.setToolTip("Click to edit plan")
        self.queue_compact.itemClicked.connect(self._on_queue_item_clicked)
        vlay.addWidget(self.queue_compact, 1)

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

        self._plot_container = QWidget()
        self._plot_container_lay = QVBoxLayout(self._plot_container)
        self._plot_container_lay.setContentsMargins(0, 0, 0, 0)
        self._plot_container_lay.setSpacing(0)

        self.plot_tabs = QTabWidget()
        self.live_viewer    = LiveViewer(worker=self.worker)
        self.history_widget = ExperimentHistoryWidget()
        self.history_widget.move_requested.connect(self._on_move_requested)
        self.plot_tabs.addTab(self.live_viewer,    "📡  Live")
        self.plot_tabs.addTab(self.history_widget, "📂  History")

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
        self._plot_container_lay.removeWidget(self.plot_tabs)
        self._plot_placeholder = QLabel(
            "Plots are in a floating window.\nClose it to re-attach.")
        self._plot_placeholder.setObjectName("dim_text")
        self._plot_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plot_placeholder.setStyleSheet("font-size: 14px;")
        self._plot_container_lay.addWidget(self._plot_placeholder)

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
        if self._plot_placeholder:
            self._plot_container_lay.removeWidget(self._plot_placeholder)
            self._plot_placeholder.deleteLater()
            self._plot_placeholder = None
        self.plot_tabs.setParent(self._plot_container)
        self._plot_container_lay.addWidget(self.plot_tabs)
        self._detached_win.hide()
        self._detached_win = None
        self._detach_btn.setText("⊔  Detach")

    # ── Motor move (from history plot double-click) ────────────────────────────

    def _on_move_requested(self, motor: str, position: float):
        if not self.worker:
            return
        item = {
            "name":      "mv",
            "args":      [motor, position],
            "kwargs":    {},
            "item_type": "plan",
        }
        ok, msg = self.worker.execute_item(item)
        self._log(f"{'✓' if ok else '✗'} Move {motor} → {position:.5g}: {msg}")

    # ── Queue operations ───────────────────────────────────────────────────────

    def _build_metadata(self) -> dict:
        """Build md dict injected automatically into every submitted plan."""
        md: dict = {}
        if self._active_exp_path:
            md["exp_dir"] = self._active_exp_path
        if self._sample_name:
            md["sample_name"] = self._sample_name
        if self._sample_description:
            md["sample_description"] = self._sample_description
        return md

    def _inject_metadata(self, result_item: dict):
        """Inject experiment/sample metadata into a plan item's md key."""
        auto_md = self._build_metadata()
        if not auto_md:
            return result_item
        existing_md = result_item.get("kwargs", {}).get("md", {}) or {}
        merged = {**auto_md, **existing_md}   # user-supplied md wins
        result_item.setdefault("kwargs", {})["md"] = merged
        return result_item

    def _add_plan(self):
        if not self.worker:
            return
        dlg = PlanDialog(self._plans, self._devices, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            item = self._inject_metadata(dlg.result_item)
            ok, msg = self.worker.add_item(item)
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
            updated = self._inject_metadata(dlg.result_item)
            ok, msg = self.worker.update_item(updated)
            self._log(f"{'✓' if ok else '✗'} Update plan: {msg}")

    # ── Console ────────────────────────────────────────────────────────────────

    def append_console(self, text: str):
        self.console.appendPlainText(text)
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_console(f"[{ts}] {msg}")

    # ── Public setters ─────────────────────────────────────────────────────────

    def is_ready_to_run(self) -> tuple:
        """Return (ok: bool, reason: str). ok is False if prerequisites are missing."""
        if not self._active_exp_path:
            return False, "No active experiment — create or open one first."
        if not self._sample_name:
            return False, "Sample name is required before starting the queue.\n\nEnter a sample name in the Experiments tab."
        return True, ""

    def set_plans(self, plans: dict):
        self._plans = plans

    def set_devices(self, devices: dict):
        self._devices = devices

    # ── Sample management ──────────────────────────────────────────────────────

    def _on_sample_name_commit(self):
        name = self.sample_name_edit.text().strip()
        if not name or name == self._sample_name:
            return
        if not self._active_exp_path:
            QMessageBox.warning(self, "No Experiment",
                                "Open or create an experiment first.")
            self.sample_name_edit.setText(self._sample_name)
            return
        safe = re.sub(r"[^\w\-]", "_", name)
        sample_dir = Path(self._active_exp_path) / "samples" / safe
        if sample_dir.exists():
            r = QMessageBox.question(
                self, "Sample Exists",
                f"Sample folder '{safe}' already exists.\nUse it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                self.sample_name_edit.setText(self._sample_name)
                return
        else:
            try:
                sample_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(self, "Error",
                                     f"Could not create sample folder:\n{e}")
                self.sample_name_edit.setText(self._sample_name)
                return
        self._sample_name = name
        display = str(sample_dir) if len(str(sample_dir)) <= 55 else "…" + str(sample_dir)[-54:]
        self.sample_dir_label.setText(display)
        self._log(f"✓ Sample: {safe}")

    def _on_sample_desc_commit(self):
        self._sample_description = self.sample_desc_edit.text().strip()

    def _clear_sample(self):
        self._sample_name = ""
        self._sample_description = ""
        self.sample_name_edit.clear()
        self.sample_desc_edit.clear()
        self.sample_dir_label.setText("—")

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
        self._clear_sample()
        self.experiment_changed.emit(str(runs_dir))
        self._exp_end_time = self._compute_exp_end_time()

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
        self._clear_sample()
        runs_dir = Path(path) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_changed.emit(str(runs_dir))

    def load_experiment(self, path: str, info: dict = None):
        """Public entry point called from File → Recent Experiments menu."""
        if not Path(path).exists():
            QMessageBox.warning(self, "Not Found",
                                f"Experiment folder not found:\n{path}")
            return
        if info is None:
            exp_json = Path(path) / "experiment.json"
            try:
                info = json.loads(exp_json.read_text())
            except Exception:
                info = {"name": Path(path).name, "path": path, "created": ""}

        active_info = {
            "name":    info.get("name", Path(path).name),
            "path":    path,
            "created": info.get("created", ""),
        }
        self._write_active_experiment(active_info)
        self._set_active_experiment(path, active_info)
        self._clear_sample()
        runs_dir = Path(path) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_changed.emit(str(runs_dir))

    def get_recent_experiments(self, limit: int = 10) -> list:
        """Return list of (path, info) tuples, most-recent first."""
        exps_dir = Path(EXPERIMENTS_DIR)
        if not exps_dir.exists():
            return []
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
        return [(path, info) for _, path, info in entries[:limit]]

    # ── HDF5 export / import ───────────────────────────────────────────────────

    def _export_hdf5(self):
        if not H5PY_AVAILABLE:
            QMessageBox.warning(self, "h5py Missing",
                                "Install h5py first:\n  pip install h5py")
            return
        if not self._active_exp_path:
            QMessageBox.warning(self, "No Experiment",
                                "Open or create an experiment first.")
            return

        exp_name     = self.exp_name_label.text()
        default_path = str(Path(self._active_exp_path) / f"{exp_name}.h5")
        path, _      = QFileDialog.getSaveFileName(
            self, "Export Experiment to HDF5", default_path,
            "HDF5 Files (*.h5 *.hdf5)"
        )
        if not path:
            return

        log_file = Path(self._active_exp_path) / "plans_log.jsonl"
        if not log_file.exists():
            QMessageBox.warning(self, "No Data",
                                "No plan log found for this experiment.")
            return

        try:
            entries: list = []
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass

            # Back-fill scan numbers for old entries without the field
            next_n = 1
            for e in entries:
                if e.get("scan_num") is None:
                    e["scan_num"] = next_n
                next_n = max(next_n, e.get("scan_num", 0)) + 1

            with h5py.File(path, "w") as hf:
                # ── Experiment-level metadata ──────────────────────────────
                meta = hf.create_group("metadata")
                meta.attrs["experiment_name"] = exp_name
                meta.attrs["exp_dir"]         = self._active_exp_path
                meta.attrs["n_scans"]         = len(entries)
                if self._sample_name:
                    meta.attrs["sample_name"] = self._sample_name
                if self._sample_description:
                    meta.attrs["sample_description"] = self._sample_description

                # ── One group per plan ─────────────────────────────────────
                for entry in entries:
                    scan_num   = entry.get("scan_num", 0)
                    name       = entry.get("name", "?")
                    args       = entry.get("args", []) or []
                    kwargs     = entry.get("kwargs", {}) or {}
                    md         = kwargs.get("md", {}) or {}
                    group_name = f"scan_{scan_num:04d}"

                    grp = hf.create_group(group_name)
                    grp.attrs["plan_name"]   = name
                    grp.attrs["scan_num"]    = scan_num
                    grp.attrs["timestamp"]   = entry.get("timestamp", "")
                    grp.attrs["exit_status"] = entry.get("exit_status", "")
                    if entry.get("duration_s") is not None:
                        grp.attrs["duration_s"] = float(entry["duration_s"])
                    for attr in ("sample_name", "sample_description", "exp_dir"):
                        val = md.get(attr, "")
                        if val:
                            grp.attrs[attr] = val

                    motor = kwargs.get("motor") or (
                        args[0] if name.lower() in _MOTION_PLANS and args else "")
                    if motor:
                        grp.attrs["motor"] = str(motor)
                    dets = kwargs.get("detectors") or kwargs.get("detector_list", [])
                    if isinstance(dets, str):
                        dets = [dets]
                    if dets:
                        grp.attrs["detectors"] = ",".join(str(d) for d in dets)

                    # ── Event data ─────────────────────────────────────────
                    run_file = entry.get("run_file", "")
                    if not run_file:
                        found = self._find_run_file_for_entry(entry)
                        run_file = str(found) if found else ""

                    if run_file and Path(run_file).exists():
                        df, _ = self.history_widget._parse_jsonl(run_file)
                        if df is not None:
                            for col in df.columns:
                                try:
                                    arr = df[col].to_numpy(dtype=float,
                                                           na_value=float("nan"))
                                    grp.create_dataset(col, data=arr,
                                                       compression="gzip")
                                except Exception:
                                    pass
                            grp.attrs["n_events"] = len(df)

            n_scans = len(entries)
            self._log(f"✓ Exported {n_scans} scans → {Path(path).name}")
            QMessageBox.information(
                self, "Export Complete",
                f"Exported {n_scans} plans to:\n{path}"
            )

        except Exception as e:
            self._log(f"✗ HDF5 export failed: {e}")
            QMessageBox.critical(self, "Export Failed", str(e))

    def _write_active_experiment(self, info: dict):
        active_file = Path(ACTIVE_EXPERIMENT_FILE)
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(json.dumps(info, indent=2))

    def _compute_exp_end_time(self) -> float:
        if not self._exp_created_at:
            return 0.0
        exps_dir = Path(EXPERIMENTS_DIR)
        if not exps_dir.exists():
            return 0.0
        next_t = float("inf")
        for d in exps_dir.iterdir():
            if not d.is_dir() or str(d) == self._active_exp_path:
                continue
            exp_json = d / "experiment.json"
            if not exp_json.exists():
                continue
            try:
                info = json.loads(exp_json.read_text())
                ct = datetime.fromisoformat(info.get("created", "")).timestamp()
                if ct > self._exp_created_at and ct < next_t:
                    next_t = ct
            except Exception:
                pass
        return next_t if next_t != float("inf") else 0.0

    def _set_active_experiment(self, path: str, info: dict):
        self._active_exp_path = path
        if self.worker and hasattr(self.worker, "set_doc_writer_exp_dir"):
            self.worker.set_doc_writer_exp_dir(path)
        self._logged_uids     = set()
        created = info.get("created", "")
        try:
            self._exp_created_at = datetime.fromisoformat(created).timestamp()
        except Exception:
            self._exp_created_at = 0.0
        self._exp_end_time = self._compute_exp_end_time()

        name = info.get("name", Path(path).name)
        display_path = path if len(path) <= 60 else "…" + path[-59:]
        self.exp_name_label.setText(name)
        self.exp_path_label.setText(display_path)
        self.exp_date_label.setText(f"Created: {created[:10]}" if created else "")
        self._load_plan_log(path)

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

    # ── Plan log ───────────────────────────────────────────────────────────────

    @staticmethod
    def _plan_summary(name: str, kwargs: dict, args: list = None) -> str:
        args    = list(args or [])
        parts   = []
        name_lc = name.lower()

        # ── Readable (detectors) ──────────────────────────────────────────────
        dets = kwargs.get("detectors") or kwargs.get("detector_list", [])
        if isinstance(dets, str):
            dets = [dets]
        # Fallback: first positional arg is often the detectors list
        if not dets and args and isinstance(args[0], list):
            dets = args[0]
        if dets:
            parts.append("det: [" + ", ".join(str(d) for d in dets) + "]")

        # ── Movable (motor) ───────────────────────────────────────────────────
        motor  = kwargs.get("motor")
        motors = kwargs.get("motors")
        if not motor and isinstance(motors, list) and motors:
            motor = motors[0]
        if not motor and name_lc in _MOTION_PLANS and args:
            motor = args[0]
        # scan-style: [dets_list, motor, start, stop, ...] in positional args
        if not motor and name_lc not in _MOTION_PLANS:
            if len(args) >= 2 and isinstance(args[0], list) and not isinstance(args[1], (int, float)):
                motor = args[1]

        if motor:
            start = kwargs.get("start")
            stop  = kwargs.get("stop")
            num   = kwargs.get("num")
            # args-based layout: [dets_list, motor, start, stop, ...]
            if start is None and len(args) >= 4 and isinstance(args[0], list):
                try:
                    start, stop = float(args[2]), float(args[3])
                except (TypeError, ValueError):
                    pass
            s = f"mot: {motor}"
            if start is not None and stop is not None:
                s += f" [{start} → {stop}"
                if num is not None:
                    s += f", {num} pts"
                s += "]"
            elif name_lc in ("mv", "mvr") and len(args) >= 2:
                try:
                    s += f" → {float(args[1]):.4g}"
                except (TypeError, ValueError):
                    s += f" → {args[1]}"
            parts.insert(0, s)

        # ── num pts / delay (when not already shown inside the motor range) ───
        num_shown = any("pts" in p for p in parts)
        num = kwargs.get("num")
        if num is not None and not num_shown:
            parts.append(f"{num} pts")
        delay = kwargs.get("delay")
        if delay is not None:
            parts.append(f"delay={delay:.4g} s")

        return "  " + "  |  ".join(parts) if parts else ""

    def _search_dirs(self) -> list:
        dirs = []
        if self._active_exp_path:
            dirs.append(Path(self._active_exp_path) / "runs")
        exps = Path(EXPERIMENTS_DIR)
        if exps.exists():
            for d in sorted(exps.iterdir(), reverse=True):
                rd = d / "runs"
                if rd.is_dir() and rd not in dirs:
                    dirs.append(rd)
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

    def _filter_plan_log(self, text: str):
        q = text.strip().lower()
        for i in range(self.plan_log_list.count()):
            item = self.plan_log_list.item(i)
            item.setHidden(bool(q and q not in item.text().lower()))

    def _load_plan_log(self, exp_path: str):
        runs_dir = Path(exp_path) / "runs"
        log_file = Path(exp_path) / "plans_log.jsonl"
        self.plan_log_list.clear()
        self._plan_log_search.clear()

        self._logged_uids = set()
        exps_dir = Path(EXPERIMENTS_DIR)
        if exps_dir.exists():
            for d in exps_dir.iterdir():
                other_log = d / "plans_log.jsonl"
                if other_log.exists():
                    try:
                        with open(other_log) as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                uid = json.loads(line).get("uid", "")
                                if uid:
                                    self._logged_uids.add(uid)
                    except Exception:
                        pass

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

            # Back-fill scan_num for old entries that predate this field.
            # Entries are in file order (chronological), so assign 1, 2, 3…
            next_backfill = 1
            for e in entries:
                if e.get("scan_num") is None:
                    e["scan_num"] = next_backfill
                next_backfill = max(next_backfill, e.get("scan_num", 0)) + 1

            max_num = max((e.get("scan_num", 0) for e in entries), default=0)
            self._next_scan_num = max_num + 1

            for entry in reversed(entries):
                name    = entry.get("name", "?")
                args    = entry.get("args", []) or []
                kwargs  = entry.get("kwargs", {}) or {}
                status  = entry.get("exit_status", "")
                ok      = status in ("completed", "success")
                motion  = _is_motion_only(name, kwargs)
                icon    = "✓" if ok else ("✗" if status else "?")
                if motion:
                    color = _NEUTRAL_COLOR
                else:
                    color = SUCCESS if ok else DANGER
                ts       = entry.get("timestamp", "")
                t_str    = ts[11:19] if len(ts) >= 19 else ts[:19]
                dur      = entry.get("duration_s")
                scan_num = entry.get("scan_num")
                summary  = self._plan_summary(name, kwargs, args)
                dur_str  = f"  ({dur:.1f}s)" if dur is not None else ""
                prefix   = f"#{scan_num:<3} " if scan_num is not None else "     "
                li = QListWidgetItem(
                    f"{prefix}{icon}  {t_str}  {name}{summary}{dur_str}")
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

            if t_stop and self._exp_created_at and t_stop < self._exp_created_at:
                self._logged_uids.add(uid)
                continue
            if t_stop and self._exp_end_time and t_stop >= self._exp_end_time:
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
                "args":        item.get("args", []) or [],
                "kwargs":      item.get("kwargs", {}) or {},
                "exit_status": exit_status,
                "duration_s":  round(dur, 2) if dur else None,
                "run_file":    "",
                "scan_num":    self._next_scan_num,
            }
            found = self._find_run_file_for_entry(tmp_entry)
            entry = {**tmp_entry, "run_file": str(found) if found else ""}
            try:
                with open(log_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                self._logged_uids.add(uid)
                self._next_scan_num += 1
                changed = True
            except Exception:
                pass

            # Show error dialog for newly failed plans
            if exit_status == "failed" and uid not in self._shown_error_uids:
                self._shown_error_uids.add(uid)
                err_msg = result.get("msg", "") or result.get("traceback", "") or "(no details)"
                QMessageBox.warning(
                    self, f"Plan Failed — {item.get('name', '?')}",
                    f"Plan  '{item.get('name', '?')}'  failed.\n\n{err_msg[:1000]}",
                )

        if changed:
            self._load_plan_log(self._active_exp_path)

    def update_compact_queue(self, items: list):
        self.queue_compact.clear()
        for i, item in enumerate(items):
            name    = item.get("name", "unknown")
            args    = item.get("args", []) or []
            kwargs  = item.get("kwargs", {}) or {}
            uid     = item.get("item_uid", "")
            summary = self._plan_summary(name, kwargs, args)
            li = QListWidgetItem(f"{i + 1}.  {name}{summary}")
            li.setData(Qt.ItemDataRole.UserRole,     uid)
            li.setData(Qt.ItemDataRole.UserRole + 1, item)
            self.queue_compact.addItem(li)
        n = len(items)
        self.queue_count_label.setText(f"{n} item{'s' if n != 1 else ''}")

    # ── Internal slots ─────────────────────────────────────────────────────────

    def _on_plan_log_selection_changed(self):
        """Called whenever selection changes in the plan log list."""
        selected = self.plan_log_list.selectedItems()
        if not selected:
            return

        entries = []
        for li in selected:
            entry = li.data(Qt.ItemDataRole.UserRole)
            if entry:
                entries.append(entry)

        # Skip motion-only plans (nothing to plot)
        plottable = [e for e in entries
                     if not _is_motion_only(e.get("name", ""), e.get("kwargs", {}) or {})]
        if not plottable:
            return

        paths = [self._find_run_file_for_entry(e) for e in plottable]
        paths = [p for p in paths if p]
        if not paths:
            return

        if len(paths) == 1:
            self.history_widget.load_jsonl_file(str(paths[0]))
        else:
            self.history_widget.load_jsonl_files(paths)
        self.plot_tabs.setCurrentIndex(1)

    def _on_plan_log_double_clicked(self, li: QListWidgetItem):
        """Double-click: open PlanDialog pre-populated so the user can edit & re-queue."""
        entry = li.data(Qt.ItemDataRole.UserRole)
        if not entry or not self.worker:
            return
        base = {
            "name":      entry.get("name", ""),
            "args":      entry.get("args", []) or [],
            "kwargs":    {k: v for k, v in (entry.get("kwargs", {}) or {}).items()
                         if k != "md"},
            "item_type": "plan",
        }
        dlg = PlanDialog(self._plans, self._devices, item=base, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            item = self._inject_metadata(dlg.result_item)
            ok, msg = self.worker.add_item(item)
            self._log(f"{'✓' if ok else '✗'} Re-queue '{base['name']}': {msg}")

    def _plan_log_context_menu(self, pos):
        li = self.plan_log_list.itemAt(pos)
        if not li:
            return
        entry = li.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        menu = QMenu(self)
        menu.addAction("Edit & Re-queue", lambda: self._on_plan_log_double_clicked(li))
        menu.addAction("View Details",    lambda: self._view_plan_detail(entry))
        menu.exec(self.plan_log_list.viewport().mapToGlobal(pos))

    def _view_plan_detail(self, entry: dict):
        ts_str = entry.get("timestamp", "")
        dur    = entry.get("duration_s")
        try:
            t_stop = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            t_stop = 0.0
        t_start = (t_stop - dur) if (t_stop and dur) else t_stop
        item = {
            "name":      entry.get("name", "?"),
            "args":      entry.get("args", []) or [],
            "kwargs":    entry.get("kwargs", {}) or {},
            "_run_file": entry.get("run_file", ""),
            "_scan_num": entry.get("scan_num"),
            "result": {
                "exit_status": entry.get("exit_status", "?"),
                "time_start":  t_start,
                "time_stop":   t_stop,
                "run_uids":    entry.get("run_uids", []),
            },
        }
        dlg = RunDetailDialog(item, worker=self.worker,
                              plans=self._plans, devices=self._devices, parent=self)
        dlg.exec()
