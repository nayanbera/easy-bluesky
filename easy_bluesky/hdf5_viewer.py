"""hdf5_viewer.py — Dedicated tab for browsing HDF5 experiment archives."""

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
    QListWidget, QListWidgetItem, QAbstractItemView, QComboBox, QFileDialog,
    QDialog, QPlainTextEdit, QDialogButtonBox, QMessageBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont

from .config import SUCCESS, DANGER, PLOT_COLORS
from .plot_tools import setup_crosshair

_MOTION_PLANS = frozenset({
    "mv", "mvr", "abs_set", "rel_set", "move", "sleep", "rd", "set",
    "kickoff", "complete", "collect", "null",
})
_NEUTRAL_COLOR = "#aaaaaa"


# ── Scan detail dialog ─────────────────────────────────────────────────────────

class ScanDetailDialog(QDialog):
    def __init__(self, attrs: dict, parent=None):
        super().__init__(parent)
        scan_num  = attrs.get("scan_num", "?")
        plan_name = attrs.get("plan_name", "?")
        self.setWindowTitle(f"Scan #{scan_num} — {plan_name}")
        self.setMinimumSize(520, 400)
        self._attrs = attrs
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Courier New", 11))
        lay.addWidget(self.text)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)
        self._populate()

    def _populate(self):
        a = self._attrs
        lines = []
        if a.get("scan_num") is not None:
            lines.append(f"Scan #:    {a['scan_num']}")
        lines += [
            f"Plan:      {a.get('plan_name', '?')}",
            f"Status:    {a.get('exit_status', '?')}",
            f"Time:      {str(a.get('timestamp', '?'))[:19]}",
        ]
        if a.get("duration_s") is not None:
            lines.append(f"Duration:  {float(a['duration_s']):.2f} s")
        if a.get("n_events") is not None:
            lines.append(f"Events:    {a['n_events']}")

        sample = str(a.get("sample_name", ""))
        desc   = str(a.get("sample_description", ""))
        exp    = str(a.get("exp_dir", ""))
        if sample or desc or exp:
            lines += ["", "── Experiment / Sample ──────────────────────"]
            if sample:
                lines.append(f"  sample_name:        {sample}")
            if desc:
                lines.append(f"  sample_description: {desc}")
            if exp:
                short = exp if len(exp) <= 72 else "…" + exp[-71:]
                lines.append(f"  exp_dir:            {short}")

        dets = str(a.get("detectors", ""))
        if dets:
            lines += ["", "── Detectors ────────────────────────────────"]
            lines.append(f"  {dets}")

        motor = str(a.get("motor", ""))
        if motor:
            lines += ["", "── Motor ────────────────────────────────────"]
            lines.append(f"  motor: {motor}")

        self.text.setPlainText("\n".join(lines))


# ── Main HDF5 Viewer widget ────────────────────────────────────────────────────

class HDF5Viewer(QWidget):
    COLORS = PLOT_COLORS

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scans:  list = []   # list of {"attrs": dict, "df": DataFrame|None}
        self._dfs:    list = []   # [(df, label)] for current selection
        self._curves: dict = {}
        self._crosshair_cleanup = None
        self._build()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setSizes([280, 920])
        lay.addWidget(splitter)

    def _build_left(self) -> QWidget:
        w    = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 4, 8)
        vlay.setSpacing(6)

        lbl = QLabel("HDF5 ARCHIVE")
        lbl.setObjectName("section_title")
        vlay.addWidget(lbl)

        btn_open = QPushButton("Open HDF5 File…")
        btn_open.setObjectName("btn_primary")
        btn_open.clicked.connect(self._open_file)
        if not H5PY_AVAILABLE:
            btn_open.setEnabled(False)
            btn_open.setToolTip("pip install h5py")
        vlay.addWidget(btn_open)

        self.file_label = QLabel("No file open")
        self.file_label.setObjectName("dim_text")
        self.file_label.setStyleSheet("font-size: 11px; font-weight: bold;")
        self.file_label.setWordWrap(True)
        vlay.addWidget(self.file_label)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("dim_text")
        self.meta_label.setStyleSheet("font-size: 10px;")
        self.meta_label.setWordWrap(True)
        vlay.addWidget(self.meta_label)

        lbl2 = QLabel("SCANS  (click to plot · multi-select to overlay)")
        lbl2.setObjectName("section_title")
        vlay.addWidget(lbl2)

        self.scan_list = QListWidget()
        self.scan_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.scan_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.scan_list.itemDoubleClicked.connect(self._on_double_clicked)
        vlay.addWidget(self.scan_list, 1)

        return w

    def _build_right(self) -> QWidget:
        w    = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(4, 8, 8, 8)
        vlay.setSpacing(6)

        # Axis controls
        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(130)
        self.x_combo.currentTextChanged.connect(self._replot)
        axis_row.addWidget(self.x_combo)

        axis_row.addWidget(QLabel("Y:"))
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMaximumHeight(56)
        self.y_list.setMaximumWidth(220)
        self.y_list.itemSelectionChanged.connect(self._replot)
        axis_row.addWidget(self.y_list)

        btn_plot = QPushButton("Plot")
        btn_plot.setObjectName("btn_primary")
        btn_plot.clicked.connect(self._replot)
        axis_row.addWidget(btn_plot)

        self.run_label = QLabel("")
        self.run_label.setObjectName("dim_text")
        self.run_label.setStyleSheet("font-size: 12px; padding: 0 8px;")
        axis_row.addWidget(self.run_label)
        axis_row.addStretch()
        vlay.addLayout(axis_row)

        if PG_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            vlay.addWidget(self.plot_widget, 1)
        else:
            vlay.addWidget(QLabel("pyqtgraph not available — pip install pyqtgraph"), 1)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim_text")
        bot.addWidget(self.stats_label, 1)
        self.coord_label = QLabel("")
        self.coord_label.setObjectName("dim_text")
        self.coord_label.setStyleSheet(
            "font-size: 11px; padding: 4px; font-family: monospace;")
        bot.addWidget(self.coord_label)
        vlay.addLayout(bot)

        if PG_AVAILABLE:
            self._crosshair_cleanup = setup_crosshair(
                self.plot_widget, self.coord_label, lambda: self._curves
            )

        return w

    # ── File loading ───────────────────────────────────────────────────────────

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open HDF5 Archive", "", "HDF5 Files (*.h5 *.hdf5)"
        )
        if path:
            self.load_file(path)

    def load_file(self, filepath: str):
        if not H5PY_AVAILABLE:
            self.file_label.setText("h5py not installed — pip install h5py")
            return
        try:
            import pandas as pd
            self._scans = []

            with h5py.File(filepath, "r") as hf:
                meta_attrs = dict(hf["metadata"].attrs) if "metadata" in hf else {}

                scan_keys = sorted(
                    [k for k in hf.keys() if k.startswith("scan_")],
                    key=lambda k: int(hf[k].attrs.get("scan_num", 0))
                )
                for key in scan_keys:
                    grp   = hf[key]
                    attrs = dict(grp.attrs)
                    cols  = {k: grp[k][:] for k in grp.keys()
                             if isinstance(grp[k], h5py.Dataset)}
                    df = pd.DataFrame(cols) if cols else None
                    self._scans.append({"attrs": attrs, "df": df})

            # Update info labels
            self.file_label.setText(Path(filepath).name)
            meta_lines = []
            if meta_attrs.get("experiment_name"):
                meta_lines.append(f"Experiment: {meta_attrs['experiment_name']}")
            if meta_attrs.get("sample_name"):
                meta_lines.append(f"Sample: {meta_attrs['sample_name']}")
            if meta_attrs.get("sample_description"):
                meta_lines.append(f"Desc: {meta_attrs['sample_description']}")
            n_data = sum(1 for s in self._scans if s["df"] is not None)
            meta_lines.append(
                f"{len(self._scans)} scans total  ({n_data} with data)")
            self.meta_label.setText("\n".join(meta_lines))

            self._populate_scan_list()

        except Exception as e:
            self.file_label.setText(f"Error: {e}")
            QMessageBox.critical(self, "HDF5 Error", str(e))

    # ── Scan list ──────────────────────────────────────────────────────────────

    def _populate_scan_list(self):
        self.scan_list.clear()
        for scan in self._scans:
            attrs     = scan["attrs"]
            scan_num  = attrs.get("scan_num", "?")
            name      = str(attrs.get("plan_name", "?"))
            ts        = str(attrs.get("timestamp", ""))
            t_str     = ts[11:19] if len(ts) >= 19 else ts
            status    = str(attrs.get("exit_status", ""))
            ok        = status in ("completed", "success")
            has_data  = scan["df"] is not None
            motion    = name.lower() in _MOTION_PLANS
            icon      = "✓" if ok else ("✗" if status else "?")
            color     = _NEUTRAL_COLOR if (motion or not has_data) \
                        else (SUCCESS if ok else DANGER)

            motor = str(attrs.get("motor", ""))
            dets  = str(attrs.get("detectors", ""))
            dur   = attrs.get("duration_s")

            parts = []
            if motor:
                parts.append(f"mot:{motor}")
            if dets:
                parts.append(f"det:{dets}")
            summary = "  " + "  ".join(parts) if parts else ""
            dur_str = f"  ({float(dur):.1f}s)" if dur is not None else ""
            label   = f"#{scan_num:<3} {icon}  {t_str}  {name}{summary}{dur_str}"

            li = QListWidgetItem(label)
            li.setForeground(QColor(color))
            li.setData(Qt.ItemDataRole.UserRole, scan)
            self.scan_list.addItem(li)

    # ── Selection → plot ───────────────────────────────────────────────────────

    def _on_selection_changed(self):
        selected = self.scan_list.selectedItems()
        if not selected:
            return

        plottable = [
            li.data(Qt.ItemDataRole.UserRole)
            for li in selected
            if li.data(Qt.ItemDataRole.UserRole) and
               li.data(Qt.ItemDataRole.UserRole)["df"] is not None
        ]
        if not plottable:
            self.run_label.setText("No data in selected scan(s)")
            return

        self._dfs = [
            (s["df"],
             f"#{s['attrs'].get('scan_num','?')} {s['attrs'].get('plan_name','?')}")
            for s in plottable
        ]
        self._setup_axes()

    def _setup_axes(self):
        if not self._dfs:
            return

        def numeric_cols(df):
            return [c for c in df.columns if df[c].dtype.kind in ("f", "i", "u")]

        col_sets = [set(numeric_cols(df)) for df, _ in self._dfs]
        common   = col_sets[0].intersection(*col_sets[1:]) \
                   if len(col_sets) > 1 else col_sets[0]
        cols     = [c for c in numeric_cols(self._dfs[0][0]) if c in common]

        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.addItems(cols)
        self.x_combo.blockSignals(False)

        self.y_list.blockSignals(True)
        self.y_list.clear()
        for c in cols:
            self.y_list.addItem(QListWidgetItem(c))
        self.y_list.blockSignals(False)

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

        n = len(self._dfs)
        self.run_label.setText(f"{n} scan{'s' if n != 1 else ''} selected")
        self._replot()

    def _replot(self):
        if not self._dfs or not PG_AVAILABLE:
            return

        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return

        for curve in self._curves.values():
            try:
                self.plot_widget.removeItem(curve)
            except Exception:
                pass
        pi = self.plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves = {}

        color_idx = 0
        stats     = []
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
                color      = self.COLORS[color_idx % len(self.COLORS)]
                pen        = pg.mkPen(color=color, width=2)
                curve_name = yc if len(self._dfs) == 1 else f"{yc}  [{df_label}]"
                curve = self.plot_widget.plot(
                    x_, y_, pen=pen, name=curve_name,
                    symbol="o", symbolSize=5,
                    symbolBrush=color, symbolPen=None,
                )
                self._curves[curve_name] = curve
                color_idx += 1
                stats.append(f"{yc}: min={y_.min():.4g}  max={y_.max():.4g}")

        self.plot_widget.setLabel("bottom", xc)
        self.plot_widget.setLabel("left", ", ".join(ycs))
        self.stats_label.setText("   ".join(stats))

    # ── Double-click → details dialog ─────────────────────────────────────────

    def _on_double_clicked(self, li: QListWidgetItem):
        scan = li.data(Qt.ItemDataRole.UserRole)
        if scan:
            ScanDetailDialog(scan["attrs"], parent=self).exec()

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._crosshair_cleanup:
            self._crosshair_cleanup()
        super().closeEvent(event)
