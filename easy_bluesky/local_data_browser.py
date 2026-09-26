"""Local Data Browser — read experiment JSONL run files without MongoDB/RE Manager.

Users at home institutions can open an experiment folder (received from the beamline)
and get the same 1D plot + curve-fit experience as the MongoDB Browser.
"""

import json
from pathlib import Path

import numpy as np

try:
    import pandas as pd
    _PANDAS_OK = True
except ImportError:
    _PANDAS_OK = False

try:
    import pyqtgraph as pg
    _PG_OK = True
except ImportError:
    _PG_OK = False

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QSizePolicy, QSplitter, QStackedWidget, QTabBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import peak_fit as _pf
from .curve_fit_dialog import FitParamsDialog
from .config import PLOT_COLORS
from .plot_tools import setup_crosshair, smart_legend_position, TwoDMapWidget


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def _parse_jsonl_run(path) -> dict:
    """Parse a bluesky suitcase JSONL run file → {field: numpy_array}.

    Each line is [doc_type, doc_body].  Collects values from 'event' and
    'event_page' documents.  Returns {} when no event data is present.
    """
    fields: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc_type, doc = json.loads(line)
                except Exception:
                    continue
                if doc_type == "event":
                    data = doc.get("data", {})
                    for k, v in data.items():
                        fields.setdefault(k, []).append(v)
                elif doc_type == "event_page":
                    data     = doc.get("data", {})
                    seq_nums = doc.get("seq_num", [])
                    n        = len(seq_nums) if seq_nums else 0
                    for k, v_list in data.items():
                        if isinstance(v_list, list):
                            fields.setdefault(k, []).extend(v_list)
                        else:
                            fields.setdefault(k, []).extend([v_list] * n)
    except Exception:
        pass

    if not fields:
        return {}

    result = {}
    for k, vals in fields.items():
        arr = []
        for v in vals:
            try:
                arr.append(float(v))
            except (TypeError, ValueError):
                arr.append(float("nan"))
        result[k] = np.array(arr)
    return result


def _poisson_sigma(y_raw, norm_raw=None):
    sigma = np.sqrt(np.abs(y_raw))
    if norm_raw is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            sigma = sigma / np.where(norm_raw != 0, norm_raw, np.nan)
    return sigma


# ── Background loader ─────────────────────────────────────────────────────────

class _RunLoader(QThread):
    """Load one or more JSONL run files in a background thread."""
    done  = pyqtSignal(list)   # list of (pd.DataFrame, label)
    error = pyqtSignal(str)

    def __init__(self, tasks, parent=None):
        """tasks: list of (jsonl_path, label)"""
        super().__init__(parent)
        self._tasks = tasks

    def run(self):
        try:
            result = []
            for path, label in self._tasks:
                data = _parse_jsonl_run(path)
                if data and _PANDAS_OK:
                    df = pd.DataFrame(data)
                    if not df.empty:
                        result.append((df, label))
            self.done.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Main widget ───────────────────────────────────────────────────────────────

class LocalDataBrowserTab(QWidget):
    """Offline experiment data browser — reads plans_log.jsonl + runs/*.jsonl."""

    COLORS = PLOT_COLORS
    sync_requested = pyqtSignal(list, str)   # (uid_list, runs_dir_path)

    def set_sync_message(self, msg: str):
        """Called by main.py to show SSH progress in this tab's status bar."""
        self._status_label.setText(msg)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exp_path   = ""
        self._entries    = []   # plans_log.jsonl rows (newest-first in table)
        self._dfs        = []   # [(pd.DataFrame, label), ...] currently shown
        self._curves:     dict = {}
        self._error_items:dict = {}
        self._fit_curves: dict = {}
        self._fit_texts:  list = []
        self._fit_preview_curve = None
        self._fit_dlg           = None
        self._saved_fit_state   = None
        self._loader            = None
        self._crosshair_cleanup = None
        self._map_mode          = False
        self._2d_map_data       = None
        self._build()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setSizes([320, 1080])
        lay.addWidget(splitter)

    def _build_left(self) -> QWidget:
        w    = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 4, 8)
        vlay.setSpacing(6)

        lbl = QLabel("LOCAL DATA BROWSER")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

        self._btn_open = QPushButton("Open Experiment Folder…")
        self._btn_open.setObjectName("btn_primary")
        self._btn_open.clicked.connect(self._open_folder)
        vlay.addWidget(self._btn_open)

        self._btn_sync = QPushButton("⟳ Fetch JSONL from Beamline")
        self._btn_sync.setToolTip(
            "Copy missing run JSONL files from the beamline computer\n"
            "via SSH — requires active connection to RE Manager"
        )
        self._btn_sync.clicked.connect(self._on_sync_clicked)
        vlay.addWidget(self._btn_sync)

        self._exp_label = QLabel("No folder open")
        self._exp_label.setObjectName("dim_text")
        self._exp_label.setStyleSheet("font-size: 11px; font-weight: bold;")
        self._exp_label.setWordWrap(True)
        vlay.addWidget(self._exp_label)

        hint = QLabel("SCANS  (click to plot · Shift/Ctrl to overlay)")
        hint.setObjectName("section_title")
        vlay.addWidget(hint)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search scans…")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._filter_table)
        vlay.addWidget(self._search)

        self._scan_table = QTableWidget()
        self._scan_table.setColumnCount(4)
        self._scan_table.setHorizontalHeaderLabels(["#", "Plan", "Date / Time", "Status"])
        hh = self._scan_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._scan_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._scan_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._scan_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._scan_table.verticalHeader().setVisible(False)
        self._scan_table.setSortingEnabled(False)
        self._scan_table.setAlternatingRowColors(True)
        self._scan_table.itemSelectionChanged.connect(
            self._on_scan_selection_changed)
        vlay.addWidget(self._scan_table, 1)

        self._status_label = QLabel("Open an experiment folder to begin")
        self._status_label.setObjectName("dim_text")
        self._status_label.setStyleSheet("font-size: 10px;")
        vlay.addWidget(self._status_label)

        return w

    def _build_right(self) -> QWidget:
        w    = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 8, 8, 8)
        vlay.setSpacing(6)

        # ── Top bar: X combo + run label + screenshot ─────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(2, 2, 2, 2)
        top_bar.setSpacing(4)
        top_bar.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(120)
        self.x_combo.setMaximumWidth(240)
        self.x_combo.setFixedHeight(26)
        self.x_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.x_combo.currentTextChanged.connect(self._replot)
        top_bar.addWidget(self.x_combo)
        top_bar.addSpacing(6)
        self.run_label = QLabel("")
        self.run_label.setObjectName("dim_text")
        self.run_label.setStyleSheet("font-size: 12px; padding: 0 4px;")
        top_bar.addWidget(self.run_label)
        top_bar.addStretch()
        btn_ss = QPushButton("📷")
        btn_ss.setFixedSize(28, 26)
        btn_ss.setToolTip("Copy plot to clipboard")
        btn_ss.clicked.connect(self._copy_screenshot)
        top_bar.addWidget(btn_ss)
        vlay.addLayout(top_bar)

        # ── Mode tabs: 1D Plot / 2D Map ────────────────────────────────────────
        self._mode_tabs = QTabBar()
        self._mode_tabs.setDocumentMode(True)

        # 1D controls bar (Norm, ±Errors, Fit)
        _tab_1d = QWidget()
        _1d_bar = QHBoxLayout(_tab_1d)
        _1d_bar.setContentsMargins(4, 2, 4, 2)
        _1d_bar.setSpacing(4)

        _1d_bar.addWidget(QLabel("Norm:"))
        self.norm_combo = QComboBox()
        self.norm_combo.setMinimumWidth(100)
        self.norm_combo.setMaximumWidth(220)
        self.norm_combo.setFixedHeight(26)
        self.norm_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.norm_combo.addItem("None", userData=None)
        self.norm_combo.currentIndexChanged.connect(self._replot)
        _1d_bar.addWidget(self.norm_combo)

        _1d_bar.addSpacing(6)
        self._err_cb = QCheckBox("± Errors")
        self._err_cb.setToolTip("Overlay Poisson √N error bars")
        self._err_cb.stateChanged.connect(self._replot)
        _1d_bar.addWidget(self._err_cb)

        _1d_bar.addSpacing(10)
        _1d_bar.addWidget(QLabel("Fit:"))

        self._fit_model_combo = QComboBox()
        self._fit_model_combo.setFixedHeight(26)
        self._fit_model_combo.setMinimumWidth(110)
        self._fit_model_combo.addItem("None")
        self._fit_model_combo.insertSeparator(self._fit_model_combo.count())
        for m in _pf.PEAK_MODELS:
            self._fit_model_combo.addItem(m)
        self._fit_model_combo.insertSeparator(self._fit_model_combo.count())
        for m in _pf.STEP_MODELS:
            self._fit_model_combo.addItem(m)
        self._fit_model_combo.setCurrentText(_pf.PEAK_MODELS[0])
        _1d_bar.addWidget(self._fit_model_combo)

        bg_lbl = QLabel("+ BG:")
        bg_lbl.setStyleSheet("font-size: 11px;")
        _1d_bar.addWidget(bg_lbl)
        self._fit_bg_combo = QComboBox()
        self._fit_bg_combo.setFixedHeight(26)
        self._fit_bg_combo.setMinimumWidth(80)
        self._fit_bg_combo.setMaximumWidth(110)
        self._fit_bg_combo.addItems(_pf.BACKGROUND_MODELS)
        _1d_bar.addWidget(self._fit_bg_combo)

        btn_fit = QPushButton("Fit…")
        btn_fit.setObjectName("btn_primary")
        btn_fit.setFixedHeight(26)
        btn_fit.clicked.connect(self._open_fit_dialog)
        _1d_bar.addWidget(btn_fit)

        btn_clear = QPushButton("✕")
        btn_clear.setFixedSize(28, 26)
        btn_clear.setToolTip("Clear fit overlays")
        btn_clear.clicked.connect(self._clear_fit_overlays)
        _1d_bar.addWidget(btn_clear)
        _1d_bar.addStretch()

        self._mode_tabs.addTab("1D Plot")
        self._mode_tabs.addTab("2D Map")
        self._mode_tabs.currentChanged.connect(self._on_mode_tab_changed)
        vlay.addWidget(self._mode_tabs)
        self._1d_controls = _tab_1d
        vlay.addWidget(self._1d_controls)

        # ── Y signal list on right side of plot ───────────────────────────────
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.y_list.setMinimumWidth(100)
        self.y_list.itemSelectionChanged.connect(self._replot)

        y_lbl = QLabel("Y signals")
        y_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        y_lbl.setObjectName("dim_text")
        y_container = QWidget()
        y_layout = QVBoxLayout(y_container)
        y_layout.setSpacing(2)
        y_layout.setContentsMargins(4, 0, 0, 0)
        y_layout.addWidget(y_lbl)
        y_layout.addWidget(self.y_list, 1)

        # coord label created before crosshair setup
        self._coord_label = QLabel("")
        self._coord_label.setObjectName("dim_text")
        self._coord_label.setStyleSheet(
            "font-size: 11px; padding: 4px; font-family: Menlo, Consolas, Monaco, 'Courier New';")

        if _PG_OK:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            plot_area = self.plot_widget
        else:
            self.plot_widget = QLabel(
                "pyqtgraph not installed — pip install pyqtgraph")
            self.plot_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            plot_area = self.plot_widget

        plot_splitter = QSplitter(Qt.Orientation.Horizontal)
        plot_splitter.addWidget(plot_area)
        plot_splitter.addWidget(y_container)
        plot_splitter.setSizes([880, 180])
        plot_splitter.setStretchFactor(0, 1)
        plot_splitter.setStretchFactor(1, 0)

        self._2d_widget = TwoDMapWidget(parent=w)
        self._2d_widget.selection_changed.connect(self._update_2d_plot)

        self._btn_save_2d = QPushButton("Save 2D…")
        self._btn_save_2d.setVisible(False)
        self._btn_save_2d.setToolTip("Save multi-scan 2D map as CSV")
        self._btn_save_2d.clicked.connect(self._save_2d_map)
        self._2d_widget.add_to_ctrl_row(self._btn_save_2d)

        self._plot_stack = QStackedWidget()
        self._plot_stack.addWidget(plot_splitter)
        self._plot_stack.addWidget(self._2d_widget)
        vlay.addWidget(self._plot_stack, 1)

        # ── Bottom bar: coord + stats + Log Y + Deriv ─────────────────────────
        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(6)
        bot.addWidget(self._coord_label, 1)

        self._log_y_cb = QCheckBox("Log Y")
        self._log_y_cb.stateChanged.connect(self._replot)
        bot.addWidget(self._log_y_cb)

        bot.addWidget(QLabel("Deriv:"))
        self._deriv_combo = QComboBox()
        self._deriv_combo.addItems(["—", "dy/dx", "d²y/dx²"])
        self._deriv_combo.setFixedHeight(22)
        self._deriv_combo.setFixedWidth(90)
        self._deriv_combo.currentIndexChanged.connect(self._replot)
        bot.addWidget(self._deriv_combo)

        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim_text")
        self.stats_label.setStyleSheet("font-size: 10px;")
        bot.addWidget(self.stats_label)
        vlay.addLayout(bot)

        if _PG_OK:
            self._crosshair_cleanup = setup_crosshair(
                self.plot_widget, self._coord_label,
                get_curves_fn=lambda: self._curves,
            )

        return w

    # ── Folder opening ────────────────────────────────────────────────────────

    def _open_folder(self):
        start = self._exp_path or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Open Experiment Folder", start)
        if folder:
            self._load_folder(folder)

    def open_folder(self, folder: str):
        """Public entry point — called from main.py when experiment changes."""
        if folder and Path(folder).is_dir():
            self._load_folder(folder)

    def _missing_uids(self) -> list:
        """Return UIDs from plans_log that have no local JSONL file."""
        missing = []
        for entry in self._entries:
            uids = entry.get("run_uids", [])
            uid  = uids[0] if uids else ""
            if uid:
                p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                if not p.exists():
                    missing.append(uid)
        return missing

    def _auto_sync(self):
        """Emit sync_requested for any UIDs with no local JSONL file."""
        if not self._exp_path:
            return
        missing = self._missing_uids()
        n_all   = sum(1 for e in self._entries if (e.get("run_uids") or [""])[0])
        n_local = n_all - len(missing)
        self._status_label.setText(
            f"{len(self._entries)} scans  ·  "
            f"{n_local}/{n_all} run files local"
            + (f"  ·  fetching {len(missing)} from beamline…" if missing else "")
        )
        if missing:
            runs_dir = str(Path(self._exp_path) / "runs")
            self.sync_requested.emit(missing, runs_dir)

    def _on_sync_clicked(self):
        """Manual re-fetch button — re-checks for missing files and emits sync_requested."""
        if not self._exp_path:
            self._status_label.setText("Open an experiment folder first")
            return
        missing = self._missing_uids()
        if not missing:
            self._status_label.setText("All run files already present locally — nothing to fetch")
            return
        runs_dir = str(Path(self._exp_path) / "runs")
        self._status_label.setText(f"Fetching {len(missing)} JSONL file(s) from beamline…")
        self.sync_requested.emit(missing, runs_dir)

    def on_sync_done(self, n_copied: int, n_total: int):
        """Called by main.py after SSH fetch completes."""
        if n_copied > 0:
            self._status_label.setText(
                f"Fetched {n_copied}/{n_total} JSONL file(s) from beamline"
            )
            # Reload table entries (plans_log unchanged, but files now exist locally)
            if self._exp_path:
                self._reload_after_sync()
        else:
            missing_still = self._missing_uids()
            if missing_still:
                self._status_label.setText(
                    f"Could not fetch JSONL files — "
                    f"check RE Console for details  "
                    f"({n_total} file(s) not found in beamline fallback directory)"
                )
            else:
                self._status_label.setText(
                    f"{len(self._entries)} scans loaded  ·  all run files present locally"
                )

    def _reload_after_sync(self):
        """Reload plan table and re-trigger any current scan selection."""
        # Remember which scan rows are currently selected
        selected_rows = sorted({idx.row() for idx in self._scan_table.selectedIndexes()})
        # Reload so file-existence checks reflect newly copied files
        log_file = Path(self._exp_path) / "plans_log.jsonl"
        if not log_file.exists():
            return
        entries = []
        try:
            with open(log_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            return
        self._entries = entries
        self._populate_table(entries)
        # Restore selection
        if selected_rows:
            self._scan_table.blockSignals(True)
            for row in selected_rows:
                if row < self._scan_table.rowCount():
                    self._scan_table.selectRow(row)
            self._scan_table.blockSignals(False)
            self._on_scan_selection_changed()

    def _load_folder(self, folder: str):
        self._exp_path = folder
        self._exp_label.setText(Path(folder).name)

        log_file = Path(folder) / "plans_log.jsonl"
        if not log_file.exists():
            QMessageBox.warning(
                self, "No Plan Log",
                f"plans_log.jsonl not found in:\n{folder}\n\n"
                "Make sure this is a valid EasyBluesky experiment folder.",
            )
            self._entries = []
            self._scan_table.setRowCount(0)
            self._status_label.setText("No plan log found")
            return

        entries = []
        try:
            with open(log_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as exc:
            QMessageBox.critical(self, "Read Error", str(exc))
            return

        self._entries = entries
        self._populate_table(entries)
        self._dfs = []
        self._clear_plot()
        self._auto_sync()

    # ── Scan table ────────────────────────────────────────────────────────────

    def _populate_table(self, entries):
        self._scan_table.setRowCount(0)

        def _ro(text, align=Qt.AlignmentFlag.AlignLeft):
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            it.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            return it

        STATUS_COLOR = {
            "success":  "#2ca02c",
            "fail":     "#d62728",
            "abort":    "#e88a00",
            "running":  "#1f77b4",
        }

        for entry in reversed(entries):   # newest first
            sn     = entry.get("scan_num", "")
            name   = entry.get("name", "?")
            ts     = entry.get("timestamp", "")
            if ts and "T" in ts:
                ts = ts[:16].replace("T", "  ")
            status = entry.get("exit_status", "")

            row = self._scan_table.rowCount()
            self._scan_table.insertRow(row)

            sn_it = _ro(str(sn) if sn != "" else "?",
                        Qt.AlignmentFlag.AlignRight)
            sn_it.setData(Qt.ItemDataRole.UserRole, entry)
            self._scan_table.setItem(row, 0, sn_it)
            self._scan_table.setItem(row, 1, _ro(name))
            self._scan_table.setItem(row, 2, _ro(ts))

            st_it = _ro(status)
            if status in STATUS_COLOR:
                st_it.setForeground(QColor(STATUS_COLOR[status]))
            self._scan_table.setItem(row, 3, st_it)

        self._scan_table.resizeColumnsToContents()

    def _filter_table(self, text: str):
        text = text.lower()
        for row in range(self._scan_table.rowCount()):
            visible = (
                not text
                or any(
                    text in (self._scan_table.item(row, c).text() or "").lower()
                    for c in range(4)
                )
            )
            self._scan_table.setRowHidden(row, not visible)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _on_scan_selection_changed(self):
        rows = sorted({idx.row() for idx in self._scan_table.selectedIndexes()})
        if not rows:
            return

        tasks        = []
        labels       = []
        missing_files = []
        no_uid_count  = 0
        for row in rows:
            if self._scan_table.isRowHidden(row):
                continue
            entry = self._scan_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if entry is None:
                continue
            uids = entry.get("run_uids", [])
            uid  = uids[0] if uids else ""
            sn   = entry.get("scan_num", "?")
            label = f"#{sn}"
            if not uid:
                no_uid_count += 1
                continue
            if self._exp_path:
                p = Path(self._exp_path) / "runs" / f"{uid}.jsonl"
                if p.exists():
                    tasks.append((str(p), label))
                    labels.append(label)
                else:
                    missing_files.append(p.name)

        if not tasks:
            if missing_files:
                self._status_label.setText(
                    f"Run file(s) not found in {Path(self._exp_path).name}/runs/  "
                    f"— data may be on the beamline computer"
                )
            elif no_uid_count:
                self._status_label.setText("Selected scan(s) have no bluesky run data (motion-only plans)")
            else:
                self._status_label.setText("No run data found for selected scan(s)")
            return

        # Cancel any running loader
        if self._loader and self._loader.isRunning():
            self._loader.done.disconnect()
            self._loader.error.disconnect()

        self._status_label.setText(f"Loading {len(tasks)} scan(s)…")
        self._loader = _RunLoader(tasks, parent=self)
        self._loader.done.connect(self._on_load_done)
        self._loader.error.connect(self._on_load_error)
        self._loader.start()

    def _on_load_done(self, dfs):
        n = len(dfs)
        self._dfs = dfs
        if n == 0:
            self._status_label.setText("No plottable data in selected scan(s)")
            self._clear_plot()
            return
        self._status_label.setText(
            f"{n} scan(s) loaded — "
            f"{', '.join(lbl for _, lbl in dfs)}"
        )
        if dfs:
            self._update_field_combos(dfs[0][0])
        if self._map_mode:
            self._update_2d_plot()
        else:
            self._replot()

    def _on_load_error(self, msg: str):
        self._status_label.setText(f"Load error: {msg}")

    # ── Mode tab / 2D map ─────────────────────────────────────────────────────

    def _on_mode_tab_changed(self, idx: int):
        self._1d_controls.setVisible(idx == 0)
        self._toggle_map_mode(idx == 1)

    def _toggle_map_mode(self, checked: bool):
        self._map_mode = checked
        self._plot_stack.setCurrentIndex(1 if checked else 0)
        if checked:
            self._update_2d_plot()
        else:
            self._btn_save_2d.setVisible(False)
            self._2d_map_data = None
            self._replot()

    def _update_2d_plot(self):
        if not self._dfs:
            return
        if len(self._dfs) >= 2:
            self._update_2d_map_multi_scan()
        else:
            self._btn_save_2d.setVisible(False)
            df, _  = self._dfs[0]
            x_col  = self.x_combo.currentText()
            y_col  = self._2d_widget.get_y_signal()
            z_col  = self._2d_widget.get_z_signal()
            if not all(c in df.columns for c in (x_col, y_col, z_col)):
                return
            self._2d_widget.replot(
                df[x_col].values, df[y_col].values, df[z_col].values,
                x_col, y_col, z_col,
            )

    def _update_2d_map_multi_scan(self):
        x_col = self.x_combo.currentText()
        y_col = self._2d_widget.get_y_signal()
        z_col = self._2d_widget.get_z_signal()
        if not x_col or not z_col:
            return

        eligible = [(df, lbl) for df, lbl in self._dfs
                    if x_col in df.columns and z_col in df.columns
                    and (not y_col or y_col in df.columns)]
        if len(eligible) < 2:
            self._mode_tabs.setCurrentIndex(0)
            QMessageBox.warning(
                self, "2D Map",
                f"Fewer than 2 scans have '{x_col}' and '{z_col}' — "
                "2D map requires at least 2 compatible scans."
            )
            return

        xs_list, ys_list, zs_list = [], [], []
        for i, (df, _lbl) in enumerate(eligible):
            x_arr = df[x_col].values.astype(float)
            z_arr = df[z_col].values.astype(float)
            y_arr = (df[y_col].values.astype(float) if y_col
                     else np.full(len(x_arr), float(i)))
            n = min(len(x_arr), len(y_arr), len(z_arr))
            xs_list.append(x_arr[:n])
            ys_list.append(y_arr[:n])
            zs_list.append(z_arr[:n])

        xs = np.concatenate(xs_list)
        ys = np.concatenate(ys_list)
        zs = np.concatenate(zs_list)

        scan_labels = [lbl for _, lbl in eligible]
        self._2d_map_data = (xs, ys, zs, x_col, y_col or "scan index", z_col, scan_labels)
        self._2d_widget.replot(xs, ys, zs,
                               x_label=x_col,
                               y_label=y_col or "scan index",
                               z_label=z_col)
        self._btn_save_2d.setVisible(True)

    def _save_2d_map(self):
        if not self._2d_map_data:
            return
        xs, ys, zs, x_col, y_col, z_col, _labels = self._2d_map_data
        path, _ = QFileDialog.getSaveFileName(
            self, "Save 2D Map", "", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w") as fh:
                fh.write(f"{x_col},{y_col},{z_col}\n")
                for x, y, z in zip(xs, ys, zs):
                    fh.write(f"{x},{y},{z}\n")
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", str(exc))

    # ── Field combos ─────────────────────────────────────────────────────────

    def _update_field_combos(self, df):
        cols = sorted(df.columns.tolist())

        prev_x    = self.x_combo.currentText()
        prev_ys   = {self.y_list.item(i).text()
                     for i in range(self.y_list.count())
                     if self.y_list.item(i).isSelected()}
        prev_norm = self.norm_combo.currentData()

        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.addItems(cols)
        if prev_x in cols:
            self.x_combo.setCurrentText(prev_x)
        else:
            for pref in ("time", "motor", "energy", "x"):
                match = next((c for c in cols if pref in c.lower()), None)
                if match:
                    self.x_combo.setCurrentText(match)
                    break
        self.x_combo.blockSignals(False)

        self.y_list.blockSignals(True)
        self.y_list.clear()
        for c in cols:
            self.y_list.addItem(c)
        for i in range(self.y_list.count()):
            if self.y_list.item(i).text() in prev_ys:
                self.y_list.item(i).setSelected(True)
        if not any(self.y_list.item(i).isSelected()
                   for i in range(self.y_list.count())):
            x_name = self.x_combo.currentText()
            for i in range(self.y_list.count()):
                name = self.y_list.item(i).text()
                if name != x_name and not name.endswith(("_setpoint", "_user_setpoint")):
                    self.y_list.item(i).setSelected(True)
                    break
        self.y_list.blockSignals(False)

        self.norm_combo.blockSignals(True)
        self.norm_combo.clear()
        self.norm_combo.addItem("None", userData=None)
        for c in cols:
            self.norm_combo.addItem(c, userData=c)
        if prev_norm and prev_norm in cols:
            self.norm_combo.setCurrentText(prev_norm)
        self.norm_combo.blockSignals(False)

        x_col = self.x_combo.currentText()
        self._2d_widget.set_columns(cols, x_col, [], cols)

    # ── Plotting ──────────────────────────────────────────────────────────────

    def _clear_plot(self):
        if not _PG_OK:
            return
        self._clear_fit_overlays()
        self._clear_fit_preview()
        for item in list(self._error_items.values()) + list(self._curves.values()):
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        pi = self.plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves      = {}
        self._error_items = {}
        self.stats_label.setText("")
        self.run_label.setText("")

    def _replot(self):
        if not _PG_OK or not self._dfs:
            return

        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return

        self._clear_plot()

        norm_col  = self.norm_combo.currentData()
        show_err  = self._err_cb.isChecked()
        color_idx = 0
        stats     = []

        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x        = df[xc].values.astype(float)
            norm_raw = (df[norm_col].values.astype(float)
                        if norm_col and norm_col in df.columns else None)

            for yc in ycs:
                if yc not in df.columns:
                    continue
                y_raw = df[yc].values.astype(float)

                y = y_raw.copy()
                if norm_raw is not None:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(norm_raw != 0, y / norm_raw, np.nan)

                sigma = _poisson_sigma(y_raw, norm_raw)

                deriv_mode = self._deriv_combo.currentIndex()
                if deriv_mode > 0:
                    _, y, sigma = self._apply_deriv(x, y, sigma, order=deriv_mode)
                if self._log_y_cb.isChecked():
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.log10(np.where(y > 0, y, np.nan))

                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_, s_ = x[mask], y[mask], sigma[mask]
                if not len(x_):
                    continue

                color      = self.COLORS[color_idx % len(self.COLORS)]
                pen        = pg.mkPen(color=color, width=2)
                base_name  = yc if not norm_col else f"{yc}/{norm_col}"
                curve_name = (base_name if len(self._dfs) == 1
                              else f"{base_name}  [{df_label}]")
                curve = self.plot_widget.plot(
                    x_, y_, pen=pen, name=curve_name,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
                self._curves[curve_name] = curve

                if show_err and np.any(np.isfinite(s_)):
                    err_item = pg.ErrorBarItem(
                        x=x_, y=y_, height=2 * s_,
                        beam=0.0, pen=pg.mkPen(color=color, width=1),
                    )
                    self.plot_widget.addItem(err_item)
                    self._error_items[curve_name] = err_item

                color_idx += 1
                stats.append(
                    f"{curve_name}: min={y_.min():.4g}  max={y_.max():.4g}")

        self.plot_widget.setLabel("bottom", xc)
        y_label = ", ".join(ycs)
        if norm_col:
            y_label += f"  /  {norm_col}"
        deriv_mode = self._deriv_combo.currentIndex()
        if deriv_mode == 1:
            y_label += "  [dy/dx]"
        elif deriv_mode == 2:
            y_label += "  [d²y/dx²]"
        if self._log_y_cb.isChecked():
            y_label = f"log₁₀({y_label})"
        self.plot_widget.setLabel("left", y_label)
        self.stats_label.setText("   ".join(stats))
        if self._dfs:
            self.run_label.setText(", ".join(lbl for _, lbl in self._dfs))
        smart_legend_position(self.plot_widget)

    @staticmethod
    def _apply_deriv(x, y, sigma=None, order=1):
        """Central-difference derivative, same implementation as HDF5Viewer."""
        finite = np.isfinite(x) & np.isfinite(y)
        if finite.sum() < 3:
            return x, y, (sigma if sigma is not None else np.zeros_like(y))
        for _ in range(order):
            dy = np.gradient(y, x)
            if sigma is not None:
                n = len(x)
                s = np.zeros(n)
                for i in range(n):
                    il = max(0, i - 1)
                    ir = min(n - 1, i + 1)
                    dx = x[ir] - x[il]
                    if dx != 0:
                        s[i] = np.sqrt(sigma[il]**2 + sigma[ir]**2) / abs(dx)
                sigma = s
            y = dy
        return x, y, (sigma if sigma is not None else np.zeros_like(y))

    # ── Fit overlays ──────────────────────────────────────────────────────────

    def _clear_fit_overlays(self):
        if not _PG_OK:
            return
        for item in self._fit_texts:
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        for curve in self._fit_curves.values():
            try:
                self.plot_widget.removeItem(curve)
            except Exception:
                pass
        self._fit_texts  = []
        self._fit_curves = {}

    def _clear_fit_preview(self):
        if self._fit_preview_curve is not None:
            try:
                self.plot_widget.removeItem(self._fit_preview_curve)
            except Exception:
                pass
            self._fit_preview_curve = None

    def _on_fit_preview(self, x_fit, y_fit):
        if not _PG_OK:
            return
        try:
            pen = pg.mkPen("#ffcc44", width=2, style=Qt.PenStyle.DotLine)
            if self._fit_preview_curve is None:
                self._fit_preview_curve = self.plot_widget.plot(
                    x_fit, y_fit, pen=pen)
            else:
                self._fit_preview_curve.setData(x_fit, y_fit)
        except Exception:
            pass

    def _on_fit_applied(self, fit_items):
        self._clear_fit_preview()
        self._clear_fit_overlays()
        fit_colors = ["#ff6688", "#66ffaa", "#ffaa33", "#33aaff", "#cc88ff"]
        for idx, item in enumerate(fit_items):
            color = fit_colors[idx % len(fit_colors)]
            x_fit = item.get("x_fit")
            y_fit = item.get("y_fit")
            info  = item.get("info", {})
            label = item.get("label", "fit")
            if x_fit is None or y_fit is None:
                continue
            pen  = pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine)
            key  = f"fit:{label}"
            self._fit_curves[key] = self.plot_widget.plot(
                x_fit, y_fit, pen=pen, name=key)
            x0 = info.get("x0", float("nan"))
            if np.isfinite(x0):
                vline = pg.InfiniteLine(
                    pos=float(x0), angle=90,
                    pen=pg.mkPen(color=color, width=1,
                                 style=Qt.PenStyle.DashLine),
                )
                self.plot_widget.addItem(vline)
                self._fit_texts.append(vline)
        if fit_items:
            info0 = fit_items[0].get("info", {})
            result_obj = info0.get("result")
            self._saved_fit_state = {
                "model_name": fit_items[0].get("model_name", ""),
                "bg_name":    fit_items[0].get("bg_name", "None"),
                "params":     result_obj.params if result_obj else None,
            }

    def _on_fit_cancelled(self):
        self._clear_fit_preview()

    def _open_fit_dialog(self):
        if not _pf.LMFIT_AVAILABLE:
            QMessageBox.warning(self, "lmfit not installed",
                                "pip install lmfit")
            return
        if not self._dfs:
            QMessageBox.warning(self, "No data",
                                "Select at least one scan first.")
            return

        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            QMessageBox.warning(self, "No fields selected",
                                "Select X and at least one Y field.")
            return

        if self._fit_dlg is not None:
            try:
                self._fit_dlg.close()
            except Exception:
                pass

        initial_model   = self._fit_model_combo.currentText()
        initial_bg_name = self._fit_bg_combo.currentText()
        initial_params  = None
        if self._saved_fit_state:
            initial_model   = self._saved_fit_state.get("model_name", initial_model)
            initial_bg_name = self._saved_fit_state.get("bg_name", initial_bg_name)
            initial_params  = self._saved_fit_state.get("params")

        norm_col   = self.norm_combo.currentData()
        deriv_mode = self._deriv_combo.currentIndex()
        log_y      = self._log_y_cb.isChecked()

        datasets = []
        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x        = df[xc].values.astype(float)
            norm_raw = (df[norm_col].values.astype(float)
                        if norm_col and norm_col in df.columns else None)
            for yc in ycs:
                if yc not in df.columns:
                    continue
                y_raw = df[yc].values.astype(float)
                y = y_raw.copy()
                if norm_raw is not None:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(norm_raw != 0, y / norm_raw, np.nan)
                if deriv_mode > 0:
                    sigma = _poisson_sigma(y_raw, norm_raw)
                    _, y, _ = self._apply_deriv(x, y, sigma, order=deriv_mode)
                if log_y:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.log10(np.where(y > 0, y, np.nan))
                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_ = x[mask], y[mask]
                if len(x_) < 4:
                    continue
                lbl = yc if not norm_col else f"{yc}/{norm_col}"
                if len(self._dfs) > 1:
                    lbl = f"{lbl}  [{df_label}]"
                datasets.append((x_, y_, lbl))

        if not datasets:
            QMessageBox.warning(self, "No data",
                                "No plottable data for the selected fields.")
            return

        self._fit_dlg = FitParamsDialog(
            datasets, initial_model, initial_bg_name, initial_params,
            parent=self,
        )
        self._fit_dlg.preview_changed.connect(self._on_fit_preview)
        self._fit_dlg.fit_applied.connect(self._on_fit_applied)
        self._fit_dlg.show()

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _copy_screenshot(self):
        if _PG_OK:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setPixmap(self.plot_widget.grab())

    def closeEvent(self, event):
        if self._crosshair_cleanup:
            self._crosshair_cleanup()
        super().closeEvent(event)
