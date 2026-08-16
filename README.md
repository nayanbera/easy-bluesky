# EasyBluesky

A PyQt6 desktop application for controlling and monitoring Bluesky experiments via the bluesky-queueserver (ZMQ transport).

## Features

- **Experiments** — Create and manage experiments with sample metadata and plan log. Supports ESAF-based folder structure (`PI group / ESAF / run`) or free-form manual paths. A live ESAF server health indicator (coloured dot) shows connection status at a glance. The Plan Log header shows a **Next scan: #N** label (recalculates on every queue change). A **"Now Running" banner** (green, above the Plan Log) shows the active plan name, scan number, and sample name while a scan is executing. Changing the sample name offers to update all already-queued plans in one step. scan_num is locked into each plan's metadata at queue time so HDF filenames and MongoDB scan numbers are always consistent.
- **Queue Manager** — Add, reorder, and delete plans (with confirmation). Each queued plan shows its assigned scan number (`#N`) instead of a queue position. Full RE controls (open environment, start, pause, resume, abort, stop). The Start button is disabled whenever the RE Manager is busy (`manager_state ≠ idle`).
- **Plan Builder** — Two-panel interface: a **Visual Composer** for assembling scan sequences from drag-and-drop blocks (no Python required), and a **Code Editor** for full custom plans with syntax highlighting, auto-indent, and templates.
- **Live Viewer** — Real-time pyqtgraph plots streamed over ZMQ. Crosshair cursor, point-hover tooltip, double-click motor move, screenshot.
- **MongoDB Browser** — Browse completed runs stored in MongoDB. Filters automatically to the active experiment (UID-based, not regex); select multiple runs for overlay plotting with common-column intersection; auto-plots when selection or axis choices change; 1st and 2nd derivative transforms (`np.gradient`, no x-shift) with error propagation applied to data and fit overlays; double-click the plot to move the motor; screenshot; HDF5 export.
- **HDF5 Viewer** — Open exported HDF5 archives, browse scans, overlay plots, view metadata.
- **RE Console** — Live console output from the RE Manager (color-coded for errors/warnings/success).
- **Instance Profiles** — Run multiple named RE Manager instances simultaneously (e.g. `ASWAXS`, `SURF`, `Sim`) each with its own device set and auto-assigned ports. Switch profiles from the toolbar.
- **Local Profiles** — Run RE Manager as a local subprocess with zero setup. Starts automatically when you launch the profile and stops when you close the app. Ideal for learning and testing with simulated devices.
- **Edit Devices File** — Full code editor for any profile's devices file: line numbers, current-line highlight, auto-indent, Tab→spaces, and ophyd-aware autocomplete. Local profiles read/write the file on disk; remote profiles pull from and push to the RE machine via SFTP.
- **Live Device Monitor** — Real-time EPICS Channel Access (CA) monitoring in the Devices & Plans tab. Each device shows its connected/disconnected status and live PV readings that update instantly as values change — no polling during scans. pyepics is auto-installed if missing.
- **Sim Device Monitor** — In simulation mode, device values are polled from the RE environment every 2 seconds via `read_devices_status()`. Tweak widgets on motor rows allow nudging simulated motors without running a full plan.
- **Custom Scan Plans** — A library of beamline-optimised scan plans (`custom_plans.py`) that ship with the app. Every plan guarantees `image_mode='Single'` at each detector step, saves and restores `acquire_time` (even on abort), and accepts `hdf_autosave=False` to suppress HDF file writing for alignment scans without permanently changing detector configuration.
- **Smart Legend Positioning** — After each plot update in the Live Viewer, MongoDB Browser, and HDF5 Viewer, the legend automatically moves to the emptiest quadrant of the view. Legends are also draggable — click and drag to pin them anywhere on the plot.
- **Curve Fitting** — Interactive lmfit-powered curve fitting in both the HDF5 Viewer and MongoDB Browser. The non-modal Curve Fit dialog displays a live preview curve on the plot the moment it opens, and the preview updates in real time as you edit parameters. Run Fit refines the model and updates the table with fitted values. Choose from 5 peak models, 4 step/interface models, 4 polynomial background terms, and 6 minimisation algorithms. Fit parameters are saved **per dataset** (keyed by run UIDs + x/y fields) — re-opening the dialog for the same data automatically restores the last fit and shows the full results table without clicking Fit again. Export fitted parameters and curves to CSV, or copy the results text to the clipboard.
- **Find / Replace** — Floating find bar (Ctrl+F) in the Plan Builder Code Editor, Devices Editor, and RE Console. Ctrl+R opens find-and-replace in editable editors.
- **ESAF Integration** — Import Experiment Safety Assessment Forms (ESAFs) from a local PDF, a REST server, or manual entry. Auto-generates the beamline folder structure and injects ESAF metadata into every plan. The picker includes a **technique dropdown** to filter ESAFs by technique (selection is persisted per profile across sessions) and a **client-side regex search** that matches any field — PI name, title, ESAF ID, users, and more. Attach arbitrary **extra fields** (custom key-value pairs) to any ESAF — empty for older entries, editable from the picker or via the REST API. Includes a standalone FastAPI admin server with MongoDB or SQLite backend.
- **Remote Data Root** — Per-profile setting for the Linux RE machine's data root directory. Automatically propagated to every plan as `remote_exp_dir` so area detectors can write HDF files to the correct network path without manual entry each run.
- **Experiment Console Log** — RE Manager console output is automatically appended to `<exp_dir>/console.log` for the duration of each session. The log opens when an experiment is set and closes cleanly when the experiment changes or the app exits.
- **Experiment Folder Monitoring** — A background filesystem watcher detects if the active experiment folder is deleted or becomes inaccessible while the app is running. A modal dialog notifies the user; if a scan is running it is paused automatically. NFS mount failures (ESTALE, EIO, ENOTCONN) are reported with a distinct message. All filesystem probes run in daemon threads — the Qt event loop is never blocked.
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
│   • pymongo (optional)      │          │   • hardware ophyd drivers        │
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

Core dependencies installed automatically: `PyQt6`, `pyqtgraph`, `numpy`, `scipy`, `lmfit`, `pandas`, `pyzmq`, `h5py`, `paramiko`, `pymongo`, `pdfplumber`. EPICS support (`pyepics`) is auto-installed on first use of the Live Device Monitor.

### RE Manager host

Only `bluesky-queueserver` and `pyepics` need to be installed — not the full EasyBluesky app:

```bash
pip install bluesky-queueserver pyepics
```

For MongoDB data storage (recommended):

```bash
pip install pymongo          # on both client and RE Manager host
# then install and start MongoDB on the RE Manager host
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
- **MongoDB Browser** tab → browse completed runs, select runs to plot

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
EASY_BLUESKY_MONGO_DB=mybeamline \
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

### Editing devices files

**File → Edit Devices File…** opens a full code editor for any profile's devices file — no terminal needed.

```
┌─────────────────────────────────────────────────────────────────┐
│  Edit Devices File — devices_aswaxs.py  [ASWAXS]  *            │
│                                                                 │
│  Profile: [ ASWAXS ▼ ]  Remote: myhost:~/.easy_bluesky/…      │
│ ┌────┬────────────────────────────────────────────────────────┐ │
│ │  1 │ from ophyd import EpicsMotor, EpicsSignal             │ │
│ │  2 │                                                        │ │
│ │  3 │ # ── Motors ─────────────────────────────────────────  │ │
│ │  4 │ sample_x = EpicsMotor("IOC:m1", name="sample_x")      │ │  ← current line highlighted
│ │  5 │ sample_y = EpicsMotor("IOC:m2", name="sample_y")      │ │
│ └────┴────────────────────────────────────────────────────────┘ │
│  ✓ Pulled from myhost:~/.easy_bluesky/scripts/devices_aswaxs.py │
│  [Pull from RE Machine]  [Save & Push to RE Machine]  [Close]   │
└─────────────────────────────────────────────────────────────────┘
```

### Editor features

| Feature | Detail |
|---------|--------|
| Line numbers | Gutter on the left; auto-widens as the file grows |
| Current-line highlight | Subtle highlight on the active row (theme-aware) |
| Syntax highlighting | Python keywords, strings, comments, decorators, numbers |
| Auto-indent | Enter after `:` adds one indent level automatically |
| Tab → 4 spaces | Smart tab stops at column boundaries |
| Smart backspace | Removes a full 4-space indent block at once |
| Autocomplete | Ctrl+Space (or type 2+ chars) — ophyd classes, common kwargs, Python keywords |

Autocomplete includes ophyd-specific words out of the box: `EpicsMotor`, `EpicsSignal`, `EpicsSignalRO`, `AreaDetector`, `HDF5Plugin`, `SynAxis`, `SynGauss`, `name=`, `kind=`, and more. If `jedi` is installed (`pip install jedi`), completions become fully context-aware.

### Find / Replace

A floating find bar is available in every code editor and the RE Console:

| Shortcut | Action | Available in |
|---------|--------|-------------|
| Ctrl+F | Open find bar | Code Editor, Devices Editor, RE Console |
| Ctrl+R | Open find + replace | Code Editor, Devices Editor (not RE Console — read-only) |
| Enter / ↓ | Next match | Find bar focused |
| Shift+Enter / ↑ | Previous match | Find bar focused |
| Escape | Close bar | Find bar focused |

All matches in the current document are highlighted immediately as you type. The current match is shown in orange; other matches in amber. A `N / M` counter shows which match is selected. The search field turns red when no matches are found.

The Replace row (Ctrl+R) offers **Replace** (current match) and **Replace All** buttons. Replace operations are undoable as a single action.

### Starter template

If the devices file does not exist yet (new profile), the editor pre-fills a starter template:

```python
"""
devices_aswaxs.py — Hardware device definitions for ASWAXS profile.
...
"""
from ophyd import EpicsMotor, EpicsSignal, EpicsSignalRO

# ── Motors ────────────────────────────────────────────────────────────────────
# sample_x = EpicsMotor("IOC:m1", name="sample_x")
# sample_y = EpicsMotor("IOC:m2", name="sample_y")

# ── Detectors ─────────────────────────────────────────────────────────────────
# det = EpicsSignal("IOC:det", name="det")

# ── Read-only signals ─────────────────────────────────────────────────────────
# ring_current = EpicsSignalRO("RING:current", name="ring_current")
```

Uncomment and fill in your PV names, then click **Save** (local) or **Save & Push to RE Machine** (remote).

### Local vs remote

**Local profiles** — reads and writes `~/.easy_bluesky/scripts/<file>` directly. Buttons: **Reload** and **Save**.

**Remote profiles** — transfers the file via SFTP over the existing SSH connection. Buttons:

| Button | Action |
|--------|--------|
| Pull from RE Machine | Downloads the file from the remote host. Also saves a local copy so the sim generator can read it offline. |
| Save & Push to RE Machine | Saves a local copy, then uploads to the remote host. |

The **profile combo** at the top lets you switch between any profile's devices file without reopening the dialog. Unsaved changes prompt for confirmation before switching or closing.

A `*` in the title bar marks unsaved changes.

### Typical remote workflow

1. **File → Edit Devices File…** — dialog opens and auto-pulls the current file from the RE machine
2. Edit the devices (uncomment and fill in PV addresses)
3. Click **Save & Push to RE Machine**
4. Click **⚡ Start RE Mgr** in the main toolbar to restart the RE Manager and pick up the new devices

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

**Automated (recommended):** Open **File → Connection Settings**, fill in the host and SSH username, then click **Setup SSH Key…** next to the key path field. The app will:

1. Generate an Ed25519 key pair at `~/.ssh/id_ed25519` (if one does not already exist)
2. Prompt for your SSH password **once** in a masked dialog — it is never stored anywhere
3. Connect to the remote host and append the public key to `~/.ssh/authorized_keys`
4. Update the key path field automatically

After the key is installed, click **Test SSH Connection** to verify passwordless auth works.

**Manual alternative:** If you prefer the command line:

```bash
# 1. Generate key pair (skip if you already have one)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519

# 2. Install on remote host
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-beamline-host

# 3. Verify
ssh -i ~/.ssh/id_ed25519 user@your-beamline-host echo ok
```

> **Note:** SSH key authentication requires that your home directory on the remote host is **not** group- or world-writable. If key auth is rejected, run `chmod go-w ~` on the remote machine.

**In the app**, open **File → Connection Settings → Remote SSH Management**:

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

The launcher script exports `EASY_BLUESKY_DEVICES_FILE=<profile's devices file>` and `EASY_BLUESKY_MONGO_DB=<db name>` so `re_startup_mongo.py` loads the right devices and writes to the right database.

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

## Live Device Monitor

The **Devices & Plans** tab shows all devices registered in the open RE environment, with real-time PV readings sourced directly from EPICS Channel Access (CA) — not through the RE Manager. This means values update instantly while a scan is running without adding any polling overhead.

### How it works

When the RE environment transitions from `closed` → `idle`, the app calls `get_device_pvnames()` in the RE worker namespace to retrieve a map of every device's signals and their PV names. It then opens a persistent CA monitor for each PV using `pyepics` with `form='ctrl'` (DBR_CTRL subscription). DBR_CTRL callbacks deliver the current value **and** the engineering units (EGU field) together in every update, with no extra round-trip to the IOC. Value and connection-change callbacks fire on the CA background thread and are forwarded to Qt via queued signals — fully thread-safe.

Units and descriptions are also cached to `~/.easy_bluesky/device_metadata.json` so they can be reused in sim mode display without a CA connection.

### Device tree layout

```
AVAILABLE DEVICES
┌────────────────────────────────────────────────────────────────────────────┐
│ Device / Signal    Class        Value       Units  Description   Tweak     │
│ ──────────────────────────────────────────────────────────────────────────│
│ ▼ EpicsMotor  (2)                                                          │
│   ▼ sample_x        EpicsMotor  12.5000     mm     Sample X     [◀][0.1][▶]│
│       user_readback             12.5000     mm                             │
│       user_setpoint             12.5000     mm                             │
│   ▼ sample_y        EpicsMotor   0.0000     mm     Sample Y     [◀][0.1][▶]│
│ ▼ AreaDetector  (1)                                                        │
│   ▼ Pil300K         AreaDetector ○ Connecting…                             │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Green value** on the device row — primary signal is connected and live
- **"○ Disconnected"** in red — CA connection lost (IOC down or PV name mismatch)
- **"○ Connecting…"** in grey — waiting for the first CA callback
- **Tweak column** — motor devices get `[◀][step][▶]` inline widgets to nudge the motor by the step size. Mouse-wheel on the step spinbox is disabled to prevent accidental moves.
- Signal sub-rows (indented) show all readable signals; hovering shows the raw PV name

A **search bar** above the tree filters by device name, class, or description as you type. Group headers hide when all their children are filtered out.

The **primary signal** displayed on the device row is chosen in priority order: `user_readback` → `readback` → signal with the same name as the device → first available signal.

### ⟳ Reconnect button

Click **⟳ Reconnect** to re-fetch PV names from the RE environment and reopen all CA monitors. Useful after restarting the RE Manager or changing the devices file.

### Sim mode

In simulation profiles (devices are `ophyd.sim` objects), PV names are empty so CA monitoring is unavailable. Instead, the app polls device values every 2 seconds by calling `read_devices_status()` in the RE environment. Units and descriptions are populated from the cached `device_metadata.json` when available.

Tweak buttons in sim mode call `set_sim_device(name, value)` in the RE environment via `function_execute` — the device is moved without adding a queue item.

### CA callback coalescing

In real mode, pyepics can fire dozens of CA callbacks per second during active scans. Applying each callback directly to the Qt tree (one repaint per callback) drove CPU usage above 1000 % during scans. EasyBluesky coalesces all incoming updates before applying them:

- Incoming value and description callbacks are buffered into dictionaries (`_pending_pv_updates` and `_pending_desc_updates`).
- A 100 ms `QTimer` (`_pv_flush_timer`) flushes and applies all buffered changes at 10 Hz — at most one tree repaint per 100 ms, regardless of scan speed.
- Device list changes are fingerprinted (`_last_devices_fp`). If the device list has not changed between RE Manager restarts, CA monitor teardown and rebuild are skipped entirely — avoiding unnecessary PV reconnections.

This keeps the Live Device Monitor responsive at full scan speed without any additional polling overhead.

### pyepics auto-install

If `pyepics` is not installed in the app's Python environment, a background thread runs `pip install pyepics` automatically the first time live monitoring is attempted. No manual installation step is needed.

> **Network requirement:** the client machine running the app must be on the same network as the EPICS IOCs (or have the appropriate CA gateway configured). CA connections go directly from the client to the IOC — they do not pass through the RE Manager host.

---

## MongoDB Browser

The **MongoDB Browser** tab is the primary interface for reviewing and analyzing completed runs. It replaces the old history plot with a richer multi-run workflow.

### Setup

Enable MongoDB in Connection Settings for each profile:

| Field | Example | Notes |
|-------|---------|-------|
| Database | `aswaxs_runs` | One database per profile |
| Mongo Host | `localhost` | Host where `mongod` is running |
| Mongo Port | `27017` | Default MongoDB port |

The RE Manager writes runs to MongoDB during acquisition (via `re_startup_mongo.py`). The app reads from the same database.

`pymongo` must be installed on **both** the client machine and the RE Manager host:

```bash
pip install pymongo
```

### Experiment filtering

When you open or switch experiments, the MongoDB Browser automatically filters the run list to show only runs from that experiment. The experiment name is shown in the top bar. To see all runs (across all experiments), tick **All runs**.

### Multi-run overlay

Hold Shift or Ctrl/Cmd and click rows in the run table to select multiple runs. The app computes the **intersection of available columns** across all selected runs and offers only common fields for plotting — so every selected Y signal has data in every run.

Up to 10 runs can be selected simultaneously.

### Auto-plotting

The plot updates automatically whenever:
- The run selection changes (180 ms debounce)
- The X-axis field changes
- A Y signal checkbox is toggled
- The Norm field changes
- Log Y is toggled

No Plot button to click — the plot always reflects the current selection.

Each unique (run, field) pair gets a distinct color. A legend identifies every curve.

### Double-click to move motor

Double-click any point on the plot to move the X-axis motor to that position. This works when the X-axis is a motor field (not time or sequence number).

The confirmation dialog shows:
- The motor name (derived by stripping readback suffixes like `_user_readback`)
- The target position (where you clicked)
- The last known position from the scan data

Click **Yes** to execute `mv(motor, position)` immediately on the RE Manager.

### Screenshot

Click **Screenshot** to save the current plot as a PNG. A file dialog lets you choose the output path.

### HDF5 export

Select one or more runs and click **Export HDF5…** to save them as a portable `.h5` file. The file is compatible with the **HDF5 Viewer** tab:

```
my_export.h5
├── metadata/          # global attrs: n_scans
│   └── (attrs)
├── scan_0001/         # one group per scan
│   ├── (attrs)        # plan_name, uid, exit_status, timestamp, duration_s, motor, detectors
│   ├── time           # dataset: timestamps
│   ├── motor_pos      # dataset: motor positions
│   └── det_counts     # dataset: detector readings
└── scan_0002/
    └── ...
```

Click **Export Exp…** (next to the run-count label) to export **all runs belonging to the active experiment** in one step — useful for handing off a complete dataset to a user. The output file is compatible with the HDF5 Viewer.

### Curve fitting

Select one or more runs, choose signals and axes, then click **Fit**. The **Curve Fit** dialog opens (see [Curve Fitting](#curve-fitting) for full details).

Fit parameters are remembered **per dataset** (keyed by run UIDs, stream, x-field, and y-fields). Re-opening the dialog for the same data restores the last fit state immediately — the parameter table is pre-filled with the previous fitted values and the fit is re-run automatically so the results table (Center, FWHM, R², Amplitude) and curve overlay appear without any extra clicks. Switching to a different run or different x/y fields opens a fresh dialog with auto-guessed initial parameters.

### Run table columns

| Column | Content |
|--------|---------|
| Scan # | Monotonically increasing scan ID |
| Plan | Plan name (scan, count, rel_scan, …) |
| Date / Time | Start time |
| Status | ✓ success / ✗ fail / ⊘ abort / … running |
| Points | Number of primary-stream events |
| Detectors | Detector names from the run start document |

### Plot legend

The legend auto-positions to the emptiest corner of the view after each plot update. Drag it to any position to override the automatic placement.

---

## Live Viewer

The **Live Viewer** tab shows real-time plots as a scan runs. Documents are received over ZMQ from the RE Manager's PUB socket on the doc port.

### Controls

| Control | Purpose |
|---------|---------|
| X combo | Choose the X-axis signal |
| Y list | Select one or more Y signals (multi-select) |
| Norm by | Divide Y by this signal (e.g. beam monitor) |
| ± Errors | Toggle Poisson error bars (σ = √\|y\| for raw counts; propagated through normalisation) |
| Clear | Reset the plot and data buffer |
| Screenshot | Save the current plot as a PNG |

### Error bars

Check **± Errors** to display Poisson error bars on every curve. For raw counts the uncertainty is σ = √|y|. When a normalisation signal is selected, the uncertainty is propagated as σ_f = √(y/n² + y²/n³), where n is the normalisation value. Error bars update in real time as new scan events arrive.

### Double-click motor move

Double-click any point on the live plot to move the X-axis motor to that position. The confirmation dialog shows the motor name and target value.

### Crosshair cursor

A crosshair follows the mouse and a tooltip shows the nearest curve's value at the cursor position.

### Plot legend

The legend auto-positions to the emptiest corner of the view at the end of each scan update. Drag it to any position to pin it there.

---

## HDF5 Viewer

The **HDF5 Viewer** tab opens exported HDF5 archives and allows offline analysis — no beamline connection required. This makes it the primary tool for users who take data home after a beamline session.

### Opening a file

Click **Open HDF5…** and select a `.h5` file produced by the MongoDB Browser's Export HDF5 function or the Experiments tab's Export HDF5 fallback. The scan list on the left is populated automatically.

### Browsing scans

Select one or more scans in the list. The app finds the common set of fields across all selected scans and populates the X, Y, and Norm dropdowns accordingly. Overlay up to 10 scans on the same plot.

### Controls

| Control | Purpose |
|---------|---------|
| X combo | X-axis field |
| Y list | One or more Y fields (multi-select) |
| Norm by | Divide Y by this field |
| Log Y | Logarithmic Y axis |
| Fit | Open the Curve Fit dialog for the current plot |
| Screenshot | Save plot as PNG |

### Metadata

Select a single scan to view its metadata (plan name, UID, exit status, timestamp, motor, detectors) in the panel below the scan list.

### Offline use

The HDF5 Viewer works with no network connection and no RE Manager running. Install the package on any computer:

```bash
pip install easy-bluesky
easy-bluesky
```

Open the **HDF5 Viewer** tab — everything else can be ignored. Users at the beamline export their data once (`Export HDF5…` in the MongoDB Browser or Experiments tab), copy the `.h5` file to a USB drive or network share, and open it at home.

### Plot legend

The legend auto-positions to the emptiest corner of the view after each scan selection change. Drag it to any position to override the automatic placement.

---

## Curve Fitting

The **Curve Fit** dialog is available in both the **HDF5 Viewer** and the **MongoDB Browser**. Click **Fit** after choosing X/Y signals. The dialog uses [lmfit](https://lmfit.github.io/lmfit-py/) for robust non-linear least-squares fitting.

### Signal models

#### Peak models

| Model | Shape | Derived quantity |
|-------|-------|-----------------|
| Gaussian | exp(−½((x−x₀)/σ)²) | FWHM = 2.355 σ |
| Lorentzian | 1 / (1 + ((x−x₀)/σ)²) | FWHM = 2 σ |
| Voigt | Voigt profile | Pseudo-FWHM (Thompson formula) |
| Pseudo-Voigt | η·Lorentzian + (1−η)·Gaussian | FWHM = 2 σ |
| Super-Gaussian | exp(−((x−x₀)²/(2σ²))ⁿ) | FWHM = 2σ·(2ln2)^(1/2n) |

#### Step / interface models

Used when an interface is scanned (e.g. a knife-edge, a slit, or a material boundary). The derived quantity reported is the **10–90% width** rather than FWHM.

| Model | Shape | 10–90% width |
|-------|-------|-------------|
| Step (erf) | erf-based sigmoid | 2.197 σ |
| Step (tanh) | tanh-based sigmoid | 2.197 σ |
| Step (arctan) | arctan-based sigmoid | ≈ π σ × 0.8 |
| Step (logistic) | logistic function | 2.197 σ |

### Background models

A polynomial background is fit simultaneously with the signal model so that the signal parameters reflect only the peak/step shape, not the baseline.

| Background | Polynomial order |
|-----------|-----------------|
| None | — (no background) |
| Constant | c₀ |
| Linear | c₀ + c₁x |
| Quadratic | c₀ + c₁x + c₂x² |
| Cubic | c₀ + c₁x + c₂x² + c₃x³ |

Background initial values are estimated automatically from the data edges (first and last 20% of data points). Background parameters appear in the table alongside signal parameters and can be adjusted manually.

### Minimisation algorithms

| Algorithm | Use when |
|-----------|---------|
| Levenberg-Marquardt (default) | Well-conditioned peaks; fastest |
| Least Squares (Trust Region) | Better for problems with tight bounds |
| Nelder-Mead | Noisy data; no gradient needed |
| L-BFGS-B | Fast for smooth well-behaved problems |
| Powell | Derivative-free; good general fallback |
| Differential Evolution | Global search; avoids local minima for multi-peak data |

### Workflow

The Curve Fit dialog is **non-modal** — it stays open alongside the main plot so you can see the model update in real time without the dialog blocking the view.

1. Click **Fit** in the HDF5 Viewer or MongoDB Browser.
2. The **Curve Fit** dialog opens. A dotted gold **preview curve** immediately appears on the plot, drawn with the auto-guessed initial parameters.
3. Choose a **Model**, **Background**, and **Algorithm** from the dropdowns. The preview updates automatically whenever the selection changes.
4. Review and edit the **Parameters** table — adjust initial values, set Min/Max bounds (type `-inf` / `+inf` for no bound), or tick **Fixed** to hold a parameter constant. The preview curve on the plot updates 400 ms after each edit, so you can see the effect of your changes before committing to a fit.
5. Click **Run Fit** to execute the fit:
   - The table updates in place with the fitted parameter values.
   - The preview curve updates to the fitted result.
   - The **Fit Results** text area shows R², N points, all parameter values with ± uncertainties, and FWHM (peaks) or 10–90% width (steps).
6. Adjust parameters and re-fit as many times as needed.
7. Click **Apply & Close** to commit:
   - The dotted preview is replaced by a solid dashed fit overlay for every selected dataset.
   - An annotation box shows: model, x₀, FWHM/width, and R².
   - The fit state (model, background, and all parameter values) is **saved automatically**.
8. Click **Cancel** (or close the dialog) to discard changes and remove the preview.

> **Saved fit state:** the next time you click Fit on the same viewer, the dialog opens pre-populated with the saved model, background, and parameter values from step 7 — you can re-run or fine-tune without starting from scratch.

### Exporting fit results

Two export options appear below the **Fit Results** text area and are available after **Run Fit**:

| Button | Action |
|--------|--------|
| **Copy Results** | Copies the fit summary text (model, R², parameters ± uncertainties, FWHM/width) to the system clipboard. Paste directly into a lab notebook, spreadsheet, or paper. |
| **Export Fit…** | Opens a save dialog and writes a CSV file. |

The exported CSV contains:

```
# EasyBluesky Curve Fit Export
# Model     : Gaussian
# R²        : 0.999200
# N points  : 80
# Parameters:
#   Amplitude                    1000.0    ±  2.3
#   Center (x₀)                 5.0001    ±  0.0012
#   Sigma (σ)                   0.7981    ±  0.0014
#   FWHM                        1.8795
#
# Section 1: data points and fit at each measurement x
# Columns: x_data, y_data, y_fit, residual
0.000,...
#
# Section 2: smooth fit curve
# Columns: x_fit, y_fit_smooth
0.000,...
```

**Section 1** (data + fit + residual at each measured point) is most useful for residual analysis and checking goodness of fit. **Section 2** (500-point smooth curve) is most useful for replotting in Origin, Igor Pro, or matplotlib alongside raw data. Both sections can be imported with `pd.read_csv(file, comment='#')` — each section is read independently since the column counts differ.

The **Export Fit…** button is disabled until a fit is run and re-disables if the model or background selection changes (because the saved fit would no longer match the current model).

The fit overlay on the main plot includes:
- A smooth dashed curve evaluated at 5× the data density
- A text annotation at the peak/step centre: model name, x₀, FWHM or 10–90% width, and R²

---

## Visual Plan Composer

The **Visual Composer** tab inside Plan Builder lets you build a complete Bluesky plan by assembling blocks — no Python required. It generates a fully parametric Python plan function that you can send directly to the Code Editor or the RE Manager.

### Layout

```
┌──────────────────┬─────────────────────────────────────┬──────────────────────┐
│  BLOCK PALETTE   │  MAIN SEQUENCE          PER-STEP     │  BLOCK PROPERTIES    │
│                  │                                      │                      │
│  ▸ Motion        │  ⏲ Set Exposure  ×1   ◉ Open Shutter│  ⏲  Set Exposure     │
│  ▸ Timing        │  ⟳ Scan          ×1   📷 Trigger & R│                      │
│  ▸ Detector      │                       ○ Close Shutter│  Detectors:          │
│  ▸ Shutter       │                       ⏱ Sleep  0.5s │  [✓ Eig1M, Pil300K]  │
│  ▸ Device        │                                      │                      │
│  ▸ Plans         │  [Plan name: my_scan ]               │  Exposure attr:      │
│  ▸ Flow          │                                      │  cam.acquire_time    │
│                  │  GENERATED CODE ────────────────────  │                      │
│  Double-click or │  def my_scan(                        │  Exposure time:      │
│  drag to add     │    detectors: List[Readable] = None, │  1.0                 │
│                  │    motor: Movable = None, ...        │                      │
│  [Add to Main ↑] │                        [→ Send to   │                      │
│  [Add to Per-Step│                         Code Editor] │                      │
└──────────────────┴─────────────────────────────────────┴──────────────────────┘
```

- **Main Sequence** — blocks that run once per plan (set exposure, configure files, then scan)
- **Per-Step Sequence** — blocks injected at every point of the scan or count (open shutter → trigger → close shutter → sleep)
- **Block Properties** — parameter form for the selected block; device pickers show all connected devices
- **Generated Code** — live preview; updates as you edit any block property
- **Send to Code Editor** — pushes the generated plan to the Code Editor tab for further customization or upload

Drag blocks from the palette directly into either sequence list, or double-click to append to the last active list. Delete with the **Del** key or the Remove button. Drag rows to reorder.

---

### Block Reference

#### Motion

| Block | Bluesky call | Parameters |
|-------|-------------|------------|
| **Move** | `bps.mv(device, position)` | device, position |
| **Relative Move** | `bps.mvr(device, delta)` | device, delta |

#### Timing

| Block | Bluesky call | Parameters |
|-------|-------------|------------|
| **Sleep** | `bps.sleep(seconds)` | seconds |

#### Detector

| Block | Bluesky call | Parameters |
|-------|-------------|------------|
| **Set Exposure** | `bps.mv(det.cam.acquire_time, t)` for each det | detectors, exposure_attr, exposure_time |
| **Set AD File** | `bps.abs_set(det.hdf1.file_path / file_name, ...)` | detector, plugin, file_path, file_name |
| **Trigger & Read** | `bps.trigger_and_read(detectors)` | detectors |

#### Shutter

| Block | Bluesky call | Parameters |
|-------|-------------|------------|
| **Open Shutter** | `bps.mv(shutter, 'open')` | shutter |
| **Close Shutter** | `bps.mv(shutter, 'closed')` | shutter |

#### Device

| Block | Bluesky call | Parameters |
|-------|-------------|------------|
| **Stage Device** | `bps.stage(device)` | device |
| **Unstage Device** | `bps.unstage(device)` | device |
| **Set Attribute** | `bps.mv(device.attribute, value)` | device, attribute, value |

#### Plans

All scan blocks support the Per-Step sequence injection.

| Block | Bluesky call | Notes |
|-------|-------------|-------|
| **Scan** | `bp.scan(dets, motor, start, stop, num)` | Single motor: start/stop parametric. Multi-motor: enter comma-separated motors, starts, stops — each motor gets its own range. Shorter lists are padded with the last value. |
| **Relative Scan** | `bp.rel_scan(dets, motor, start, stop, num)` | Same as Scan but positions are relative to the current motor position. |
| **Grid Scan** | `bp.grid_scan(dets, m1, s1, e1, n1, m2, …)` | Comma-separated motors/starts/stops/nums (one value per motor). Optional **Energy inner axis** — set `energy_motor`/`energy_start`/`energy_stop`/`energy_num` to append energy as the innermost (fastest-varying) axis. |
| **List Scan** | `bp.list_scan(dets, motor, [positions])` | Explicit comma-separated position list. Optional **Energy inner loop** — generates nested for-loop: move to each spatial position, then run `bp.scan` over energy. |
| **Adaptive Scan** | `bp.adaptive_scan(dets, field, motor, …)` | Intelligent step sizing based on signal change. `target_field` is the detector reading name to adapt on (e.g. `Pil300K_stats1_total`). All numeric params (min_step, max_step, target_delta, threshold) are parametric. |
| **Fly Scan** | `bp.fly([flyer])` | Hardware-triggered continuous acquisition. If `motor` is set, prepends `bps.abs_set(motor.velocity, …)` + `bps.mv(motor, start)` before the fly call. |
| **Count** | `bp.count(dets, num, delay)` | Fixed-position acquisition. |
| **Plan Stub** | `yield from stub_name(args)` | Free-form plan stub for one-liners not covered by other blocks. |

#### Flow  *(shown in purple/italic)*

| Block | What it generates | Notes |
|-------|------------------|-------|
| **Repeat N Times** | `for _i in range(n): <body>` | Wraps the entire Main Sequence body. Composable: a Repeat block inside a sequence containing For Each Position produces a correctly nested double loop. |
| **For Each Position** | `for _pos in [p1, p2, …]: bps.mv(motor, _pos); bp.count(dets, …)` | Self-contained — includes its own detector, num, and delay fields. |
| **Custom Python** | Raw code injected at that position | Multiline editor in Block Properties. Use for anything not covered by other blocks. |

---

### Multi-motor scans

For **Scan**, **Relative Scan**, and **Grid Scan** with multiple motors selected, enter comma-separated values in the `start` and `stop` fields — one value per motor:

```
Motors : coll_x, coll_y
Start  : 0.0, 10.0
Stop   : 5.0, 20.0
Num    : 11
```

Generates: `bp.scan(dets, coll_x, 0.0, 5.0, coll_y, 10.0, 20.0, 11)`.

If you enter a single value for start/stop, it is repeated for all motors.

---

### Energy inner loops (Grid Scan / List Scan)

Set the **Energy motor**, **Energy start**, **Energy stop**, and **Energy num** fields in Block Properties:

**Grid Scan + energy** — energy is appended as the last (innermost) `bp.grid_scan` axis:
```python
yield from bp.grid_scan(dets, sample_x, 0, 5, 5, sample_y, 0, 10, 10, dcm_energy, 7100, 7200, 20)
```

**List Scan + energy** — generates a nested for-loop (spatial outer, energy inner):
```python
for _pos in [0.5, 1.0, 2.5]:
    yield from bps.mv(motor, _pos)
    yield from bp.scan(dets, dcm_energy, 7100, 7200, 50)
```

Leave `energy_motor` blank to skip the energy loop entirely.

---

### Generated code

The Visual Composer produces a fully parametric Python function. Device selections from Block Properties become the **default values** — callers can override any device or numeric value at call time:

```python
def my_scan(
        detectors: List[Readable] = None,
        motor: Movable = None,
        start: float = 0.0,
        stop: float = 5.0,
        num: int = 11,
        exposure_time: float = 1.0,
):
    """
    Plan generated by EasyBluesky Visual Composer.

    Sequence
    --------
    Main     : Set Exposure → Scan
    Per-step : Open Shutter → Trigger & Read → Close Shutter → Sleep
    ...
    """
    detectors = detectors or [Eig1M]
    motor = motor or coll_x

    for _det in detectors:
        yield from bps.mv(_det.cam.acquire_time, exposure_time)

    def _per_step(detectors, step, pos_cache):
        yield from bps.move_per_step(step, pos_cache)
        yield from bps.mv(shutter, 'open')
        yield from bps.trigger_and_read(detectors)
        yield from bps.mv(shutter, 'closed')
        yield from bps.sleep(0.5)

    yield from bp.scan(detectors, motor, start, stop, num, per_step=_per_step)
```

Click **→ Send to Code Editor** to transfer the code to the Code Editor tab, where you can review, edit, and upload to the RE Manager.

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

Handles RE setup, MongoDB data routing, and ZMQ doc publishing. Reads `EASY_BLUESKY_DEVICES_FILE` and `EASY_BLUESKY_MONGO_DB` from the environment. **All profiles share this single startup script** — no per-profile startup scripts needed.

The devices file is loaded by direct path (`importlib.util.spec_from_file_location`) rather than by module name, with a case-insensitive glob fallback. This means `devices_file: "devices_aswaxs.py"` in the profile will find `devices_ASWAXS.py` on a Linux beamline machine even though Linux filesystems are case-sensitive.

Callable functions (via `function_execute`):

| Function | Purpose |
|----------|---------|
| `get_device_pvnames()` | Returns `{dev_name: {sig_name: pvname}}` — used by the CA monitor |
| `read_devices_status()` | Returns live readings for all devices — used by the sim monitor |
| `set_sim_device(name, value)` | Moves a simulated device — used by tweak buttons in sim mode |
| `prime_detector(det)` | Warms up area detector file plugins before first scan |

### `devices_sim.py` — simulation devices

Auto-generated by **File → Generate Sim Devices…**. Contains simulated equivalents of your real devices plus generic test devices. Referenced by a `Sim` profile's **Devices file** field.

### YAML permission files

- `existing_plans_and_devices.yaml` — device/plan list for the Default profile (auto-updated when environment opens)
- `existing_plans_and_devices_sim.yaml` — device/plan list for the Sim profile (kept separate so real and sim don't overwrite each other)
- `user_group_permissions.yaml` — controls which user groups can run which plans

---

## Custom Scan Plans

`custom_plans.py` (at the repo root) is a library of beamline-optimised scan plans that ships with EasyBluesky. The file is uploaded to the RE Manager alongside `re_startup_mongo.py` on every restart, so all plans are immediately available in the queue without any extra installation step.

### Area detector save/restore

Every plan in `custom_plans.py` applies the following safety measures around area detectors:

- **`image_mode` forced to `'Single'`** — called at the start of each per-step or per-shot callback via `_set_image_mode_single(detectors)`. Some HDF5Plugin `stage()` implementations put `'Multiple'` directly to `cam.image_mode`, bypassing `stage_sigs`. Setting it explicitly before every trigger is the reliable override — the extra CA put is negligible.

- **`acquire_time` save and restore** — the detector's `cam.acquire_time` (and `cam.acquire_period` for area detectors, or `preset_time` / `count_time` / `preset_real` for scalers) is read before the scan body runs and written back after via the `bpp.finalize_wrapper` cleanup closure. Restore runs even when the scan is aborted.

- **`hdf1.auto_save` control** — every plan accepts an `hdf_autosave: bool = True` keyword argument. When `False`, `hdf1.auto_save` is set to `'No'` for the scan duration and the matching `stage_sigs` entry is patched so staging cannot re-enable it. The original value is restored after the scan completes or aborts.

The helper that manages all save/restore logic is `_save_and_set_det_mode(detectors, hdf_autosave, saved)`. The mutable `saved` dict is populated by this helper and consumed by the `_cleanup()` closure that `bpp.finalize_wrapper` calls unconditionally.

### Common parameters

All standard plans share the following keyword parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `acquire_time` / `exposure_time` | float | `1.0` | Detector exposure time in seconds |
| `delay` | float | `0.0` | Extra wait after each step or shot (seconds) |
| `shutter` | Device | `None` | Fast shutter — opened before each trigger, closed after (300 ms settling time) |
| `hdf_autosave` | bool | `True` | `False` suppresses HDF file writing; useful for alignment scans |
| `md` | dict | `None` | Plan metadata; experiment fields are auto-injected by EasyBluesky |

### Available plans

| Plan | Description |
|------|-------------|
| `count_w_time` | Fixed-position count with per-shot exposure time and optional shutter |
| `scan_w_time_n_delay` | Absolute motor scan (`bp.scan`) |
| `rel_scan_w_time_n_delay` | Relative motor scan from current position (`bp.rel_scan`) |
| `grid_scan_w_time_n_delay` | Absolute grid scan (`bp.grid_scan`) |
| `rel_grid_scan_w_time_n_delay` | Relative grid scan from current position (`bp.rel_grid_scan`) |
| `list_scan_w_time_n_delay` | Scan over an explicit position list (`bp.list_scan`) |
| `rel_list_scan_w_time_n_delay` | Relative list scan from current position (`bp.rel_list_scan`) |
| `list_grid_scan_w_time_n_delay` | List grid scan (`bp.list_grid_scan`) |
| `rel_list_grid_scan_w_time_n_delay` | Relative list grid scan (`bp.rel_list_grid_scan`) |
| `list_scan_w_time_n_delay_from_csv` | Multi-motor list scan with positions loaded from a CSV file (one column per motor) |
| `aswaxs_energy_scan` | Move to each energy in a list, sweep sample positions, take frames; includes a relative beam-loss suspender |
| `energy_xrf_scan` | Relative energy scan around a centre value with XRF measurement at each point; opens/closes a single Bluesky run |
| `energy_nested_scan` | Run an arbitrary inner plan at each absolute energy in a list; optional undulator tracking |
| `energy_nested_scan_relative` | Relative energy scan around a centre with configurable step, optional undulator tracking, and initial-energy restore |

### Example

```python
# Alignment count — 5 frames, 0.2 s exposure, no HDF files saved
count_w_time(
    [Pil300K],
    num=5,
    exposure_time=0.2,
    hdf_autosave=False,
    md={"sample_name": "align"},
)

# Standard scan — 1 s exposure, 0.5 s settling delay
scan_w_time_n_delay(
    [Pil300K],
    sample_x, 0, 5,
    num=11,
    acquire_time=1.0,
    delay=0.5,
)
```

### Beam-loss suspender (`RelativeBeamdownSuspenders`)

`aswaxs_energy_scan` installs a `RelativeBeamdownSuspenders` at the start of each energy point. The suspender reads a ring-current PV and captures its live value as the reference. If the readback falls below `suspend_fraction × reference` (default 50 %), the RE pauses automatically. Scanning resumes when the readback recovers above `resume_fraction × reference` (default 80 %).

---

## ESAF Integration

An **Experiment Safety Assessment Form (ESAF)** is the proposal document that governs who can access a synchrotron beamline, what experiments they may perform, and when the beam time is allocated. EasyBluesky can import ESAF data and use it to:

- Organise experiment folders under a canonical hierarchy keyed to the PI group and ESAF number
- Inject ESAF metadata (`esaf_id`, `pi_group`, `proposal_id`, `esaf_start_date`) into every plan's run-start document, so all data is traceable to the proposal without manual bookkeeping

---

### Folder structure (From ESAF mode)

When you create a new experiment with **From ESAF**, the local and remote paths are constructed automatically:

```
<experiments_root>/
└── uchicago_john_rogers/          ← PI group slug
    └── ESAF-12345_2026-08-01/     ← ESAF number + start date
        ├── run_A/                 ← first run (e.g. "SAXS_day1")
        │   ├── experiment.json
        │   └── runs/
        └── run_B/                 ← second run, same ESAF, same or different day
            ├── experiment.json
            └── runs/
```

The remote path on the RE machine mirrors this structure under the profile's **Remote Data Root**:

```
/home/chem_epics/data/
└── uchicago_john_rogers/ESAF-12345_2026-08-01/run_A/
```

---

### PI Groups

A **PI group** identifies the research team responsible for an ESAF. Slugs follow the convention `[univ_short_name]_[first_name]_[last_name]`, e.g. `uchicago_john_rogers` or `anl_alice_smith`.

PI groups are stored locally at `~/.easy_bluesky/pi_groups.json`. Each group has:

| Field | Example | Notes |
|-------|---------|-------|
| Slug | `uchicago_john_rogers` | Used as folder name; URL-safe |
| PI first / last name | `John Rogers` | Human-readable label |
| PI institution | `University of Chicago` | Full institution name |
| University short name | `uchicago` | Part of the slug |
| Known members | `Smith, Alice; Patel, Raj` | Used for auto-matching ESAFs |

**Auto-matching** — when you import an ESAF that lists users who are already in a PI group's `known_members` list, EasyBluesky automatically selects that group and highlights the matched name. After confirming, new names from the ESAF can be added to the group's member list with one click.

#### Managing PI groups

Open **File → Manage PI Groups…** (or click **Manage…** in any PI group picker) to add, edit, or delete groups. You can also create a group on the fly from the **New Experiment → From ESAF** dialog.

---

### Importing an ESAF

#### From the Open / New Experiment dialog

The **From ESAF** tab works as a combined open-or-create picker — you use it both to resume a previous run and to start a new one:

1. Open the **Experiments** tab and click **New Experiment**.
2. Select the **From ESAF** tab.
3. Use the **Technique** dropdown to narrow ESAFs by technique. The selection is remembered per profile across sessions.
4. Type in the **Search** field to filter by any field — ESAF ID, PI name, title, user names, proposal ID, institution, or any other text. Regex patterns are supported (e.g. `rogers|smith` for multiple PIs).
5. Pick an ESAF from the filtered list (or click **Import New ESAF…**).
6. The dialog scans the ESAF folder on disk and lists any existing runs.
   - **To resume**: click a run in the list and press **OK** (or double-click). The experiment re-opens exactly as it was — sample name, remote path, and ESAF metadata are all restored.
   - **To create a new run**: type a run name in the **New run name** field (selecting a run from the list and typing a name are mutually exclusive — one clears the other).
7. The path preview updates live to show where the run folder will be created.
8. Click **OK** — the folder is created (new run) or reopened (existing run).

#### Import wizard (ESAFImportDialog)

The wizard is a three-tab dialog:

**Tab 1 — Source**

| Option | Description |
|--------|-------------|
| Parse PDF locally | Select a PDF file on your Mac; parsed with `pdfplumber` (no server needed) |
| Upload PDF to server | Send the PDF to the ESAF server for parsing (requires server URL in settings) |
| Fetch from server by ID | Enter an ESAF number; fetches from the server's database |
| Enter manually | Skip parsing; fill in fields by hand |

Click **Load / Parse** to read the ESAF. The dialog advances to the Review tab automatically.

**Tab 2 — Review**

All extracted fields are editable. Label colours indicate extraction confidence:

| Colour | Meaning |
|--------|---------|
| Green | High confidence (≥ 0.7) — field parsed cleanly |
| Orange | Uncertain (0.4–0.7) — plausible but may need checking |
| Red | Low confidence (< 0.4) — field not found or ambiguous |

The user table lists everyone named in the ESAF. Add or remove rows as needed.

**Tab 3 — PI Group**

Select which PI group this ESAF belongs to. If a user in the ESAF is already in a group's `known_members` list, that group is pre-selected and the matched name is shown. Click **Manage…** to edit the group list or create a new group.

If the ESAF server is configured, an optional checkbox uploads the record to the server immediately after saving.

Clicking **OK** saves the ESAF to the local cache at `~/.easy_bluesky/esaf_cache/<esaf_id>.json`.

---

### ESAF server health indicator

The **Experiments tab** left panel shows a persistent live indicator of the ESAF server's status — no need to open settings to see whether the shared database is reachable:

| Dot colour | Text | Meaning |
|-----------|------|---------|
| Grey | ESAF server not configured | No server URL set in Connection Settings |
| Green | Connected · MongoDB | Server reachable; MongoDB backend active |
| Orange | Connected · SQLite | Server reachable; SQLite backend active |
| Red | Unreachable (`host:port`) | Server URL configured but not responding |

The indicator auto-checks whenever you switch profiles or save new connection settings. A small **Check** button next to it triggers an immediate re-check at any time. The check runs in a background thread so the UI stays responsive.

---

### Extra fields (custom key-value pairs)

Every `ESAFRecord` supports an `extra_fields` dictionary for arbitrary user-defined metadata. This field is empty by default so **older ESAF entries are completely unaffected** — they gain an empty `extra_fields: {}` transparently on load.

Use extra fields to record anything not captured by the standard ESAF schema:
- Beamline-specific notes (`"optics_config": "pink_beam"`)
- Approval or safety annotations (`"hazmat_review": "approved 2026-07-01"`)
- Data processing parameters (`"calibration_file": "LaB6_2026.poni"`)

#### Editing from the picker

In the ESAF picker (New Experiment → From ESAF), select an ESAF from the dropdown and click **Edit Extra Fields…**. A dialog shows the current key-value table:

- **Add field** — inserts a new empty row; type the field name and value
- **Remove selected** — deletes the highlighted rows
- **Push to server** checkbox — if an ESAF server URL is configured, the changes are also pushed to the shared database via `PATCH /api/esafs/{id}/extra_fields`. Setting a value to empty removes the key server-side.

The picker summary panel shows the count of extra fields for the selected ESAF at a glance.

#### Via the REST API

Send a `PATCH` request to merge new key-value pairs into an existing ESAF's extra fields:

```bash
curl -X PATCH http://mybeamline:8765/api/esafs/12345/extra_fields \
     -H "X-API-Key: secret-key" \
     -H "Content-Type: application/json" \
     -d '{"fields": {"optics_config": "pink_beam", "calibration_file": "LaB6_2026.poni"}}'
```

Merging is additive: existing keys not present in the request are preserved. To **delete** a key, set its value to `null`:

```bash
curl -X PATCH http://mybeamline:8765/api/esafs/12345/extra_fields \
     -H "X-API-Key: secret-key" \
     -H "Content-Type: application/json" \
     -d '{"fields": {"calibration_file": null}}'
```

The full updated `ESAFRecord` (including the merged `extra_fields`) is returned as JSON.

---

### Plan metadata injection

Every plan added to the queue from an ESAF-linked experiment automatically receives the following fields in its `md` (metadata) kwargs:

| Key | Example | Source |
|-----|---------|--------|
| `esaf_id` | `"12345"` | ESAF number |
| `pi_group` | `"uchicago_john_rogers"` | PI group slug |
| `proposal_id` | `"GUP-67890"` | Proposal / GUP number |
| `esaf_start_date` | `"2026-08-01"` | ESAF beam time start |
| `exp_dir` | `"/Users/…/run_name"` | Local experiment path |
| `remote_exp_dir` | `"/home/…/run_name"` | Remote RE machine path |

These appear in every run's MongoDB `run_start` document and JSONL file, enabling facility-level reporting of usage by PI group.

---

### Remote Data Root

Each connection profile has a **Remote Data Root** field that stores the base path on the Linux RE machine where detector data is written. Set it once in **File → Connection Settings**:

```
Remote Data Directory: /home/chem_epics/data
```

A **Browse…** button opens an SFTP directory browser that connects to the remote host and lets you navigate the filesystem without a terminal. The selected path is stored in the profile and used to auto-populate the remote path whenever you create a new experiment (both in Manual and From ESAF modes).

The remote path is injected into every plan as `remote_exp_dir`. `re_startup_mongo.py` reads this field and calls `os.makedirs(remote_exp_dir, exist_ok=True)` on the RE machine before writing detector HDF files, solving the `PermissionError` that occurs when the detector plugin tries to create directories on the client Mac path.

---

### ESAF Server (optional)

The `esaf_server/` package is a standalone **FastAPI** service that provides:

- A REST API for ESAF records, PDF storage, and PI group management
- An HTML admin UI (Bootstrap 5 dark theme) for staff to enter ESAFs without installing EasyBluesky
- PDF storage via MongoDB GridFS or local filesystem (SQLite backend)
- Shared access: multiple clients (app installations, staff laptops) can read and write the same database

The ESAF server is optional. Local PDF parsing and the local ESAF cache work with no server configured.

#### Starting the server

**Via the app** — open **File → Connection Settings**, scroll to **ESAF Server**, fill in the server URL and API key, then click **Start via SSH**. The app runs `uvicorn esaf_server.main:app` on the remote host via SSH.

**Via terminal**:

```bash
cd easy-bluesky
pip install -e .                            # pdfplumber included; also add fastapi, uvicorn, jinja2
pip install fastapi uvicorn jinja2
uvicorn esaf_server.main:app --host 0.0.0.0 --port 8765
```

The server config lives at `~/.easy_bluesky/esaf_server/config.json`. Default backend is SQLite (zero setup). To use MongoDB:

```json
{
  "backend": "mongodb",
  "mongo_uri": "mongodb://localhost:27017",
  "mongo_db": "esaf_db"
}
```

#### Admin UI

Navigate to `http://<server-host>:8765/admin/` to manage ESAFs and PI groups via a web browser — no app install required. Features:

- List, search, add, edit, and delete ESAF records
- Upload PDF files; re-parse PDFs already on the server
- List, add, edit, and delete PI groups with member lists
- View each ESAF's full detail and download its attached PDF

#### REST API

All endpoints are under `/api/`. Reads are open; writes require the `X-API-Key` header.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/esafs` | List all ESAFs |
| GET | `/api/esafs/{id}` | Get a single ESAF |
| POST | `/api/esafs` | Create an ESAF |
| PUT | `/api/esafs/{id}` | Full update of an ESAF |
| PATCH | `/api/esafs/{id}/extra_fields` | Merge-update `extra_fields`; `null` values delete keys |
| DELETE | `/api/esafs/{id}` | Delete an ESAF |
| POST | `/api/esafs/{id}/pdf` | Upload a PDF |
| GET | `/api/esafs/{id}/pdf` | Download the stored PDF |
| POST | `/api/parse-pdf` | Parse a PDF; returns fields + confidence scores |
| GET | `/api/pi-groups` | List all PI groups |
| POST | `/api/pi-groups` | Create a PI group |
| PUT | `/api/pi-groups/{slug}` | Update a PI group |
| GET | `/health` | Health check; returns `{"status": "ok", "backend": "mongodb"|"sqlite"}` |

#### Configuring the app

In **File → Connection Settings**, under **ESAF Server**:

| Field | Description |
|-------|-------------|
| Server URL | Base URL, e.g. `http://mybeamline:8765` |
| API key | Shared secret for write access (shown as password) |
| Test button | GET `/health`; confirms the server is reachable and shows the backend type |
| Start via SSH | Runs `uvicorn esaf_server.main:app` on the remote host via SSH |

Once configured, the **Import New ESAF…** dialog offers two additional source modes: *Upload PDF to server* and *Fetch from server by ESAF ID*. The ESAF picker also shows a **Refresh from server** button that syncs all server records to the local cache.

#### `pdfplumber` is a core dependency

`pdfplumber` is installed automatically with `pip install easy-bluesky` — no separate step needed. Local PDF parsing works immediately after installation.

---

## Data Storage

### MongoDB (primary)

When MongoDB is configured, run data is written to MongoDB by `re_startup_mongo.py`. The MongoDB Browser reads from the same database.

Documents are stored in collections that match bluesky document names:

| Collection | Contents |
|-----------|---------|
| `run_start` | One doc per run: plan name, scan ID, motor/detector lists, timestamps, `exp_dir` |
| `run_stop` | One doc per run: exit status, timestamps, event counts |
| `event_descriptor` | One doc per stream: field definitions |
| `event` / `event_page` | Event data: timestamps and field readings |
| `resource` / `datum` | File references (area detectors) |

The `exp_dir` field in `run_start` links each run to an experiment and is used by the MongoDB Browser's experiment filter.

### JSONL run files (always-on fallback)

Every run is **also** written to a per-run JSONL file by `re_startup_mongo.py`, regardless of whether MongoDB is configured. Each file is named `<uid>.jsonl` and written to `<exp_dir>/runs/` (over NFS/shared filesystem) or `~/.easy_bluesky/data/runs/` on the RE machine when the experiment path is not accessible.

Each line is a JSON array `[doc_type, doc_body]`:

```jsonl
["start",      {"uid": "abc123", "plan_name": "scan", ...}]
["descriptor", {"uid": "def456", "data_keys": {...}, ...}]
["event",      {"uid": "ghi789", "data": {...}, ...}]
["stop",       {"uid": "jkl012", "exit_status": "success", ...}]
```

The **Experiments tab** uses these JSONL files directly when MongoDB is not configured. Click **Export HDF5…** in the Experiments tab to bundle all JSONL runs from the active experiment into a portable `.h5` file — no MongoDB required.

### Experiment folder layout

**Manual mode** (classic free-form path):

```
experiments/<name>/
├── experiment.json         # experiment metadata (name, created, remote_exp_dir)
├── plans_log.jsonl         # lightweight plan execution log (scan IDs, status, timestamps)
├── console.log             # RE Manager console output (appended each session)
└── runs/
    ├── <uid1>.jsonl        # per-run JSONL data files (NFS-accessible)
    └── <uid2>.jsonl
```

**From ESAF mode** (canonical hierarchy):

```
experiments/
└── uchicago_john_rogers/              ← PI group slug
    └── ESAF-12345_2026-08-01/         ← ESAF number + beam-time start date
        ├── SAXS_day1/                 ← first run
        │   ├── experiment.json        #   includes "esaf" block (see below)
        │   ├── plans_log.jsonl
        │   ├── console.log            #   RE console output for this run
        │   └── runs/
        │       ├── <uid1>.jsonl
        │       └── <uid2>.jsonl
        └── SAXS_day2/                 ← second run, same ESAF
            ├── experiment.json
            ├── plans_log.jsonl
            ├── console.log
            └── runs/
```

`experiment.json` for an ESAF-linked experiment includes an `esaf` block:

```json
{
  "name": "run_name",
  "created": "2026-08-08T09:15:00",
  "description": "",
  "remote_exp_dir": "/home/chem_epics/data/uchicago_john_rogers/ESAF-12345_2026-08-01/2026-08-08/run_name",
  "esaf": {
    "esaf_id": "12345",
    "pi_group": "uchicago_john_rogers",
    "proposal_id": "GUP-67890",
    "esaf_start_date": "2026-08-01",
    "title": "SAXS study of …",
    "beamline": "15-ID-B"
  }
}
```

`plans_log.jsonl` is a fast local index. Full data lives in MongoDB (when configured) and is also mirrored to JSONL run files. Use **Export HDF5…** in the MongoDB Browser (requires MongoDB) or the Experiments tab (JSONL fallback) to produce a portable `.h5` file.

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
  "esaf_server_url": "http://mybeamline:8765",
  "esaf_api_key": "secret-key",
  "profiles": [
    {
      "name": "ASWAXS",
      "devices_file": "devices_aswaxs.py",
      "is_local": false,
      "control_port": 60615,
      "info_port": 60625,
      "doc_port": 60630,
      "procserv_port": 60635,
      "mongo_db": "aswaxs_runs",
      "mongo_host": "localhost",
      "mongo_port": 27017,
      "remote_data_root": "/home/chem_epics/data"
    },
    {
      "name": "Local Sim",
      "devices_file": "devices_sim.py",
      "is_local": true,
      "control_port": 60644,
      "info_port": 60645,
      "doc_port": 60646,
      "procserv_port": 60647,
      "mongo_db": "sim_runs",
      "mongo_host": "localhost",
      "mongo_port": 27017,
      "remote_data_root": ""
    }
  ],
  "deleted_profiles": []
}
```

New fields added by the ESAF integration:

| Field | Scope | Description |
|-------|-------|-------------|
| `esaf_server_url` | top-level | Base URL of the ESAF REST server (empty = server disabled) |
| `esaf_api_key` | top-level | Shared secret for ESAF server write operations |
| `remote_data_root` | per-profile | Root path on the RE machine for detector / experiment data |
```

Environment variable overrides:

| Variable | Default | Description |
|---|---|---|
| `BLUESKY_ZMQ_CONTROL` | `tcp://localhost:60615` | RE Manager control address |
| `BLUESKY_ZMQ_INFO` | `tcp://localhost:60625` | RE Manager info address |
| `BLUESKY_ZMQ_PUB_HOST` | `localhost` | Live doc stream host |
| `BLUESKY_ZMQ_PUB_PORT` | `60630` | Live doc stream port |

---

## Project Structure

```
easy-bluesky/
├── easy_bluesky/
│   ├── main.py               # MainWindow + entry point
│   ├── worker.py             # ZMQ worker thread (RE Manager API)
│   ├── config.py             # Configuration constants (env-overridable)
│   ├── connection_settings.py# Connection dialog + settings I/O + profiles + RemotePathBrowser
│   ├── ssh_manager.py        # SSH-based remote RE Manager control (procServ)
│   ├── devices_editor.py     # Edit Devices File dialog (local read/write + SFTP pull/push)
│   ├── code_editor.py        # CodeEditor widget — line numbers, auto-indent, autocomplete
│   ├── highlighter.py        # Python syntax highlighter (used by CodeEditor)
│   ├── sim_generator.py      # Auto-generate sim devices file from real script
│   ├── re_control_bar.py     # RE control toolbar (status + buttons + profile dropdown)
│   ├── re_console.py         # RE console output tab
│   ├── experiments_tab.py    # Experiments tab (two-tab new-exp dialog, ESAF-aware paths)
│   ├── esaf.py               # ESAF data layer: ESAFRecord, PIGroup, ESAFServerClient, cache
│   ├── esaf_dialog.py        # ESAF Qt dialogs: importer, picker, PI group manager
│   ├── queue_manager.py      # Queue Manager tab
│   ├── plan_builder.py       # Plan Builder tab + code editor (uses CodeEditor)
│   ├── widgets.py            # Shared widgets (ScanArgsWidget, ParamForm, …)
│   ├── live_viewer.py        # Live Viewer (ZMQ + pyqtgraph, screenshot)
│   ├── mongo_browser.py      # MongoDB Browser (multi-run, auto-plot, motor move, screenshot)
│   ├── hdf5_viewer.py        # HDF5 Viewer tab
│   ├── devices_plans_tab.py  # Devices & Plans tab (CA monitor, sim monitor, tweak, search)
│   ├── peak_fit.py           # lmfit models (peaks, steps, background polynomials) + auto-guess
│   ├── curve_fit_dialog.py   # Interactive parameter dialog (initial values, bounds, algorithm)
│   ├── plot_tools.py         # Shared plot utilities (crosshair, smart_legend_position, norm combo)
│   ├── pv_watchdog.py        # PV Watchdog tab
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
├── esaf_server/              # Optional FastAPI ESAF server (MongoDB or SQLite backend)
│   ├── main.py               # FastAPI app: 16 REST endpoints + 11 HTML admin routes
│   ├── models.py             # Pydantic v2 models: ESAFRecord, PIGroup, ParsedPDFResult
│   ├── repository.py         # Abstract repository interfaces + backend factory
│   ├── config.py             # Server config (loads ~/.easy_bluesky/esaf_server/config.json)
│   ├── pdf_parser.py         # pdfplumber-based APS ESAF PDF extractor with confidence scoring
│   ├── backends/
│   │   ├── mongo_backend.py  # MongoDB + GridFS backend
│   │   └── sqlite_backend.py # SQLite + flat-file backend (zero setup)
│   ├── templates/            # Jinja2 HTML templates (Bootstrap 5 dark sidebar)
│   │   ├── base.html
│   │   ├── esaf_list.html
│   │   ├── esaf_form.html
│   │   ├── esaf_detail.html
│   │   ├── pi_group_list.html
│   │   └── pi_group_form.html
│   └── requirements.txt      # fastapi, uvicorn, pydantic, pymongo, pdfplumber, jinja2
├── pyproject.toml
└── README.md
```

### Local ESAF data files

| Path | Content |
|------|---------|
| `~/.easy_bluesky/esaf_cache/<esaf_id>.json` | Cached ESAF records (one file per ESAF) |
| `~/.easy_bluesky/pi_groups.json` | PI group registry |
| `~/.easy_bluesky/esaf_server/config.json` | ESAF server configuration (backend, URI, API key) |
| `~/.easy_bluesky/device_metadata.json` | Cached EPICS units/descriptions (reused in sim mode) |

---

## Changelog

### 2026-08-15 / 2026-08-16

- **Feat: "Next scan: #N" label in Plan Log header** — Displays the next sequential scan number beside the PLAN LOG heading. Recomputed from `plans_log.jsonl` on experiment open and recalculates on every queue add or remove. After app restart it automatically advances past any scan numbers already assigned to plans sitting in the queue.
- **Feat: Scan number locked in at queue time** — Each plan's `md["scan_num"]` is set when the plan is added to the queue (using the current `_next_scan_num`) rather than being computed at execution time from `scans_log.json`. Eliminates HDF filename off-by-one errors caused by stale local `scans_log.json` reads. `custom_plans.py` prefers `md.get("scan_num")` and falls back to `_scan_num_from_log` only if the app did not inject one.
- **Feat: Scan number shown in queue list** — Each queued plan displays `#28  rel_scan_w_time_n_delay  …` instead of a position number. Motion-only plans (`mv`, `sleep`) without a `scan_num` continue to show their position.
- **Feat: "Now Running" banner** — A green `▶ Running: <plan>  [scan #N, sample]` banner appears above the Plan Log while a scan is executing. Disappears automatically on idle, abort, or disconnect. Driven by a new `running_item_updated` signal on `ZMQWorker` that emits `queue["running_item"]` each poll cycle.
- **Fix: Start queue button disabled when RE Manager busy** — `update_re_status` now checks `manager_state` in addition to `re_state`. The Start button is disabled when `manager_state != "idle"` (e.g. during environment open/close), preventing the "RE Manager is busy" rejection error.
- **Fix: "Next scan" label stale on app startup** — `_load_plan_log` (which reads the authoritative `plans_log.jsonl`) now updates `_next_scan_label` immediately after computing `_next_scan_num`. Removed the `directoryChanged → _update_next_scan_label` connection that was resetting the label to the stale `scans_log.json` count after `_load_plan_log` rewrote `plans_log.jsonl`.
- **Feat: Confirmation dialog before removing plans from queue** — Single-item removal shows the plan name; multi-item removal shows the count.
- **Feat: Sample name "already exists" warning** — Committing a sample name whose folder already exists in the experiment directory now shows a Yes/No prompt.
- **Feat: Offer to update sample_name in queued plans** — When the sample name changes, a dialog prompts to patch `md["sample_name"]` in all already-queued plans via `worker.update_item`.
- **Feat: Curve fit parameters remembered per dataset (MongoDB Browser)** — `_saved_fit_states` (keyed by run UIDs + stream + x/y fields) replaces the single `_saved_fit_state`. Re-opening the Fit dialog for the same data auto-runs the fit and shows the full results table (Center, FWHM, R², Amplitude) immediately. Switching data opens a fresh dialog.

### 2026-08-14 (session 2)

- **Fix: MongoDB Browser shows scans from other experiments** — When `plans_log.jsonl` contained UIDs the filter query used `$or [{uid: …}, {exp_dir: regex}]`, which matched any experiment folder sharing the same name (e.g. every old "test" folder). Fixed: when UIDs are available they are used exclusively; the folder-name regex fallback (empty plan log) now uses the last two path components for a tighter match.
- **Fix: History list — single-click shows plan detail** — Clicking a history item now populates the PLAN DETAIL panel on the right, the same way clicking a queue item does. Previously nothing happened on single-click.
- **Fix: Re-queue from history when RE environment is closed** — `_requeue_from_history_dialog` now detects when the plan name is absent from the current allowed-plans list and offers a Yes/No prompt to re-queue with the original arguments directly. Previously the PlanDialog showed a silent "Select a valid plan" error with no escape.
- **Feat: "Re-queue directly" context menu on history items** — Right-click on any history item shows a "Re-queue directly" option that adds the plan back to the queue immediately with its original args/kwargs, bypassing the edit dialog.
- **UI: Swap queue button rows in Experiments tab** — `[Add | Remove | Save | Load | Clear]` moved above the queue list; `[Start | Pause | Resume | Abort | Stop]` + Auto-start/Loop moved below. Queue-building actions are now at the top; execution controls follow naturally after the queue is visible.
- **Feat: Derivative transform in MongoDB Browser** — A `Deriv:` dropdown (`— / dy/dx / d²y/dx²`) added to the bottom bar beside the crosshair label. Uses `np.gradient` (central differences) — no x-shift, same point count as original. Applied after normalization and before log, so `log(dy/dx)` is also available. Error bars propagated using the exact central-difference stencil formula. Fit overlay curves (preview and permanent) receive the same derivative transform so they stay aligned with the data.
- **UI: Log Y and ± Errors moved to bottom bar** — Both controls relocated from the crowded top control bar to the new bottom bar beside the crosshair X/Y coordinates.
- **Fix: Fit overlay not updated when derivative changes** — Changing the Deriv dropdown now redraws fit curves with the new transform. Previously the fit stayed in raw space while the data switched to derivative space. Raw fit data is cached in `_fit_items_cache`; `_auto_plot` clears and redraws from cache on every replot.

### 2026-08-14

- **Fix: devices file case mismatch on Linux** — `re_startup_mongo.py` now loads the devices file by direct path (`spec_from_file_location`) with a case-insensitive glob fallback. Profiles with `devices_file: "devices_aswaxs.py"` now correctly find `devices_ASWAXS.py` on case-sensitive Linux filesystems, fixing empty device lists after RE Manager restart.
- **Fix: Qt main thread blocking from filesystem watcher** — `_on_exp_dir_changed` no longer calls `Path.exists()` on the main thread. Watcher events are debounced (500 ms) and all filesystem probes run in background threads. Parent-directory watching removed to eliminate spurious events on NFS-mounted experiment directories. This was the root cause of ZMQ heartbeat failures that cleared the device list.
- **Fix: Reconnect button now reloads full device list** — Previously, clicking ⟳ Reconnect only re-fetched PV names. It now calls `reload_plans_devices()` so devices are fully re-enumerated from the RE environment. The device fingerprint is also cleared on disconnect so CA monitors are rebuilt correctly after a reconnect.
- **Feat: ESAF technique filter** — Technique dropdown in the ESAF picker narrows results by technique before text search. Selection is persisted per profile across sessions.
- **Feat: Client-side regex ESAF search** — The search field filters across all ESAF record fields (PI name, title, users, institution, proposal ID, ESAF ID, etc.) using Python regex. Replaces the previous server-side partial-text search which missed many fields.
- **Feat: Auto console log** — RE Manager console output is automatically appended to `<exp_dir>/console.log` while an experiment is active. Each session is delimited by a timestamp header.
- **Feat: Experiment folder loss detection** — QFileSystemWatcher + 10-second background health check detect if the active experiment folder is deleted or the NFS mount is lost. A modal dialog notifies the user; running scans are paused automatically.
- **Fix: ESAF folder name** — Folder name uses date only (`ESAF-{ID}_{YYYY-MM-DD}`), with no time component.
- **Fix: ESAF picker button layout** — Removed fixed-size constraints from Check and Open buttons so they render correctly on macOS.

---

## Acknowledgements

EasyBluesky is developed at **NSF's ChemMatCARS, Sector 15** at the Advanced Photon Source (APS), Argonne National Laboratory (ANL).

NSF's ChemMatCARS is supported by the Divisions of Chemistry (CHE) and Materials Research (DMR), National Science Foundation, under grant number **NSF/CHE-2335833**.

Developed with assistance from [Claude](https://claude.ai) (Anthropic).

## License

BSD 3-Clause License
