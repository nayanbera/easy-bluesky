# EasyBluesky — Claude Context

## Commit rules

- **Never** add `Co-Authored-By:` trailers to commit messages. GitHub parses them as
  co-authorship and lists Claude as a contributor on the project page.

EasyBluesky is a PyQt6 desktop GUI for controlling a Bluesky/ophyd beamline via the
bluesky-queueserver. It targets synchrotron beamlines (current user: ASWAXS beamline,
`Pil300K` Pilatus area detector). The app runs locally on a Mac; the RE Manager runs
remotely on a Linux beamline computer (`chem_epics` user).

## Architecture

```
Mac (local app)                    Linux beamline computer (remote)
─────────────────                  ─────────────────────────────────
easy_bluesky/ (PyQt6 GUI)   SSH    bluesky-queueserver v0.0.25
  main.py          ──────────────→   start-re-manager (procServ-managed)
  worker.py (ZMQ)  ←── ZMQ ──────   ports: ctrl=60615, info=60625
  ssh_manager.py   ──SSH log tail──  log: /tmp/re-manager-<slug>.log
                                     startup: ~/.easy_bluesky/scripts/re_startup_mongo.py
                                     devices: ~/.easy_bluesky/scripts/devices_ASWAXS.py
                                              ~/.easy_bluesky/scripts/devices_sim.py (sim)
```

## Key files

| File | Purpose |
|---|---|
| `easy_bluesky/main.py` | Main window, tab setup, signal wiring |
| `easy_bluesky/worker.py` | `ZMQWorker` — background thread: polls RE Manager status, drains console, emits Qt signals |
| `easy_bluesky/ssh_manager.py` | SSH-based start/stop of remote RE Manager via paramiko |
| `easy_bluesky/queue_manager.py` | Queue list, history list, plan detail panel |
| `easy_bluesky/experiments_tab.py` | Experiment creation, plan log, data browser |
| `easy_bluesky/connection_settings.py` | Profile schema, settings file `~/.easy_bluesky/connection.json` |
| `easy_bluesky/config.py` | Path constants (`EXPERIMENTS_DIR`, `ACTIVE_EXPERIMENT_FILE`, etc.) |
| `easy_bluesky/devices_plans_tab.py` | Available Devices tree + Available Plans panel |
| `easy_bluesky/sim_generator.py` | Parses real devices file → generates `devices_sim.py` |
| `easy_bluesky/scripts/re_startup_mongo.py` | Remote RE Manager startup script — auto-uploaded on every restart |

## Connection settings (local only, never committed)

Stored at `~/.easy_bluesky/connection.json`. Key fields per profile:

```json
{
  "profiles": [{
    "name": "Default",
    "host": "<beamline-host>",
    "ssh_user": "<user>",
    "ssh_key_path": "~/.ssh/id_ed25519",
    "conda_env": "easy-bluesky",
    "conda_path": "~/anaconda3",
    "devices_file": "devices_ASWAXS.py",
    "control_port": 60615,
    "info_port": 60625,
    "procserv_port": 60635,
    "epics_ca_addr_list": "",
    "epics_ca_auto_addr_list": true
  }]
}
```

- **No passwords ever** — SSH key auth only.
- `devices_file` is a bare filename (e.g. `devices_ASWAXS.py`) — resolved relative to
  `~/.easy_bluesky/scripts/` on the remote machine. Absolute paths also work.
- `epics_ca_addr_list` / `epics_ca_auto_addr_list` set `EPICS_CA_ADDR_LIST` /
  `EPICS_CA_AUTO_ADDR_LIST` in the local process before pyepics initialises libca.
  Applied by `apply_epics_env()` in `connection_settings.py` at startup and on
  settings change.

## Remote RE Manager lifecycle

`ssh_manager.restart_re_manager()`:
1. Opens SSH, detects remote `$HOME`, creates `~/.easy_bluesky/scripts/` if needed.
2. SFTPs the local `re_startup_mongo.py` and `user_group_permissions.yaml` from the
   **package** `easy_bluesky/scripts/` to the remote scripts dir (always uploads latest).
3. If `~/.easy_bluesky/scripts/devices_sim.py` exists locally, SFTPs it to remote too
   (auto-syncs without a separate "Copy to Remote?" step).
4. Writes a launcher shell script to `/tmp/_easy_bluesky_<slug>.sh` via SFTP.
   The launcher exports `EASY_BLUESKY_DEVICES_FILE=<devices_file>` before starting
   `start-re-manager`.
5. **Stop** (separate SSH channel): kills by pid file + `pkill -f start-re-manager`.
   The channel may self-terminate (pkill matches the ssh bash process) — that's expected.
6. `time.sleep(2)` between stop and start.
7. **Start** (fresh SSH channel): launches via `procServ` (preferred) or `nohup+setsid`.

## Simulation profile (`devices_sim.py`)

- **Generate**: `File → Generate Sim Devices` parses the active profile's real devices
  file (e.g. `devices_ASWAXS.py`) and writes `~/.easy_bluesky/scripts/devices_sim.py`.
  If the active profile already points at `devices_sim.py` (circular), the generator
  searches other profiles and the scripts directory for a non-sim devices file.
- **Upload**: The generated file is SFTPed to the remote immediately (dialog) and also
  auto-uploaded on every subsequent RE Manager restart.
- **Devices file**: the sim profile's `devices_file` must be `devices_sim.py` (not
  `device_sim.py` — note the `s`).
- **SynGauss caveat**: do **not** use `noise='poisson'` in `SynGauss` calls. Newer
  numpy returns a 1-element array from `random_state.poisson(..., 1)` and `int()` fails.
  The generator omits `noise=` entirely.

## Available Devices tab

`devices_plans_tab.py` — `DevicesPlansTab`:

### Tree columns
`["Device / Signal", "Class", "Value", "Units", "Description", "Tweak"]`

- **Class**: ophyd classname from `devices_allowed()` (e.g. `EpicsMotor`, `SynAxis`).
- **Value / Units**: live CA readback via pyepics DBR_CTRL subscriptions (real mode) or
  polled via `read_devices_status()` every 2 s (sim mode).
- **Description**: EPICS `.DESC` field, fetched by subscribing to `{record_base}.DESC`.
  Field suffixes are stripped before appending `.DESC` — e.g. `IOC:M1.RBV` →
  `IOC:M1.DESC`, not `IOC:M1.RBV.DESC`.
- **Tweak**: motor devices (`user_setpoint` signal present) get `[◀][step][▶]` inline
  widget. Mouse-wheel on the step spinbox is disabled.

### EPICS monitoring flow (real mode)
1. `update_devices(devices)` populates group/device rows, then auto-emits
   `fetch_pvnames_requested` → `worker.fetch_device_pvnames()`.
2. `_PVNamesReader` calls `get_device_pvnames()` in the RE environment via
   `function_execute` and emits `pv_names_ready(pv_map)`.
3. `setup_epics_monitors(pv_map)` adds signal sub-rows and starts CA subscriptions.
4. CA callbacks (`value_changed`, `connection_changed`, `desc_changed`) update tree
   cells. Units and descriptions are also cached to
   `~/.easy_bluesky/device_metadata.json` for reuse in sim mode.

### Sim monitoring flow
Triggered when `pv_map` has zero total PVs (all devices are `ophyd.sim` objects):
1. `setup_epics_monitors` detects sim mode, adds Tweak widgets for `SynAxis` devices,
   starts a 2-second `QTimer`.
2. On each tick, `poll_sim_values_requested` → `worker.read_devices_status()` →
   `_DeviceStatusReader` calls `read_devices_status()` in the RE environment.
3. `update_sim_values(readings)` fills the Value column from the readings dict, and
   populates Units / Description from the cached `device_metadata.json` if available.
4. Tweak buttons emit `set_sim_device_requested(dev_name, value)` →
   `worker.set_sim_device()` → calls `set_sim_device(name, value)` in the RE
   environment via `function_execute` (blocks until `device.set().wait()` completes).

### Search
A search bar above the tree filters device rows by name, class, or description as the
user types. Group headers hide when all their children are filtered out.

### State reset
`update_devices({})` (empty devices — e.g. RE Manager crash) stops the sim timer,
clears CA subscriptions, resets the status label, and disables the Reconnect button.

## `re_startup_mongo.py` functions

Callable via `function_execute` from the app:

| Function | Purpose |
|---|---|
| `get_device_pvnames()` | Returns `{dev_name: {sig_name: pvname}}` for all EPICS signals |
| `read_devices_status()` | Returns `{dev_name: {connected, kind, reading, error}}` — used by sim monitor |
| `set_sim_device(name, value)` | Calls `device.set(value).wait()` on a sim device — used by sim tweak |
| `get_device_pvnames()` | Used to detect sim mode (returns empty dicts for ophyd.sim devices) |

## `re_startup_mongo.py` subscriptions (in order)

1. `suitcase.jsonl` via `RunRouter` — writes JSONL run files
2. `BestEffortCallback` — prints live scan table to stdout/log
3. ZMQ PUB socket on port 60630 — for Live Viewer tab

## Console output (RE console tab)

bluesky-queueserver v0.0.25 does **not** forward worker stdout to ZMQ.
Fix: `_SSHLogTailer` in `worker.py` runs `tail -n 50 -f <log_file>` over SSH and
drains lines into the console widget via a `Queue`. The poll loop drains both ZMQ and
SSH tailer on every tick.

`BestEffortCallback` is subscribed in `re_startup_mongo.py` so live scan tables appear
in the log and therefore in the RE console widget.

## Data flow

- The local app injects `exp_dir` (the active experiment's local path) into every
  plan's `md` kwargs before adding it to the queue.
- `re_startup_mongo._jsonl_factory` reads `doc["exp_dir"]` from the start document
  and writes run JSONL files into `<exp_dir>/runs/`. This works over NFS/shared
  filesystems. Falls back to `~/.easy_bluesky/data/runs/` on the remote machine
  when the path is inaccessible.
- The experiments tab `update_history` reads the queue server history API, matches
  completed plans to JSONL run files via `_find_run_file_for_entry`, and appends
  entries to `<exp_dir>/plans_log.jsonl`.

## Queue Manager — history list

`queue_manager.py` — `QueueManager`:

- **Single-click** on a history item populates `detail_text` via `_on_history_selection` (same as queue list).
- **Double-click** (`_requeue_from_history_dialog`): if plan name is in `self.plans` → opens `PlanDialog` for editing; if not (RE environment closed or plan not allowed) → shows a Yes/No prompt and adds the original item directly via `worker.add_item`.
- **Right-click context menu**: `Edit & Re-queue` (PlanDialog), `Re-queue directly` (`_direct_requeue` — adds original item, no dialog), `View Details` (opens `RunDetailDialog`).
- `RunDetailDialog` has its own Re-queue button that also opens `PlanDialog`.

## MongoDB Browser — notes

`mongo_browser.py` — `MongoDataBrowserTab`:

### Experiment filter
- `set_active_experiment(exp_dir)` is called by `main._on_experiment_changed`. It unchecks "All runs" and calls `_fetch_runs`.
- `_RunListFetcher` query strategy: if `run_uids` (from `plans_log.jsonl`) are non-empty → filter by UID only (no regex, to avoid matching other experiments with the same folder name). Fallback (empty log): `exp_dir` regex on the last two path components.

### Derivative transform
- Bottom bar (below plot, beside crosshair label): `Log Y | ± Errors | Deriv: [— / dy/dx / d²y/dx²]`
- Implemented via `np.gradient` (central differences) — result has the same N points at the same x positions, no shift.
- Applied in `_auto_plot` after normalization and before log transform.
- Error propagation: exact central-difference formula `σ_dy[i] = √(σ[i+1]² + σ[i−1]²) / (x[i+1] − x[i−1])`; 2nd order iterates the formula.
- Fit overlays: raw fit data cached in `_fit_items_cache`; `_auto_plot` clears stale fit items from the plot and redraws from cache with the current log/deriv settings on every replot.

## Experiments tab — scan numbering

- **`_next_scan_num` / `_base_next_scan_num`**: `_load_plan_log` sets both from
  `plans_log.jsonl` entry count (`scan_counter`). `_base_next_scan_num` is the floor
  (completed scans + 1) and never changes during a session. `_next_scan_num` is
  advanced past queued plan scan numbers in `update_compact_queue` every poll.
- **scan_num injection**: `_inject_metadata` locks `scan_num = _next_scan_num` into
  each plan's `md` at queue time, then increments `_next_scan_num` and updates the
  label. `custom_plans.py` uses `md.get("scan_num") or _scan_num_from_log(_dir)`.
- **"Now Running" banner**: `ZMQWorker.running_item_updated(dict)` emits
  `queue["running_item"]` each poll. `ExperimentsTab.update_running_item` shows/hides
  the green banner. `update_re_status` hides it when `re_state` is not running/paused.
- **Start queue guard**: `update_re_status` checks `manager_state == "idle"` in
  addition to `re_state` before enabling `btn_q_start`.
- **directoryChanged NOT connected to `_update_next_scan_label`**: removed because
  `_load_plan_log` rewrites `plans_log.jsonl`, triggering `directoryChanged` which was
  resetting the label to the stale `scans_log.json` count.
- **`custom_plans.py` is uploaded manually** — never auto-deploy it; the user uploads
  it to the remote machine themselves.

## MongoDB Browser — curve fitting

- **Per-dataset fit memory**: `_saved_fit_states: dict` maps
  `fit_key → {model_name, bg_name, params}`. Key = `(sorted UIDs, stream, x_field,
  sorted y_fields)`. On dialog open with saved state, `QTimer.singleShot(0, _run_fit)`
  auto-runs the fit to restore the full results table and curve overlay.

## Known issues / non-obvious decisions

- **`pkill` self-kill**: `pkill -f start-re-manager` matches the bash process running
  the SSH command (pattern appears in cmdline). Fix: stop and start use two separate
  `exec_command` channels; the stop channel may die early, which is harmless.

- **Device list empty after env open**: `_load_plans_devices()` was only called on
  connect, not when env transitions from closed→idle. Fix: `poll()` tracks
  `_prev_env_state` and calls `_load_plans_devices()` on the `closed→idle` transition.

- **`devices_file` path**: use a bare filename (e.g. `devices_ASWAXS.py`), not a full
  path. The startup script resolves it relative to `~/.easy_bluesky/scripts/`.
  Absolute paths also work (via `importlib.util.spec_from_file_location`).

- **Area detector priming**: `hdf1` plugin must be warmed up once per IOC session
  before scanning. Add `prime_detector(Pil300K)` to the queue and run it first.
  (Defined in `re_startup_mongo.py`.)

- **Plan summary display**: `_plan_summary()` is duplicated in `queue_manager.py` and
  `experiments_tab.py` (both as `@staticmethod`). Changes must be made in both files.

- **DESC subscription field stripping**: `_EPICSMonitor` strips any field suffix from
  a PV name before appending `.DESC`. `IOC:M1.RBV` → `IOC:M1.DESC`. Without this,
  `IOC:M1.RBV.DESC` is an invalid CA address and the DESC column stays empty.

- **`_desc_map` is a list**: multiple signals on the same record share one DESC PV.
  `_desc_map` maps `desc_pvname → list[(dev_name, sig_name)]`, not a single tuple.

- **Sim profile `devices_file` typo**: profile must have `"devices_file": "devices_sim.py"`
  (with the `s`). A typo (`device_sim.py`) causes RE Manager to fail with ImportError
  and AVAILABLE DEVICES to be empty.

## Running locally

```bash
pip install -e .
python -m easy_bluesky
# or
./launch.sh
```
