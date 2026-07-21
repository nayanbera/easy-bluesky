# EasyBluesky

A PyQt6 desktop application for controlling and monitoring Bluesky experiments via the bluesky-queueserver (ZMQ transport).

## Features

- **Experiments** — Create and manage experiments with sample metadata, plan log, motor/detector summaries, and overlay plotting.
- **Queue Manager** — Add, reorder, and delete plans. Full RE controls (open environment, start, pause, resume, abort, stop).
- **Plan Builder** — Auto-generated parameter forms for any allowed plan. Upload and run custom plans from a code editor.
- **Live Viewer** — Real-time pyqtgraph plots streamed over ZMQ. Crosshair cursor, point-hover tooltip, double-click motor move.
- **History Plot** — Browse completed runs. Multi-select overlay with common-column intersection.
- **HDF5 Viewer** — Open exported HDF5 archives, browse scans, overlay plots, view metadata.
- **RE Console** — Live console output from the RE Manager (color-coded for errors/warnings/success).
- **Instance Profiles** — Run multiple named RE Manager instances simultaneously (e.g. `ASWAXS`, `SURF`, `Sim`) each with its own device set and auto-assigned ports. Switch profiles from the toolbar.
- **Local Profiles** — Run RE Manager as a local subprocess with zero setup. Starts automatically when you launch the profile and stops when you close the app. Ideal for learning and testing with simulated devices.
- **Remote Control** — Start, stop, and restart any RE Manager instance on a remote host via SSH key authentication (no passwords stored).
- **Single-instance enforcement** — Only one app window per profile is allowed on the same computer. Profiles in use by another window are shown greyed out at startup.

---

## Architecture

EasyBluesky separates the **client** (this app) from the **RE Manager host**:

```
┌─────────────────────────────┐          ┌───────────────────────────────────┐
│   Client machine            │          │   RE Manager host                 │
│   (your laptop/workstation) │          │   (beamline control computer)     │
│                             │  ZMQ/TCP │                                   │
│   EasyBluesky app  ─────────┼──────────┼──► RE Manager (ASWAXS profile)   │
│      profile selector       │          │   RE Manager (SURF profile)       │
│                             │          │   RE Manager (Sim profile)        │
│   Needs:                    │          │                                   │
│   • easy-bluesky            │          │   Needs:                          │
│   • Python ≥ 3.10           │          │   • bluesky-queueserver           │
│                             │          │   • hardware ophyd drivers        │
│                             │          │   • startup scripts               │
└─────────────────────────────┘          └───────────────────────────────────┘
```

Each profile has its own ZMQ ports and devices file. The app connects to whichever profile is active in the toolbar dropdown.

The app does **not** need to be installed on the RE Manager host.

---

## Installation

### Client machine (the app)

```bash
git clone https://github.com/nayanbera/easy-bluesky.git
cd easy-bluesky

conda create -n easy-bluesky python=3.11
conda activate easy-bluesky
pip install -e .
```

Or from PyPI (once released):

```bash
pip install easy-bluesky
```

### RE Manager host

Only `bluesky-queueserver` and `pyepics` need to be installed — not the full EasyBluesky app:

```bash
pip install bluesky-queueserver pyepics
```

> **Startup scripts** (`re_startup_mongo.py`, YAML permission files) must also be present on the RE Manager host. See [Startup Scripts](#startup-scripts) below.

---

## Quick Start (local — same machine)

### 1. Launch the app

```bash
easy-bluesky
```

On first run the **Profile Picker** appears. A **Local Sim** profile is automatically created with simulated devices and free ports — no configuration needed. Select it and click **Launch**.

The app starts the RE Manager locally and connects automatically.

### 2. Try it out

- **Queue Manager** tab → add a `count` or `scan` plan using `det`, `motor1`, etc.
- **Live Viewer** tab → see real-time plots as plans run

### 3. Add your real hardware

Open `~/.easy_bluesky/scripts/devices.py` and add your ophyd devices:

```python
from ophyd import EpicsMotor

m1 = EpicsMotor("IOC:m1", name="m1")
m2 = EpicsMotor("IOC:m2", name="m2")
```

Then create a new profile (see [Instance Profiles](#instance-profiles)) that points to `devices.py`.

`devices.py` is only created on first run and is never overwritten by app updates.

### Manual RE Manager start (optional)

If you prefer to start the RE Manager yourself rather than using a Local profile:

```bash
EASY_BLUESKY_DEVICES_FILE=devices.py \
start-re-manager \
  --zmq-control-addr tcp://*:60615 \
  --zmq-info-addr    tcp://*:60625 \
  --zmq-publish-console ON \
  --startup-script   ~/.easy_bluesky/scripts/re_startup_mongo.py \
  --existing-plans-devices ~/.easy_bluesky/scripts/existing_plans_and_devices.yaml \
  --user-group-permissions ~/.easy_bluesky/scripts/user_group_permissions.yaml
```

Use **File → Connection Settings** to configure the host, create profiles, or change ports.

---

## Toolbar Overview

The persistent toolbar at the top provides:

| Button / Control | Action |
|-----------------|--------|
| Profile dropdown | Switch the active RE Manager instance (profile) |
| ▶ Start | Start the plan queue |
| ⏸ Pause / ▶▶ Resume | Pause / resume running plan |
| ✕ Abort / ⬛ Stop | Abort or stop the running plan |
| Open Env / Close Env | Open or close the RE worker environment |
| ⚡ Start RE Mgr | Start (or restart) the active profile's RE Manager |
| ⏹ Stop RE Mgr | Stop the active profile's RE Manager |
| ↺ Reconnect | Reconnect ZMQ without restarting RE Manager |

---

## Instance Profiles

Profiles let you run **multiple RE Manager instances simultaneously**, each with its own set of devices and ZMQ ports. You can name them after your techniques, modes, or sample environments — for example `ASWAXS`, `SURF`, or `Sim`.

### Profile Picker (startup dialog)

Every time you launch EasyBluesky, the **Profile Picker** appears before the main window:

```
┌─────────────────────────────────────────────┐
│  EasyBluesky — Select Profile               │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │  Local Sim  [LOCAL]                 │    │
│  │  ASWAXS                             │    │
│  │  SURF  (already running)            │ ← greyed, locked by another window
│  └─────────────────────────────────────┘    │
│                                             │
│  [Restore Deleted…] [New Profile] [Delete]  │
│                          [Cancel] [Launch]  │
└─────────────────────────────────────────────┘
```

- **`[LOCAL]`** — profile runs RE Manager locally on this computer
- **`(already running)`** — profile is open in another window; greyed out and unselectable
- **New Profile** — create a profile without opening Connection Settings
- **Delete** — requires typing the profile name to confirm (see [Deleting profiles](#deleting-profiles))
- **Restore Deleted…** — recover a recently deleted profile

On **first run**, a `Local Sim` profile is created automatically — just click Launch.

### One app per profile (single-instance enforcement)

Only one EasyBluesky window can run a given profile at a time on the same computer. If you try to switch to a profile already held by another window (via the toolbar dropdown), the switch is blocked and a warning is shown.

To run two profiles simultaneously, launch the app twice and pick a different profile in each window.

### Local profiles

A **Local** profile runs the RE Manager as a subprocess on the same machine as the app:

- RE Manager **starts automatically** when you launch into the profile
- RE Manager **stops automatically** when you close the app (also on crash via `atexit`)
- No SSH, no procServ, no configuration needed
- Choose any devices file — including `devices_sim.py` for a zero-setup simulation

To create a local profile, click **New Profile** in the picker (or **Add Profile** in Connection Settings) and check **Local (runs on this computer)**.

> **First-run default:** the auto-created `Local Sim` profile is local, uses `devices_sim.py`, and gets free ports automatically — nothing to configure.

### Remote profiles

A **Remote** profile connects to an RE Manager running on another machine via SSH + procServ. See [Remote RE Manager](#remote-re-manager).

### Creating a profile

**From the Profile Picker** (quickest):
1. Click **New Profile**
2. Enter a name (e.g. `SURF`)
3. Check **Local** if running on this machine, or leave unchecked for remote
4. Set the devices file (e.g. `devices_surf.py`)
5. Click OK — ports are auto-assigned

**From Connection Settings** (full control):
1. Open **File → Connection Settings**
2. In the **Profiles** pane, click **＋ Add**
3. Fill in name, devices file, and local/remote toggle
4. Click **Auto-assign Ports**
5. Click OK

### Switching profiles

Select a profile from the dropdown in the toolbar. The app immediately attempts to connect to that profile's RE Manager. If it is not yet running, a message appears in the status bar — click **⚡ Start RE Mgr** to start it.

### Deleting profiles

Select a profile in the picker and click **Delete**. A confirmation dialog requires you to **type the profile name exactly** before deletion is allowed — preventing accidental deletes.

Deleted profiles are kept for **30 days** (up to 20 entries) and can be recovered via **Restore Deleted…** in the picker. Ports are auto-reassigned on restore if the originals are now in use.

The last remaining profile cannot be deleted.

### Port layout

Each profile has four ports, all auto-assigned by default:

| Port field | Purpose |
|-----------|---------|
| Control port | ZMQ REQ/REP — sends commands to RE Manager |
| Info port | ZMQ PUB — status/event stream from RE Manager |
| Doc port | ZMQ PUB — live document stream for Live Viewer |
| procServ port | procServ management socket (remote profiles only) |

> **Port conflicts are resolved automatically.** On every load and save, the app scans all profiles for duplicate port numbers and reassigns any duplicates — profiles earlier in the list keep their ports, later ones get bumped to the next free port. No manual intervention needed. Using **Auto-assign Ports** when creating profiles is still recommended to start with clean, non-overlapping ports.

### Devices file per profile

Each profile loads a separate Python file of device definitions via the `EASY_BLUESKY_DEVICES_FILE` environment variable, which is passed to the RE Manager subprocess so `re_startup_mongo.py` imports the right file.

Example layout for two technique profiles:

```
~/.easy_bluesky/scripts/
├── devices.py          ← default hardware (never overwritten)
├── devices_surf.py     ← SURF-specific devices
├── devices_sim.py      ← simulated devices (auto-generated)
└── re_startup_mongo.py ← shared startup script (all profiles use this)
```

### Configuration migration

If you had a previous EasyBluesky installation with separate real/sim port fields, those settings are automatically migrated:

- Real ports → **Default** profile
- Sim ports → **Sim** profile (if sim ports were configured)

Existing profiles from the named-profiles release get `is_local: false` backfilled automatically. No manual editing of `connection.json` is needed.

---

## Simulated Devices

### Generate a simulated devices file

**File → Generate Sim Devices…** reads your real `devices.py` (and `re_startup_mongo.py`) and auto-generates `devices_sim.py`:

- `EpicsMotor` → `SynAxis`
- Area detectors → `SimAreaDetector` (Poisson-noise images)
- Scalers/counters → `SynGauss`
- Generic test devices always included: `motor1`, `motor2`, `det`, `det1`, `det2`, `sim_ad`
- Separate device list file (`existing_plans_and_devices_sim.yaml`) so real and sim don't overwrite each other

Example generated content:

```python
from ophyd.sim import SynAxis, SynGauss

# Auto-mapped from real script
m1 = SynAxis(name='m1')

# Generic sim devices (always included)
motor1 = SynAxis(name='motor1')
motor2 = SynAxis(name='motor2')
det    = SynGauss('det',  motor1, 'motor1', center=0, Imax=1000, sigma=0.5)
det1   = SynGauss('det1', motor1, 'motor1', center=0, Imax=500,  sigma=1.0)
det2   = SynGauss('det2', motor2, 'motor2', center=0, Imax=800,  sigma=0.5)
sim_ad = SimAreaDetector(name='sim_ad')
```

When the host is **remote**, the dialog offers to **copy the generated file directly to the RE Manager host** via SFTP — no manual `scp` needed.

### Running a sim profile

1. Create a profile named `Sim` (or any name) and set its **Devices file** to `devices_sim.py`.
2. Click **Auto-assign Ports** to get ports that don't conflict with real profiles.
3. Select the `Sim` profile in the toolbar.
4. Click **⚡ Start RE Mgr** to launch it.

Both real and sim instances run simultaneously — switching profiles in the toolbar reconnects without stopping anything.

### Conda environments

If the RE Manager must run inside a specific conda environment, set **Conda env** and **Conda path** in Connection Settings. The app constructs the full binary path directly — no `conda activate` needed at runtime:

```
{conda_path}/envs/{conda_env}/bin/start-re-manager
```

---

## Remote RE Manager

### Connection settings

Open **File → Connection Settings** and set:
- **Host / IP** — hostname or IP of the RE Manager machine
- **Profiles** — one profile per RE Manager instance, each with its own ports and devices file

The app reconnects immediately after clicking OK.

### Remote restart via SSH (key auth — no passwords)

**⚡ Start RE Mgr** and **⏹ Stop RE Mgr** SSH into the remote host to manage the RE Manager. No passwords are stored or committed to git — only the **path** to your private key is saved in `~/.easy_bluesky/connection.json` (a local file, never in the repo).

#### One-time SSH setup

**1. Generate an SSH key pair** on your client machine (skip if you already have one):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

**2. Copy the public key to the RE Manager host:**

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-beamline-host
```

**3. Verify passwordless login:**

```bash
ssh -i ~/.ssh/id_ed25519 user@your-beamline-host echo ok
```

> **Note:** SSH key authentication requires that your home directory on the remote host is **not** group- or world-writable. If key auth is rejected, run `chmod go-w ~` on the remote machine.

**4. In the app**, open **File → Connection Settings → Remote SSH Management**:

| Field | Example | Notes |
|-------|---------|-------|
| SSH user | `beamline` | Username on the remote machine |
| SSH port | `22` | Default SSH port |
| Private key | `~/.ssh/id_ed25519` | Local path only — never committed |
| Service name | | systemd service name, or leave empty for procServ |
| Conda env | `easy-bluesky` | Conda environment name on the remote host |
| Conda path | `~/anaconda3` | Base conda install directory on the remote host |

Click **Test SSH Connection** to verify — it also checks that `start-re-manager` exists in the configured conda env and reports the procServ version.

### procServ (recommended for remote hosts)

When `procServ` is available on the remote host, **⚡ Start RE Mgr** uses it automatically. procServ is an EPICS process manager that:

- Daemonizes the child process — survives SSH session close regardless of systemd-logind settings
- Writes a PID file for clean shutdown
- Logs RE Manager output to `/tmp/re-manager-<profile>.log`
- Falls back to `systemd-run --user --scope` or `nohup` if procServ is not found

procServ is available at most synchrotron beamlines. To check:

```bash
which procServ && procServ --version
```

To install (RHEL/CentOS):

```bash
sudo yum install procServ
```

### How remote start/stop works

With the host set to a non-localhost IP and SSH configured, **⚡ Start RE Mgr**:

1. Writes a launcher shell script to `/tmp/_easy_bluesky_<profile>.sh` via SFTP
2. Kills the existing instance for this profile only (via procServ PID file — other profiles are unaffected)
3. Launches `procServ ... /bin/bash /tmp/_easy_bluesky_<profile>.sh`
4. Waits (polling every 2 s) until the ZMQ control port opens, then reconnects

The launcher script exports `EASY_BLUESKY_DEVICES_FILE=<profile's devices file>` so `re_startup_mongo.py` loads the right devices.

**⏹ Stop RE Mgr** kills only the active profile's instance (via its PID file), leaving all other profiles running.

Profile names are slugified for filenames (lowercase, spaces → underscores). For example:
- Profile `ASWAXS` → `/tmp/_easy_bluesky_aswaxs.sh`, `/tmp/re-manager-aswaxs.log`
- Profile `SURF` → `/tmp/_easy_bluesky_surf.sh`, `/tmp/re-manager-surf.log`

#### Service name field (systemd alternative)

If you have a systemd user service set up, enter its name (e.g. `re-manager-aswaxs`) in the **Service name** field. The app will use `systemctl --user restart/stop <service>` instead of procServ.

#### Remote startup scripts

The startup scripts must exist on the RE Manager host at `~/.easy_bluesky/scripts/`. Copy them once:

```bash
# From the client machine
scp ~/.easy_bluesky/scripts/re_startup_mongo.py \
    ~/.easy_bluesky/scripts/existing_plans_and_devices.yaml \
    ~/.easy_bluesky/scripts/user_group_permissions.yaml \
    user@your-beamline-host:~/.easy_bluesky/scripts/
```

The sim devices file can be copied automatically via **File → Generate Sim Devices… → Copy to Remote?**.

---

### Running as a systemd service (optional, for production)

Service templates are provided at `~/.easy_bluesky/scripts/`.

**1. Find the full path to `start-re-manager` in your environment:**

```bash
conda activate bluesky
which start-re-manager
```

**2. Edit the templates** — replace `YOUR_USER` and `/path/to/start-re-manager`:

```bash
nano ~/.easy_bluesky/scripts/re-manager-real.service
nano ~/.easy_bluesky/scripts/re-manager-sim.service
```

**3. Install and enable:**

```bash
mkdir -p ~/.config/systemd/user
cp ~/.easy_bluesky/scripts/re-manager-real.service ~/.config/systemd/user/
cp ~/.easy_bluesky/scripts/re-manager-sim.service  ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now re-manager-real
systemctl --user enable --now re-manager-sim
```

**4. Allow services to survive logout** (run once with sudo):

```bash
sudo loginctl enable-linger YOUR_USER
```

**5. Useful commands:**

```bash
systemctl --user status re-manager-real
systemctl --user restart re-manager-real
journalctl --user -u re-manager-real -f    # live logs
```

---

## Startup Scripts

Scripts live at `~/.easy_bluesky/scripts/` and are auto-created on first run.

### `devices.py` — the only file you need to edit

Add all your ophyd/EPICS hardware here:

```python
from ophyd import EpicsMotor, EpicsSignal

m1  = EpicsMotor("IOC:m1", name="m1")
m2  = EpicsMotor("IOC:m2", name="m2")
det = EpicsSignal("IOC:det", name="det")
```

This file is:
- **Never overwritten** by app updates — safe to edit freely
- **Imported automatically** by `re_startup_mongo.py` via the `EASY_BLUESKY_DEVICES_FILE` env var
- **Parsed by the sim generator** — `File → Generate Sim Devices…` reads `devices.py` and maps each device to its simulated equivalent

You can also split hardware across multiple files and import them from `devices.py`. When using multiple profiles, create a separate devices file for each (e.g. `devices_surf.py`, `devices_aswaxs.py`).

### `re_startup_mongo.py` — do not edit

Handles RE setup, data routing (suitcase.jsonl), and ZMQ doc publishing. Reads `EASY_BLUESKY_DEVICES_FILE` from the environment to decide which devices file to load. **All profiles share this single startup script** — no per-profile startup scripts needed.

### `devices_sim.py` — simulation devices

Auto-generated by **File → Generate Sim Devices…**. Contains simulated equivalents of your real devices plus generic test devices. Referenced by a `Sim` profile's **Devices file** field.

### YAML permission files

- `existing_plans_and_devices.yaml` — device/plan list for the Default profile (auto-updated when environment opens)
- `existing_plans_and_devices_sim.yaml` — device/plan list for the Sim profile (kept separate so real and sim don't overwrite each other)
- `user_group_permissions.yaml` — controls which user groups can run which plans

---

## Configuration Reference

Connection settings are stored in `~/.easy_bluesky/connection.json` (local only, never committed to git):

```json
{
  "host": "192.168.1.50",
  "ssh_user": "beamline",
  "ssh_port": 22,
  "ssh_key_path": "~/.ssh/id_ed25519",
  "ssh_service": "",
  "conda_env": "easy-bluesky",
  "conda_path": "~/anaconda3",
  "active_profile": "ASWAXS",
  "profiles": [
    {
      "name": "ASWAXS",
      "devices_file": "devices_aswaxs.py",
      "is_local": false,
      "control_port": 60615,
      "info_port": 60625,
      "doc_port": 60630,
      "procserv_port": 60635
    },
    {
      "name": "SURF",
      "devices_file": "devices_surf.py",
      "is_local": false,
      "control_port": 60640,
      "info_port": 60641,
      "doc_port": 60642,
      "procserv_port": 60643
    },
    {
      "name": "Local Sim",
      "devices_file": "devices_sim.py",
      "is_local": true,
      "control_port": 60644,
      "info_port": 60645,
      "doc_port": 60646,
      "procserv_port": 60647
    }
  ],
  "deleted_profiles": []
}
```

Environment variable overrides:

| Variable | Default | Description |
|---|---|---|
| `BLUESKY_ZMQ_CONTROL` | `tcp://localhost:60615` | RE Manager control address |
| `BLUESKY_ZMQ_INFO` | `tcp://localhost:60625` | RE Manager info address |
| `BLUESKY_ZMQ_PUB_HOST` | `localhost` | Live doc stream host |
| `BLUESKY_ZMQ_PUB_PORT` | `60630` | Live doc stream port |

---

## Data Storage

Runs are written as JSONL files using [suitcase-jsonl](https://blueskyproject.io/suitcase-jsonl/):

```
experiments/<timestamp>_<name>/
├── experiment.json       # experiment metadata
├── plans_log.jsonl       # plan execution log (scan numbers, status)
├── runs/                 # one JSONL file per scan UID
└── samples/<name>/       # sample-specific subfolders
```

Use **Export HDF5…** to bundle all runs into a single portable `.h5` file.

---

## Project Structure

```
easy-bluesky/
├── easy_bluesky/
│   ├── main.py               # MainWindow + entry point
│   ├── worker.py             # ZMQ worker thread (RE Manager API)
│   ├── config.py             # Configuration constants (env-overridable)
│   ├── connection_settings.py# Connection dialog + settings I/O + profiles
│   ├── ssh_manager.py        # SSH-based remote RE Manager control (procServ)
│   ├── sim_generator.py      # Auto-generate sim devices file from real script
│   ├── re_control_bar.py     # RE control toolbar (status + buttons + profile dropdown)
│   ├── re_console.py         # RE console output tab
│   ├── experiments_tab.py    # Experiments tab (plan log, plots, HDF5 export)
│   ├── queue_manager.py      # Queue Manager tab
│   ├── plan_builder.py       # Plan Builder tab + code editor
│   ├── widgets.py            # Shared widgets (ScanArgsWidget, ParamForm, …)
│   ├── live_viewer.py        # Live Viewer (ZMQ + pyqtgraph)
│   ├── hdf5_viewer.py        # HDF5 Viewer tab
│   ├── devices_plans_tab.py  # Devices & Plans tab
│   ├── themes.py             # Theme definitions + stylesheet builder
│   └── scripts/              # Bundled default scripts (copied to ~/.easy_bluesky/scripts/)
│       ├── devices.py            ← edit this to add hardware (never overwritten)
│       ├── re_startup_mongo.py   ← shared startup script (all profiles use this)
│       ├── existing_plans_and_devices.yaml
│       ├── user_group_permissions.yaml
│       ├── start_re_managers.sh
│       ├── stop_re_managers.sh
│       ├── re-manager-real.service
│       └── re-manager-sim.service
├── pyproject.toml
└── README.md
```

---

## Acknowledgements

Developed with assistance from [Claude](https://claude.ai) (Anthropic).

## License

BSD 3-Clause License
