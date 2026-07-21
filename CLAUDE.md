# EasyBluesky — Claude Context

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
    "procserv_port": 60635
  }]
}
```

- **No passwords ever** — SSH key auth only.
- `devices_file` is a bare filename (e.g. `devices_ASWAXS.py`) — resolved relative to
  `~/.easy_bluesky/scripts/` on the remote machine. Absolute paths also work.

## Remote RE Manager lifecycle

`ssh_manager.restart_re_manager()`:
1. Opens SSH, detects remote `$HOME`, creates `~/.easy_bluesky/scripts/` if needed.
2. SFTPs the local `re_startup_mongo.py` to the remote scripts dir (always uploads latest).
3. Writes a launcher shell script to `/tmp/_easy_bluesky_<slug>.sh` via SFTP.
4. **Stop** (separate SSH channel): kills by pid file + `pkill -f start-re-manager`.
   The channel may self-terminate (pkill matches the ssh bash process) — that's expected.
5. `time.sleep(2)` between stop and start.
6. **Start** (fresh SSH channel): launches via `procServ` (preferred) or `nohup+setsid`.

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

## `re_startup_mongo.py` subscriptions (in order)

1. `suitcase.jsonl` via `RunRouter` — writes JSONL run files
2. `BestEffortCallback` — prints live scan table to stdout/log
3. ZMQ PUB socket on port 60630 — for Live Viewer tab

## Running locally

```bash
pip install -e .
python -m easy_bluesky
# or
./launch.sh
```
