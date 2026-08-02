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
    QListWidget, QListWidgetItem, QAbstractItemView, QComboBox, QCheckBox,
    QFileDialog, QDialog, QPlainTextEdit, QDialogButtonBox, QMessageBox,
    QTextEdit, QSizePolicy,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont

from .config import SUCCESS, DANGER, PLOT_COLORS


def _poisson_sigma(y_raw, norm_raw=None):
    """Poisson √N error with propagation through y/norm normalization.

    Raw:        σ = √|y|
    Normalized: σ = √(y/n² + y²/n³)  where n = norm_raw
                  = √|y|/n · √(1 + y/n)
    """
    y = np.abs(y_raw)
    if norm_raw is None:
        return np.sqrt(y)
    n = np.abs(norm_raw)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n > 0, np.sqrt(y / n ** 2 + y ** 2 / n ** 3), np.nan)
from .plot_tools import setup_crosshair
from . import peak_fit as _peak_fit

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
        self._curves: dict      = {}
        self._error_items: dict = {}   # pg.ErrorBarItem per curve
        self._fit_curves: dict  = {}
        self._fit_texts:  list  = []
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

        # Compact control bar
        ctrl_bar = QHBoxLayout()
        ctrl_bar.setSpacing(4)

        ctrl_bar.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(120)
        self.x_combo.setFixedHeight(26)
        self.x_combo.currentTextChanged.connect(self._replot)
        ctrl_bar.addWidget(self.x_combo)

        ctrl_bar.addSpacing(6)
        ctrl_bar.addWidget(QLabel("Norm:"))
        self.norm_combo = QComboBox()
        self.norm_combo.setMinimumWidth(100)
        self.norm_combo.setFixedHeight(26)
        self.norm_combo.addItem("None", userData=None)
        self.norm_combo.currentIndexChanged.connect(self._replot)
        ctrl_bar.addWidget(self.norm_combo)

        ctrl_bar.addSpacing(6)
        self._err_cb = QCheckBox("± Errors")
        self._err_cb.setToolTip(
            "Overlay Poisson √N error bars (propagated through normalization)"
        )
        self._err_cb.stateChanged.connect(self._replot)
        ctrl_bar.addWidget(self._err_cb)

        btn_plot = QPushButton("Plot")
        btn_plot.setObjectName("btn_primary")
        btn_plot.setFixedHeight(26)
        btn_plot.clicked.connect(self._replot)
        ctrl_bar.addWidget(btn_plot)

        ctrl_bar.addSpacing(10)

        ctrl_bar.addWidget(QLabel("Fit:"))
        self._fit_model_combo = QComboBox()
        self._fit_model_combo.setFixedHeight(26)
        self._fit_model_combo.setMinimumWidth(110)
        for m in _peak_fit.MODELS:
            self._fit_model_combo.addItem(m)
        ctrl_bar.addWidget(self._fit_model_combo)

        btn_fit = QPushButton("Fit")
        btn_fit.setFixedHeight(26)
        btn_fit.clicked.connect(self._fit_peak)
        ctrl_bar.addWidget(btn_fit)

        btn_clear_fit = QPushButton("✕")
        btn_clear_fit.setFixedHeight(26)
        btn_clear_fit.setFixedWidth(28)
        btn_clear_fit.setToolTip("Clear fit overlays")
        btn_clear_fit.clicked.connect(self._clear_fit_overlays)
        ctrl_bar.addWidget(btn_clear_fit)

        ctrl_bar.addSpacing(8)
        self.run_label = QLabel("")
        self.run_label.setObjectName("dim_text")
        self.run_label.setStyleSheet("font-size: 12px; padding: 0 4px;")
        ctrl_bar.addWidget(self.run_label)
        ctrl_bar.addStretch()
        vlay.addLayout(ctrl_bar)

        # Y signal list on right of plot
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setFixedWidth(140)
        self.y_list.itemSelectionChanged.connect(self._replot)

        y_panel = QVBoxLayout()
        y_panel.setSpacing(2)
        y_panel.setContentsMargins(0, 0, 0, 0)
        y_lbl = QLabel("Y signals")
        y_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        y_lbl.setObjectName("dim_text")
        y_panel.addWidget(y_lbl)
        y_panel.addWidget(self.y_list, 1)

        plot_row = QHBoxLayout()
        plot_row.setSpacing(4)
        if PG_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            plot_row.addWidget(self.plot_widget, 1)
        else:
            plot_row.addWidget(
                QLabel("pyqtgraph not available — pip install pyqtgraph"), 1)
        plot_row.addLayout(y_panel)
        vlay.addLayout(plot_row, 1)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim_text")
        bot.addWidget(self.stats_label, 1)
        self.coord_label = QLabel("")
        self.coord_label.setObjectName("dim_text")
        self.coord_label.setStyleSheet(
            "font-size: 11px; padding: 4px; font-family: Menlo, Monaco, Courier New, monospace;")
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

        prev_norm = self.norm_combo.currentData()
        self.norm_combo.blockSignals(True)
        self.norm_combo.clear()
        self.norm_combo.addItem("None", userData=None)
        for c in cols:
            self.norm_combo.addItem(c, userData=c)
        for i in range(self.norm_combo.count()):
            if self.norm_combo.itemData(i) == prev_norm:
                self.norm_combo.setCurrentIndex(i)
                break
        self.norm_combo.blockSignals(False)

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

        self._clear_fit_overlays()
        for item in list(self._error_items.values()) + list(self._curves.values()):
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        pi = self.plot_widget.getPlotItem()
        if pi.legend:
            pi.legend.clear()
        self._curves = {}
        self._error_items = {}

        norm_col  = self.norm_combo.currentData()
        show_err  = self._err_cb.isChecked()
        color_idx = 0
        stats     = []
        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x        = df[xc].values.astype(float)
            norm_raw = df[norm_col].values.astype(float) \
                       if norm_col and norm_col in df.columns else None
            for yc in ycs:
                if yc not in df.columns:
                    continue
                y_raw = df[yc].values.astype(float)

                # Normalization
                y = y_raw.copy()
                if norm_raw is not None:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(norm_raw != 0, y / norm_raw, np.nan)

                # Poisson σ (propagated through normalization)
                sigma = _poisson_sigma(y_raw, norm_raw)

                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_, s_ = x[mask], y[mask], sigma[mask]
                if not len(x_):
                    continue

                color      = self.COLORS[color_idx % len(self.COLORS)]
                pen        = pg.mkPen(color=color, width=2)
                label      = yc if not norm_col else f"{yc}/{norm_col}"
                curve_name = label if len(self._dfs) == 1 else f"{label}  [{df_label}]"
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
                stats.append(f"{curve_name}: min={y_.min():.4g}  max={y_.max():.4g}")

        self.plot_widget.setLabel("bottom", xc)
        y_label = ", ".join(ycs)
        if norm_col:
            y_label += f"  /  {norm_col}"
        self.plot_widget.setLabel("left", y_label)
        self.stats_label.setText("   ".join(stats))

    # ── Double-click → details dialog ─────────────────────────────────────────

    def _on_double_clicked(self, li: QListWidgetItem):
        scan = li.data(Qt.ItemDataRole.UserRole)
        if scan:
            ScanDetailDialog(scan["attrs"], parent=self).exec()

    # ── Peak fitting ───────────────────────────────────────────────────────────

    def _clear_fit_overlays(self):
        if not PG_AVAILABLE:
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
        self._fit_texts = []
        self._fit_curves = {}

    def _get_xy_for_fit(self):
        """Return list of (x, y, label) for all plotted curves."""
        if not self._dfs:
            return []
        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return []
        norm_col = self.norm_combo.currentData()
        result = []
        for df, df_label in self._dfs:
            if xc not in df.columns:
                continue
            x = df[xc].values.astype(float)
            norm_vals = df[norm_col].values.astype(float) \
                        if norm_col and norm_col in df.columns else None
            for yc in ycs:
                if yc not in df.columns:
                    continue
                y = df[yc].values.astype(float)
                if norm_vals is not None:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        y = np.where(norm_vals != 0, y / norm_vals, np.nan)
                mask = np.isfinite(x) & np.isfinite(y)
                x_, y_ = x[mask], y[mask]
                if len(x_) < 4:
                    continue
                lbl = yc if len(self._dfs) == 1 else f"{yc} [{df_label}]"
                result.append((x_, y_, lbl))
        return result

    def _add_fit_overlay(self, x_fit, y_fit, info, label, color):
        pen = pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine)
        key = f"fit:{label}"
        curve = self.plot_widget.plot(x_fit, y_fit, pen=pen, name=key)
        self._fit_curves[key] = curve

        ann = (f"  {info['model']}\n"
               f"  x₀={info['x0']:.4g}\n"
               f"  FWHM={info['fwhm']:.4g}\n"
               f"  R²={info['r2']:.4f}")
        txt = pg.TextItem(ann, color=color, anchor=(0, 1))
        txt.setPos(float(info["x0"]), float(info["A"]))
        self.plot_widget.addItem(txt)
        self._fit_texts.append(txt)
        return info

    def _fit_peak(self):
        if not PG_AVAILABLE:
            return
        model = self._fit_model_combo.currentText()
        datasets = self._get_xy_for_fit()
        if not datasets:
            QMessageBox.warning(self, "Fit", "No data to fit. Select scans and Y signals first.")
            return

        if len(datasets) > 1:
            dlg = _HDF5FitModeDialog(len(datasets), parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            combine = dlg.combine

            if combine:
                x_all = np.concatenate([d[0] for d in datasets])
                y_all = np.concatenate([d[1] for d in datasets])
                order = np.argsort(x_all)
                datasets_to_fit = [(x_all[order], y_all[order], "combined")]
            else:
                datasets_to_fit = datasets
        else:
            datasets_to_fit = datasets

        self._clear_fit_overlays()
        colors = ["#ff6688", "#66ffaa", "#ffaa33", "#33aaff", "#cc88ff"]
        fit_infos = []
        errors = []
        for idx, (x, y, lbl) in enumerate(datasets_to_fit):
            color = colors[idx % len(colors)]
            try:
                x_fit, y_fit, info = _peak_fit.fit_peak(x, y, model)
                self._add_fit_overlay(x_fit, y_fit, info, lbl, color)
                info["_label"] = lbl
                fit_infos.append(info)
            except Exception as exc:
                errors.append(f"{lbl}: {exc}")

        if errors:
            QMessageBox.warning(self, "Fit errors", "\n".join(errors))

        if fit_infos:
            rpt = _HDF5FitReportDialog(fit_infos, parent=self)
            rpt.show()

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._crosshair_cleanup:
            self._crosshair_cleanup()
        super().closeEvent(event)


# ── Helper dialogs for HDF5 peak fitting ──────────────────────────────────────

class _HDF5FitModeDialog(QDialog):
    def __init__(self, n_datasets: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fit mode")
        self.combine = False
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"{n_datasets} datasets selected. Fit each individually or combine all?"))
        bb = QDialogButtonBox()
        btn_each    = bb.addButton("Fit each individually", QDialogButtonBox.ButtonRole.AcceptRole)
        btn_combine = bb.addButton("Combine all",           QDialogButtonBox.ButtonRole.AcceptRole)
        btn_cancel  = bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        btn_each.clicked.connect(lambda: self._choose(False))
        btn_combine.clicked.connect(lambda: self._choose(True))
        btn_cancel.clicked.connect(self.reject)
        lay.addWidget(bb)

    def _choose(self, combine: bool):
        self.combine = combine
        self.accept()


class _HDF5FitReportDialog(QDialog):
    def __init__(self, fit_infos: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Peak Fit Report")
        self.setMinimumSize(480, 320)
        lay = QVBoxLayout(self)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setFont(QFont("Courier New", 11))
        lay.addWidget(txt)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)

        lines = []
        for info in fit_infos:
            label = info.get("_label", "")
            lines.append(f"{'─'*52}")
            if label:
                lines.append(f"Dataset : {label}")
            lines.append(f"Model   : {info['model']}")
            lines.append(f"Center  : {info['x0']:.6g}")
            lines.append(f"FWHM    : {info['fwhm']:.6g}")
            lines.append(f"R²      : {info['r2']:.6f}")
            lines.append(f"N pts   : {info['n_points']}")
            lines.append("")
            lines.append("Parameters:")
            for name, val, err in zip(info["param_names"], info["params"], info["perr"]):
                lines.append(f"  {name:<22} {val:.6g}  ± {err:.3g}")
            if info["model"] == "Super-Gaussian":
                lines.append(f"  Exponent n             {info.get('n_exp', '?'):.4g}")
            lines.append("")
        txt.setPlainText("\n".join(lines))

