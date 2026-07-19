"""data_browser.py — Data Browser tab: historical runs via Tiled or Databroker."""

from datetime import datetime
import numpy as np

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

try:
    import pyqtgraph as pg
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QComboBox, QAbstractItemView, QLineEdit,
    QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from .config import CATALOG_NAME, TILED_URI, TILED_API_KEY, DATA_RUNS_DIR, SUCCESS, DANGER, ACCENT, PLOT_COLORS


# ── Background loader thread ───────────────────────────────────────────────────

class CatalogLoader(QThread):
    runs_ready  = pyqtSignal(list)   # list of (uid, label, color, run_obj)
    error       = pyqtSignal(str)

    def __init__(self, source_type, source_value):
        super().__init__()
        self.source_type  = source_type   # "tiled" | "databroker"
        self.source_value = source_value  # URL or catalog name

    def run(self):
        try:
            runs = []
            if self.source_type == "tiled":
                runs = self._load_tiled(self.source_value)
            elif self.source_type == "local":
                runs = self._load_local_jsonl(self.source_value)
            else:
                runs = self._load_databroker(self.source_value)
            self.runs_ready.emit(runs)
        except Exception as e:
            self.error.emit(str(e))

    def _load_tiled(self, uri):
        from tiled.client import from_uri
        client = from_uri(uri, api_key=TILED_API_KEY)
        # Tiled catalogs may be nested; find the runs container
        catalog = client
        # Try to descend into a 'runs' or first child if it's a container of containers
        try:
            if hasattr(catalog, 'values'):
                first = next(iter(catalog.values()), None)
                if first is not None and not hasattr(first, 'metadata'):
                    # one level deeper
                    catalog = first
        except Exception:
            pass

        runs = []
        uids = list(catalog)[-50:]
        for uid in reversed(uids):
            try:
                run  = catalog[uid]
                md   = run.metadata if hasattr(run, 'metadata') else {}
                s    = md.get("start", {}) if md else {}
                stop = md.get("stop",  {}) if md else {}
                ts   = s.get("time", 0)
                t    = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
                plan = s.get("plan_name", "?")
                ok   = stop.get("exit_status", "") == "success" if stop else False
                icon = "✓" if ok else "✗"
                color = SUCCESS if ok else DANGER
                label = f"{icon} {t}  {plan}  [{str(uid)[:8]}]"
                runs.append((str(uid), label, color, run))
            except Exception:
                pass
        return runs

    def _load_databroker(self, catalog_name):
        import databroker
        cat = databroker.catalog[catalog_name]
        runs = []
        uids = list(cat)[-50:]
        for uid in reversed(uids):
            try:
                run  = cat[uid]
                md   = run.metadata
                s    = md.get("start", {})
                stop = md.get("stop",  {})
                ts   = s.get("time", 0)
                t    = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
                plan = s.get("plan_name", "?")
                ok   = stop.get("exit_status", "") == "success" if stop else False
                icon = "✓" if ok else "✗"
                color = SUCCESS if ok else DANGER
                label = f"{icon} {t}  {plan}  [{str(uid)[:8]}]"
                runs.append((str(uid), label, color, run))
            except Exception:
                pass
        return runs

    def _load_local_jsonl(self, directory):
        import json
        from pathlib import Path
        runs = []
        files = sorted(Path(directory).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for fpath in files[:50]:
            try:
                docs_by_name = {}
                events = []
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        name, doc = json.loads(line)
                        if name in ("start", "stop"):
                            docs_by_name[name] = doc
                        elif name == "event":
                            events.append(doc)
                        elif name == "event_page":
                            # Unpack event_page into individual event dicts
                            times = doc.get("time", [])
                            seq_nums = doc.get("seq_num", [])
                            data_cols = doc.get("data", {})
                            n = len(times)
                            for i in range(n):
                                ev = {
                                    "time": times[i] if i < len(times) else None,
                                    "seq_num": seq_nums[i] if i < len(seq_nums) else None,
                                    "data": {k: v[i] for k, v in data_cols.items() if i < len(v)},
                                }
                                events.append(ev)
                s    = docs_by_name.get("start", {})
                stop = docs_by_name.get("stop",  {})
                uid  = s.get("uid", fpath.stem)
                ts   = s.get("time", 0)
                t    = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
                plan = s.get("plan_name", "?")
                ok   = stop.get("exit_status", "") == "success" if stop else False
                icon = "✓" if ok else "✗"
                color = SUCCESS if ok else DANGER
                label = f"{icon} {t}  {plan}  [{uid[:8]}]"
                run_obj = {"_jsonl_events": events, "_start": s, "_stop": stop}
                runs.append((uid, label, color, run_obj))
            except Exception:
                pass
        return runs


# ── Main widget ────────────────────────────────────────────────────────────────

class DataBrowser(QWidget):
    COLORS = PLOT_COLORS

    def __init__(self, parent=None):
        super().__init__(parent)
        self._run     = None
        self._df      = None
        self._loader  = None
        self._build()

    def _build(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)

        # ── Connection bar ─────────────────────────────────────────────────────
        conn_frame = QFrame()
        conn_frame.setFrameShape(QFrame.Shape.StyledPanel)
        conn_lay = QHBoxLayout(conn_frame)
        conn_lay.setContentsMargins(6, 4, 6, 4)

        conn_lay.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Local JSONL files", "Tiled server", "Databroker catalog"])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        conn_lay.addWidget(self.source_combo)

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("directory path  or  http://localhost:8000  or  catalog-name")
        self.source_edit.setText(DATA_RUNS_DIR)
        conn_lay.addWidget(self.source_edit, 1)

        btn_connect = QPushButton("Connect")
        btn_connect.setObjectName("btn_primary")
        btn_connect.clicked.connect(self._load_catalog)
        conn_lay.addWidget(btn_connect)

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("color: #888; font-size: 12px;")
        conn_lay.addWidget(self.status_label)

        main.addWidget(conn_frame)

        # ── Main splitter ──────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: run list
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 4, 0)

        hdr = QHBoxLayout()
        lbl = QLabel("RUNS")
        lbl.setObjectName("section_title")
        hdr.addWidget(lbl)
        hdr.addStretch()
        btn_refresh = QPushButton("⟳")
        btn_refresh.setToolTip("Refresh run list")
        btn_refresh.setMaximumWidth(32)
        btn_refresh.clicked.connect(self._load_catalog)
        hdr.addWidget(btn_refresh)
        llay.addLayout(hdr)

        self.run_list = QListWidget()
        self.run_list.currentItemChanged.connect(self._on_run_selected)
        llay.addWidget(self.run_list, 1)

        splitter.addWidget(left)

        # Right: plot + stats
        right = QWidget()
        rlay  = QVBoxLayout(right)
        rlay.setContentsMargins(4, 0, 0, 0)

        # Axis selectors
        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
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
        rlay.addLayout(axis_row)

        if PG_AVAILABLE:
            self.plot_widget = pg.PlotWidget(background="#1e1e1e")
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.addLegend()
            rlay.addWidget(self.plot_widget, 1)
        else:
            rlay.addWidget(QLabel("pyqtgraph not available"), 1)

        lbl2 = QLabel("STATISTICS")
        lbl2.setObjectName("section_title")
        rlay.addWidget(lbl2)
        self.stats_label = QLabel("Select a run to view statistics.")
        self.stats_label.setStyleSheet("color: #888; font-size: 12px;")
        self.stats_label.setWordWrap(True)
        rlay.addWidget(self.stats_label)

        splitter.addWidget(right)
        splitter.setSizes([240, 560])
        main.addWidget(splitter, 1)

    def set_runs_dir(self, path: str):
        """Switch the browser to a specific local runs directory and reload."""
        self.source_combo.setCurrentIndex(0)
        self.source_edit.setText(path)
        self._load_catalog()

    def _on_source_changed(self, idx):
        if idx == 0:  # Local JSONL
            self.source_edit.setPlaceholderText("path/to/runs/directory")
            self.source_edit.setText(DATA_RUNS_DIR)
        elif idx == 1:  # Tiled
            self.source_edit.setPlaceholderText("http://localhost:8000")
            self.source_edit.setText(TILED_URI or "")
        else:  # Databroker
            self.source_edit.setPlaceholderText("catalog-name")
            self.source_edit.setText(CATALOG_NAME)

    def _load_catalog(self):
        if self._loader and self._loader.isRunning():
            return

        idx = self.source_combo.currentIndex()
        source_type  = ["local", "tiled", "databroker"][idx]
        source_value = self.source_edit.text().strip()

        if not source_value:
            self.status_label.setText("Enter a URL or catalog name first")
            self.status_label.setStyleSheet(f"color: {DANGER}; font-size: 12px;")
            return

        self.run_list.clear()
        self.status_label.setText("Connecting…")
        self.status_label.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")

        self._loader = CatalogLoader(source_type, source_value)
        self._loader.runs_ready.connect(self._on_runs_ready)
        self._loader.error.connect(self._on_load_error)
        self._loader.start()

    def _on_runs_ready(self, runs):
        self.run_list.clear()
        for uid, label, color, run_obj in runs:
            li = QListWidgetItem(label)
            li.setForeground(QColor(color))
            li.setData(Qt.ItemDataRole.UserRole,     uid)
            li.setData(Qt.ItemDataRole.UserRole + 1, run_obj)
            self.run_list.addItem(li)

        n = len(runs)
        self.status_label.setText(f"Connected — {n} run{'s' if n != 1 else ''} loaded")
        self.status_label.setStyleSheet(f"color: {SUCCESS}; font-size: 12px;")

    def _on_load_error(self, msg):
        self.status_label.setText(f"Error: {msg}")
        self.status_label.setStyleSheet(f"color: {DANGER}; font-size: 12px;")

    def _on_run_selected(self, current, previous):
        if not current:
            return
        run = current.data(Qt.ItemDataRole.UserRole + 1)
        if run is None:
            return
        try:
            self._df = self._read_run(run)
            if self._df is None or self._df.empty:
                self.stats_label.setText("No tabular data in this run.")
                return
            cols = [c for c in self._df.columns
                    if self._df[c].dtype.kind in ('f', 'i', 'u')]
            self.x_combo.clear()
            self.x_combo.addItems(cols)
            self.y_list.clear()
            for c in cols:
                self.y_list.addItem(QListWidgetItem(c))
            if len(cols) >= 2:
                self.x_combo.setCurrentIndex(0)
                self.y_list.item(1).setSelected(True)
            self.stats_label.setText(
                f"{len(self._df)} events  |  columns: {', '.join(cols)}")
        except Exception as e:
            self.stats_label.setText(f"Load error: {e}")

    def _read_run(self, run):
        """Try multiple access patterns across tiled, databroker, and local JSONL."""
        # Local JSONL dict
        if isinstance(run, dict) and "_jsonl_events" in run:
            events = run["_jsonl_events"]
            if not events:
                return None
            rows = []
            for ev in events:
                row = {"seq_num": ev.get("seq_num"), "time": ev.get("time")}
                row.update(ev.get("data", {}))
                rows.append(row)
            if PANDAS_AVAILABLE and rows:
                import pandas as pd
                return pd.DataFrame(rows)
            return None

        # Tiled: run['primary']['data'].read() or run.primary.read()
        for accessor in [
            lambda r: r['primary']['data'].read().to_dataframe().reset_index(drop=True),
            lambda r: r.primary.read().to_dataframe().reset_index(drop=True),
            lambda r: r['primary'].read().to_dataframe().reset_index(drop=True),
        ]:
            try:
                df = accessor(run)
                if df is not None and not df.empty:
                    return df
            except Exception:
                pass
        return None

    def _plot(self):
        if self._df is None or not PG_AVAILABLE:
            return
        self.plot_widget.clear()
        xc  = self.x_combo.currentText()
        ycs = [self.y_list.item(i).text()
               for i in range(self.y_list.count())
               if self.y_list.item(i).isSelected()]
        if not xc or not ycs:
            return

        x = self._df[xc].values.astype(float)
        stats = []
        for i, yc in enumerate(ycs):
            y    = self._df[yc].values.astype(float)
            mask = np.isfinite(x) & np.isfinite(y)
            x_, y_ = x[mask], y[mask]
            if len(x_) == 0:
                continue
            color = self.COLORS[i % len(self.COLORS)]
            pen   = pg.mkPen(color=color, width=2)
            self.plot_widget.plot(
                x_, y_, pen=pen, name=yc,
                symbol="o", symbolSize=5,
                symbolBrush=color, symbolPen=None,
            )
            stats.append(
                f"{yc}: min={y_.min():.4g}  max={y_.max():.4g}"
                f"  mean={y_.mean():.4g}  std={y_.std():.4g}"
            )
        self.plot_widget.setLabel("bottom", xc)
        self.plot_widget.setLabel("left",   ", ".join(ycs))
        self.stats_label.setText("\n".join(stats))
