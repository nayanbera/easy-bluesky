"""hdf5_browser.py — the HDF5 Viewer: the MongoDB Browser reading from an exported .h5 file.

Subclasses ``MongoDataBrowserTab`` and replaces only the data source (run list, run data, run detail
window, exports), so every feature of the MongoDB Browser — streams, baseline, detail window,
statistics markers, derivative/errors, 2D maps, fitting, motor repro — is available for archives too.
Files written by the complete export (``hdf5_export.py``) show all streams and documents; older
exports (flat primary datasets only) still open and show the primary stream.
"""
from datetime import datetime
from pathlib import Path

from qtpy.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
)

from .mongo_browser import MongoDataBrowserTab, _RunDetailDialog, H5PY_AVAILABLE
from .hdf5_export import read_docs, read_streams

try:
    import h5py
except ImportError:          # pragma: no cover
    h5py = None


class HDF5BrowserTab(MongoDataBrowserTab):
    def __init__(self, settings: dict, parent=None):
        self._h5_path   = ""
        self._h5_groups = {}          # uid -> group key in the file
        super().__init__(settings, parent)
        self._customise_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _customise_ui(self):
        self._profile_combo.setVisible(False)
        self._show_all_cb.setVisible(False)
        self._exp_label.setText("")
        self._db_label.setText("  No file open — click “Open HDF5…”")
        top = self.layout().itemAt(0).layout()          # top bar (profile / db / refresh)
        btn_open = QPushButton("Open HDF5…")
        btn_open.setObjectName("btn_primary")
        btn_open.setFixedHeight(28)
        btn_open.clicked.connect(self._open_file)
        if not H5PY_AVAILABLE:
            btn_open.setEnabled(False)
            btn_open.setToolTip("pip install h5py")
        top.insertWidget(0, btn_open)
        self._btn_export_hdf5.setToolTip("Copy the selected scans into a new HDF5 file")
        self._btn_export_exp.setToolTip("Copy all scans of this file into a new HDF5 file")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open HDF5 Archive", self._h5_path or "", "HDF5 Files (*.h5 *.hdf5)")
        if path:
            self.load_file(path)

    # ── Source plumbing (replaces the MongoDB specifics) ─────────────────────

    def _populate_profile_combo(self):      # no profiles / databases here
        pass

    def update_settings(self, settings: dict):
        self._settings = settings

    def _on_profile_changed(self, _idx: int):
        pass

    def set_active_experiment(self, exp_dir: str):   # not tied to the live experiment
        pass

    def set_running_item(self, item: dict) -> None:
        pass

    def refresh(self):                      # scan_completed refreshes the Mongo tab only
        pass

    def load_file(self, filepath: str):
        if not H5PY_AVAILABLE:
            self._db_label.setText("  h5py not installed — pip install h5py")
            return
        self._h5_path = filepath
        self._fetch_runs()

    def _fetch_runs(self):
        if not self._h5_path:
            return
        try:
            runs, groups, meta = self._read_index(self._h5_path)
        except Exception as exc:
            self._set_status(f"Cannot read file: {exc}", error=True)
            QMessageBox.critical(self, "HDF5 Error", str(exc))
            return
        self._h5_groups = groups
        self._run_data_list = []
        self._clear_axis_controls()
        self._clear_plot()
        label = Path(self._h5_path).name
        exp = meta.get("experiment_name")
        self._db_label.setText(f"  {label}" + (f"   —   experiment {exp}" if exp else ""))
        self._on_runs_ready(runs)
        self._set_status(f"{len(runs)} scan(s) in {label}.")

    @staticmethod
    def _read_index(path: str):
        runs, groups = [], {}
        with h5py.File(path, "r") as hf:
            meta = dict(hf["metadata"].attrs) if "metadata" in hf else {}
            for key in hf.keys():
                if not key.startswith("scan_"):
                    continue
                g     = hf[key]
                attrs = dict(g.attrs)
                start, stop = read_docs(g.attrs)
                n = int(attrs.get("scan_num", 0) or 0)
                if not start:                       # file from the old export: synthesize
                    t0 = 0.0
                    try:
                        t0 = datetime.fromisoformat(str(attrs.get("timestamp", ""))).timestamp()
                    except Exception:
                        pass
                    start = {"plan_name": str(attrs.get("plan_name", "")), "time": t0}
                    if attrs.get("motor"):
                        start["motors"] = [str(attrs["motor"])]
                    if attrs.get("detectors"):
                        start["detectors"] = [d.strip() for d in str(attrs["detectors"]).split(",")]
                    for k in ("sample_name", "sample_description", "exp_dir"):
                        if attrs.get(k):
                            start[k] = str(attrs[k])
                if not stop:
                    status = str(attrs.get("exit_status", "")) or "success"
                    stop = {"exit_status": "success" if status == "completed" else status,
                            "time": (start.get("time", 0) or 0) + float(attrs.get("duration_s", 0) or 0),
                            "num_events": {"primary": int(attrs.get("n_events", 0) or 0)}}
                start.setdefault("uid", str(attrs.get("uid", key)))
                start["scan_num"] = n or start.get("scan_num") or 0
                groups[start["uid"]] = key
                runs.append({"start": start, "stop": stop})
        runs.sort(key=lambda r: r["start"].get("scan_num") or 0, reverse=True)
        return runs, groups, meta

    def _load_streams(self, uid: str) -> dict:
        key = self._h5_groups.get(uid)
        if not key:
            return {}
        with h5py.File(self._h5_path, "r") as hf:
            g = hf[key]
            streams = read_streams(g)
            if not streams:                          # legacy flat primary datasets
                cols = {k: ds[()] for k, ds in g.items() if isinstance(ds, h5py.Dataset)}
                if cols:
                    import numpy as np
                    prim = {"data_keys": {k: {} for k in cols if k != "time"}}
                    prim.update(cols)
                    if "time" not in prim:
                        prim["time"] = np.arange(len(next(iter(cols.values()))), dtype=float)
                    streams = {"primary": prim}
        return streams

    def _schedule_data_fetch(self):
        rows = self._run_table.selectionModel().selectedRows()
        out = []
        for idx in rows:
            if idx.row() >= len(self._runs):
                continue
            start = self._runs[idx.row()]["start"]
            uid = start.get("uid", "")
            streams = self._load_streams(uid)
            if streams:
                out.append({"uid": uid, "label": f"#{start.get('scan_num')} {start.get('plan_name', '?')}",
                            "streams": streams})
        if rows:
            self._on_data_ready(out)

    def _on_run_double_clicked(self, row: int, _col: int):
        if row >= len(self._runs):
            return
        run = self._runs[row]
        uid = run["start"].get("uid", "")
        rd = next((r for r in self._run_data_list if r.get("uid") == uid), None)
        if rd is None:
            rd = {"uid": uid, "label": "", "streams": self._load_streams(uid)}
        _RunDetailDialog(run, rd, run["start"].get("scan_num") or 0,
                         mongo_profile=None, parent=self).show()

    # ── "Export" = copy scans into another HDF5 file ──────────────────────────

    def _copy_scans(self, runs: list, title: str):
        if not runs or not self._h5_path:
            QMessageBox.warning(self, "No scans", "No scans to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, title, str(Path(self._h5_path).with_name("selection.h5")),
            "HDF5 Files (*.h5 *.hdf5)")
        if not path:
            return
        try:
            with h5py.File(self._h5_path, "r") as src, h5py.File(path, "w") as dst:
                if "metadata" in src:
                    src.copy("metadata", dst)
                    dst["metadata"].attrs["n_scans"] = len(runs)
                for run in runs:
                    key = self._h5_groups.get(run["start"].get("uid", ""))
                    if key:
                        src.copy(key, dst)
            self._set_status(f"✓ Exported {len(runs)} scan(s) → {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))

    def _export_hdf5(self):
        rows = self._run_table.selectionModel().selectedRows()
        self._copy_scans([self._runs[i.row()] for i in rows if i.row() < len(self._runs)],
                         "Export Selected Scans to HDF5")

    def _export_experiment_hdf5(self):
        self._copy_scans(list(self._runs), "Export All Scans to HDF5")
