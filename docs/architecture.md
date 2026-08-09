# Architecture

## Overview

```
easy-bluesky
├── UI Layer (PyQt6)
│   ├── MainWindow          — tab container, toolbar, profile switching
│   ├── REControlBar        — RE state toolbar (start/pause/abort/env/profile)
│   ├── QueueManager        — queue list, history, plan detail panel
│   ├── PlanBuilder         — Visual Composer + Code Editor
│   ├── ExperimentsTab      — experiment lifecycle, plan log, HDF5 export
│   ├── LiveViewer          — ZMQ doc subscriber + pyqtgraph live plots
│   ├── MongoDataBrowserTab — browse/plot/export runs from MongoDB
│   ├── HDF5Viewer          — open and browse exported HDF5 files
│   ├── DevicesPlansTab     — live device tree (CA monitor + sim monitor)
│   ├── REConsole           — SSH log tail (live RE output)
│   └── PVWatchdog          — PV alarm monitor
│
├── Communication Layer
│   ├── ZMQWorker           — bluesky-queueserver ZMQ transport (poll thread)
│   ├── ZMQDocThread        — ZMQ SUB thread for live bluesky documents
│   └── SSHLogTailer        — SSH `tail -f` of RE Manager log file
│
├── Data Layer
│   ├── MongoDB             — primary run store (via pymongo, written by RE Manager)
│   ├── <uid>.jsonl files   — per-run JSONL fallback, always written by RE Manager
│   ├── plans_log.jsonl     — lightweight experiment plan index (local)
│   └── device_metadata.json— cached PV units/descriptions for sim mode
│
└── Config Layer
    ├── config.py           — constants, env-overridable
    └── connection.json     — profiles, ports, SSH settings (local, never committed)
```

## Thread Model

```
Main Thread (Qt event loop)
    ├── All Qt widget updates
    ├── CA callbacks (forwarded via queued signals from pyepics CA thread)
    └── ZMQ execute_item() calls (blocking — only called from UI actions)

ZMQWorker poll thread (QThread)
    └── worker.poll() — polls RE Manager status every 1 s
        └── emits Qt signals → main thread updates UI
            ├── status_updated(dict)
            ├── console_line(str)
            ├── devices_updated(dict)
            └── connected() / disconnected()

ZMQDocThread (QThread — per LiveViewer)
    └── zmq.SUB socket — receives bluesky documents in real time
        └── emits doc_received(name, doc) → LiveViewer._on_doc()

SSHLogTailer (QThread — inside ZMQWorker)
    └── SSH `tail -n 50 -f <log_file>` — streams RE Manager stdout
        └── lines queued → poll loop drains → console_line signal

_RunListFetcher / _MultiRunDataFetcher / _HDF5Exporter (QThread)
    └── pymongo queries on background threads → signals back to UI

_PVNamesReader / _DeviceStatusReader (QThread)
    └── RE environment function_execute calls → signals back to DevicesPlansTab

pyepics CA thread (internal)
    └── CA callbacks → forwarded to Qt via pyqtSignal (queued connection)
```

## Data Flow

### Live scan (ZMQ documents)

```
RE Manager
  → _zmq_publish() (re_startup_mongo.py) → ZMQ PUB port (doc_port)
  → ZMQDocThread.doc_received signal
  → LiveViewer._on_doc()
  → pyqtgraph plot update (events arrive individually or as event_page)
```

### Scan data storage

```
RE Manager
  ├── _mongo_write() (re_startup_mongo.py)
  │     → MongoDB collections: run_start, run_stop, event_descriptor, event/event_page
  │     → MongoDataBrowserTab reads via pymongo on background QThreads
  └── _JSONLRunWriter (re_startup_mongo.py) — always-on, regardless of MongoDB
        → <exp_dir>/runs/<uid>.jsonl  (or ~/.easy_bluesky/data/runs/ fallback)
        → ExperimentsTab reads for HDF5 export when MongoDB is not configured
```

### Queue control

```
User clicks button
→ ZMQWorker method (e.g. queue_start(), execute_item())
→ ZMQ REQ/REP call to RE Manager control port
→ RE Manager acts
→ ZMQWorker.poll() detects state change
→ status_updated signal
→ QueueManager / REControlBar update
```

### Device CA monitoring (real mode)

```
RE environment opens (closed → idle)
→ _PVNamesReader calls get_device_pvnames() via function_execute
→ pv_names_ready(pv_map) signal → DevicesPlansTab.setup_epics_monitors()
→ pyepics ca.subscribe() with DBR_CTRL for each PV
→ CA callback (pyepics thread) → queued signal → _pending_pv_updates / _pending_desc_updates buffer
→ _pv_flush_timer fires every 100 ms → bulk-apply buffered updates → single tree repaint
```

Device list changes are fingerprinted (`_last_devices_fp`). If the fingerprint is unchanged (e.g. after a ZMQ reconnect without an environment restart), CA monitor teardown and rebuild are skipped entirely — avoiding unnecessary PV reconnections.

### Device monitoring (sim mode)

```
RE environment opens, pv_map is empty (all ophyd.sim devices)
→ QTimer fires every 2 s
→ poll_sim_values_requested signal → ZMQWorker.read_devices_status()
→ _DeviceStatusReader calls read_devices_status() via function_execute
→ update_sim_values(readings) → tree cell update
```

### Curve fitting (HDF5 Viewer and MongoDB Browser)

The fit dialog is **non-modal** and communicates with the viewer exclusively through Qt signals.

```
User clicks Fit button
→ viewer builds datasets list, stores in self._fit_datasets
→ viewer checks self._saved_fit_state for previous model/params
→ FitParamsDialog(x0, y0, model, bg_name, saved_params) created and shown
    → on showEvent: auto_guess() or saved_params → _update_param_table()
                    → _emit_preview() → preview_changed signal
    → viewer._on_fit_preview(x_fit, y_fit): draws dotted gold preview on plot

User edits a table cell
→ itemChanged → 400 ms debounce timer
→ timer fires → _emit_preview() → preview_changed
→ viewer._on_fit_preview: updates preview curve in place (setData)

User clicks Run Fit
→ _read_params_from_table() → run_fit(x, y, params, model, method, bg)
→ lmfit Model.fit()
→ table rows updated with fitted values (blockSignals)
→ preview_changed emitted with fitted x_fit, y_fit
→ results written to _results_txt (R², params ± errors, FWHM/width)
→ _btn_export enabled

User clicks Copy Results
→ _results_txt.toPlainText() → QApplication.clipboard()

User clicks Export Fit…
→ QFileDialog.getSaveFileName()
→ model evaluated at data x for residuals
→ CSV written: header comments (model, R², params) +
               Section 1 (x_data, y_data, y_fit, residual) +
               Section 2 (x_fit_smooth, y_fit_smooth)

User clicks Apply & Close
→ _read_params_from_table() → fit_applied(params, model, method, bg) emitted
→ dialog closes (accept)
→ viewer._on_fit_applied:
      _clear_fit_preview()
      _clear_fit_overlays()
      for each dataset: run_fit() → _add_fit_overlay() (dashed curve + annotation)
      _saved_fit_state = {model_name, bg_name, params}

User clicks Cancel / closes dialog
→ rejected signal → viewer._on_fit_cancelled → _clear_fit_preview()
```

`peak_fit.py` is a pure computation module (no Qt). `curve_fit_dialog.py` owns all Qt.
Composite models (`signal_model + background_model`) are constructed inside `run_fit()`
when `bg_name != "None"` — the caller passes a single flat `params` object for both.

`_saved_fit_state` persists on the viewer instance (not on disk). The next Fit click
restores the previous model, background, and all parameter values via `initial_params`.

### Motor move from plot (MongoDB Browser or Live Viewer)

```
User double-clicks plot
→ _on_plot_clicked() maps scene coords to view coords
→ QMessageBox confirmation (shows motor name + target + last scan position)
→ move_requested.emit(motor, position)         [MongoDB Browser]
   OR worker.execute_item({"name": "mv", ...}) [Live Viewer, direct]
→ ZMQ execute_item() → RE Manager runs mv(motor, position)
```

### RE console output

```
RE Manager stdout → log file (/tmp/re-manager-<slug>.log)
→ SSHLogTailer: SSH `tail -f` → line queue
→ ZMQWorker.poll() drains queue → console_line signal
→ REConsole widget appends line (color-coded)
```

## Key Design Decisions

### No suitcase.jsonl

Run data is written directly to MongoDB by `re_startup_mongo.py` (via `_mongo_write`, a plain pymongo callable). The old suitcase.jsonl serializer was removed. This eliminates the `suitcase-mongo-normalized` dependency and works with any MongoDB version.

`plans_log.jsonl` is kept as a fast local index (scan IDs, status, timestamps) for the Experiments tab plan log — it is not a full data store.

### RE console via SSH log tail

bluesky-queueserver 0.0.25 does not forward worker stdout over ZMQ console. `SSHLogTailer` runs `tail -n 50 -f <log_file>` over SSH and drains lines into the console widget on each poll tick. `BestEffortCallback` is subscribed in the startup script so live scan tables appear in the log and therefore in the RE Console tab.

### CA callbacks are thread-safe via queued signals

pyepics calls CA callbacks on a background CA thread. All callbacks immediately put data into a Python `queue.Queue` or emit a `pyqtSignal` with `Qt.ConnectionType.QueuedConnection`. Widget updates only happen on the Qt main thread — no mutex needed.

### Curve fitting uses lmfit, not scipy.optimize

`peak_fit.py` uses `lmfit` rather than `scipy.optimize.curve_fit` directly. lmfit wraps scipy minimisers but adds parameter bounds, fixed/free flags, derived expressions (e.g. `fwhm = 2.355 * sigma`), multiple minimisation methods, and composite model arithmetic (`Model + Model`). The auto-guess logic (`auto_guess()`) returns a fully configured `lmfit.Parameters` object that `FitParamsDialog` displays and the user can override before fitting. Background polynomials use `lmfit.models.PolynomialModel(prefix="bg_")` so their parameter names (`bg_c0`, `bg_c1`, …) never collide with signal parameters.

### JSONL always-on fallback

`re_startup_mongo.py` subscribes a `RunRouter`-based JSONL writer (`_jsonl_run_factory`) unconditionally — before the MongoDB section. Even when MongoDB is fully operational, every run produces a `<uid>.jsonl` file. This means the Experiments tab's **Export HDF5** always works without a database connection, and users can take JSONL files home on a USB drive. The JSONL writer tries `<exp_dir>/runs/` first (accessible over NFS on shared filesystems) and falls back to `~/.easy_bluesky/data/runs/` on the RE machine.

### Sim mode auto-detection

`setup_epics_monitors(pv_map)` checks whether `pv_map` contains any PV names. If all devices are `ophyd.sim` objects, `get_device_pvnames()` returns empty dicts, and the function switches to sim-polling mode automatically.

### MongoDB Browser auto-plot

The MongoDB Browser has no Plot button. The plot re-renders automatically via signal connections:

- `_run_table.itemSelectionChanged` → 180 ms `QTimer` debounce → `_MultiRunDataFetcher` → `_on_data_ready` → `_plot()`
- `_x_combo.currentIndexChanged` → `_auto_plot()`
- `_y_list.itemChanged` → `_auto_plot()`
- `_norm_combo.currentIndexChanged` → `_auto_plot()`
- `_log_y_cb.stateChanged` → `_auto_plot()`

`_y_list` signals are blocked during `_update_field_lists()` rebuilds to prevent spurious re-plots.

### `custom_plans.py` — beamline scan plans

`custom_plans.py` (repo root) is a standalone Python module of scan plans that is SFTPed to the RE Manager host on every restart alongside `re_startup_mongo.py`. It is **not** part of the `easy_bluesky` package; it runs entirely inside the RE worker environment.

Every plan wraps its body in `bpp.finalize_wrapper(_body(), _cleanup())` so the cleanup closure runs unconditionally on abort, pause-abort, or normal completion. The `_save_and_set_det_mode(detectors, hdf_autosave, saved)` helper populates a `saved` dict that maps signal objects (or `('_stage_sigs', obj, attr)` tuples for in-memory `stage_sigs` entries) to their original values; `_cleanup()` iterates this dict and restores each value via `bps.mv` or direct `stage_sigs` assignment.

`_set_image_mode_single(detectors)` is called at the top of every `per_step` / `per_shot` function, after staging has already run, to override any `'Multiple'` that an HDF5Plugin `stage()` implementation may have written directly to `cam.image_mode`.

### Smart legend positioning

`plot_tools.smart_legend_position(plot_widget)` is called at the end of `_plot()` / `_update_plot()` / `_replot()` in `live_viewer.py`, `mongo_browser.py`, and `hdf5_viewer.py`. It divides the current view into four quadrants, counts data points per quadrant (subsampled to 500 pts for performance), and calls `legend.setOffset()` with the corner offsets that correspond to the emptiest quadrant. Users can override the automatic position by dragging the legend (pyqtgraph 0.12+ legends are already draggable by default).

## Adding New Features

### New RE Manager command

Add a method to `ZMQWorker` in `worker.py`:

```python
def my_command(self, arg):
    try:
        r = self.rm.my_command(arg=arg)
        return r.get("success", False), r.get("msg", "")
    except Exception as e:
        return False, str(e)
```

### New plan parameter type

Add a branch in `ParamForm._make_widget()` in `widgets.py`.

### New tab

1. Create `easy_bluesky/my_tab.py` with a `QWidget` subclass
2. In `main.py`: `from .my_tab import MyTab`
3. In `MainWindow._setup_ui()`: `self.tabs.addTab(MyTab(), "My Tab")`

### New MongoDB Browser axis control

Connect the new control's change signal to `self._auto_plot` in `_build_ui()`. Block signals during `_update_field_lists()` if the control is rebuilt there.

### New startup script function

Add a plain Python function to `re_startup_mongo.py`. Call it from the app via:

```python
ok, result = self.worker.function_execute("my_function", args=[], kwargs={})
```

`function_execute` runs the callable in the RE worker namespace and returns the result synchronously.
