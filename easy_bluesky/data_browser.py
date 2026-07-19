"""data_browser.py — Data Browser tab: Databroker historical runs."""

from datetime import datetime
import numpy as np
try:
    import intake
    intake.config.conf['catalog_path'] = ['~/.local/share/intake']
    import databroker
    import pandas as pd
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QComboBox, QAbstractItemView,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from .config import CATALOG_NAME, SUCCESS, DANGER, PLOT_COLORS

class DataBrowser(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._catalog = None
        self._df      = None
        self._build()
        self._load_catalog()

    def _build(self):
        main = QHBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: Run browser ──────────────────────────────────────────────────
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(8, 8, 8, 8)

        lbl = QLabel("RUNS")
        lbl.setObjectName("section_title")
        llay.addWidget(lbl)

        refresh = QPushButton("⟳ Refresh")
        refresh.clicked.connect(self._load_catalog)
        llay.addWidget(refresh)

        self.run_list = QListWidget()
        self.run_list.currentItemChanged.connect(self._on_run_selected)
        llay.addWidget(self.run_list, 1)

        splitter.addWidget(left)

        # ── Right: Plot + analysis ─────────────────────────────────────────────
        right = QWidget()
        rlay  = QVBoxLayout(right)
        rlay.setContentsMargins(8, 8, 8, 8)

        # Axis selectors
        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        axis_row.addWidget(self.x_combo)
        axis_row.addWidget(QLabel("Y:"))
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.y_list.setMaximumHeight(60)
        self.y_list.setMaximumWidth(200)
        axis_row.addWidget(self.y_list)
        btn_plot = QPushButton("Plot")
        btn_plot.setObjectName("btn_primary")
        btn_plot.clicked.connect(self._plot)
        axis_row.addWidget(btn_plot)
        rlay.addLayout(axis_row)

        # Plot
        if PYQTGRAPH_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            rlay.addWidget(self.plot_widget, 1)
        else:
            rlay.addWidget(QLabel("pyqtgraph not available"), 1)

        # Stats
        lbl2 = QLabel("STATISTICS")
        lbl2.setObjectName("section_title")
        rlay.addWidget(lbl2)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #888; font-size: 12px;")
        self.stats_label.setWordWrap(True)
        rlay.addWidget(self.stats_label)

        splitter.addWidget(right)
        splitter.setSizes([240, 560])
        main.addWidget(splitter)

    COLORS = ["#1f77b4","#ff7f0e","#2ca02c","#d62728","#9467bd"]

    def _load_catalog(self):
        if not DB_AVAILABLE:
            return
        try:
            self._catalog = databroker.catalog[CATALOG_NAME]
            self.run_list.clear()
            for uid in list(self._catalog)[-50:]:
                try:
                    run  = self._catalog[uid]
                    md   = run.metadata
                    s    = md.get("start", {})
                    stop = md.get("stop",  {})
                    ts   = datetime.fromtimestamp(
                               s.get("time", 0)).strftime("%m-%d %H:%M")
                    plan = s.get("plan_name", "?")
                    ok   = "✓" if stop.get("exit_status")=="success" else "✗"
                    li   = QListWidgetItem(f"{ok} {ts}  {plan}  [{str(uid)[:8]}]")
                    li.setData(Qt.ItemDataRole.UserRole, str(uid))
                    li.setForeground(QColor(SUCCESS if ok=="✓" else DANGER))
                    self.run_list.insertItem(0, li)
                except Exception:
                    pass
        except Exception as e:
            print(f"[DataBrowser] catalog error: {e}")

    def _on_run_selected(self, current, previous):
        if not current or not self._catalog:
            return
        uid = current.data(Qt.ItemDataRole.UserRole)
        try:
            run = self._catalog[uid]
            ds  = run.primary.read()
            self._df = ds.to_dataframe().reset_index(drop=True)
            cols = [c for c in self._df.columns
                    if pd.api.types.is_numeric_dtype(self._df[c])]
            self.x_combo.clear()
            self.x_combo.addItems(cols)
            self.y_list.clear()
            for c in cols:
                self.y_list.addItem(QListWidgetItem(c))
            if len(cols) >= 2:
                self.x_combo.setCurrentIndex(0)
                self.y_list.item(1).setSelected(True)
        except Exception as e:
            print(f"[DataBrowser] load error: {e}")

    def _plot(self):
        if self._df is None or not PYQTGRAPH_AVAILABLE:
            return
        self.plot_widget.clear()
        xc = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return
        x = self._df[xc].values.astype(float)
        stats_parts = []
        for i, yc in enumerate(ycs):
            y     = self._df[yc].values.astype(float)
            mask  = np.isfinite(x) & np.isfinite(y)
            x_, y_ = x[mask], y[mask]
            color = self.COLORS[i % len(self.COLORS)]
            pen   = pg.mkPen(color=color, width=2)
            self.plot_widget.plot(
                x_, y_, pen=pen, name=yc,
                symbol="o", symbolSize=5,
                symbolBrush=color, symbolPen=None,
            )
            stats_parts.append(
                f"{yc}: min={y_.min():.4f} max={y_.max():.4f} "
                f"mean={y_.mean():.4f} std={y_.std():.4f}"
            )
        self.plot_widget.setLabel("bottom", xc)
        self.plot_widget.setLabel("left",   ", ".join(ycs))
        self.stats_label.setText("\n".join(stats_parts))
