# EasyBluesky

A PyQt6 desktop application for controlling and monitoring Bluesky experiments via the bluesky-queueserver (ZMQ transport).

## Features

- **Experiments** — Create and manage experiments with sample metadata, plan log, motor/detector summaries, and overlay plotting.
- **Queue Manager** — Add, reorder, and delete plans. Full RE controls (open environment, start, pause, resume, abort, stop).
- **Plan Builder** — Two-panel interface: a **Visual Composer** for assembling scan sequences from drag-and-drop blocks (no Python required), and a **Code Editor** for full custom plans with syntax highlighting, auto-indent, and templates.
- **Live Viewer** — Real-time pyqtgraph plots streamed over ZMQ. Crosshair cursor, point-hover tooltip, double-click motor move.
- **History Plot** — Browse completed runs. Multi-select overlay with common-column intersection.
- **HDF5 Viewer** — Open exported HDF5 archives, browse scans, overlay plots, view metadata.
- **RE Console** — Live console output from the RE Manager (color-coded for errors/warnings/success).
- **Instance Profiles** — Run multiple named RE Manager instances simultaneously (e.g. `ASWAXS`, `SURF`, `Sim`) each with its own device set and auto-assigned ports. Switch profiles from the toolbar.
- **Local Profiles** — Run RE Manager as a local subprocess with zero setup. Starts automatically when you launch the profile and stops when you close the app. Ideal for learning and testing with simulated devices.
- **Edit Devices File** — Full code editor for any profile's devices file: line numbers, current-line highlight, auto-indent, Tab→spaces, and ophyd-aware autocomplete. Local profiles read/write the file on disk; remote profiles pull from and push to the RE machine via SFTP.
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
│   ├── devices_editor.py     # Edit Devices File dialog (local read/write + SFTP pull/push)
│   ├── code_editor.py        # CodeEditor widget — line numbers, auto-indent, autocomplete
│   ├── highlighter.py        # Python syntax highlighter (used by CodeEditor)
│   ├── sim_generator.py      # Auto-generate sim devices file from real script
│   ├── re_control_bar.py     # RE control toolbar (status + buttons + profile dropdown)
│   ├── re_console.py         # RE console output tab
│   ├── experiments_tab.py    # Experiments tab (plan log, plots, HDF5 export)
│   ├── queue_manager.py      # Queue Manager tab
│   ├── plan_builder.py       # Plan Builder tab + code editor (uses CodeEditor)
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

EasyBluesky is developed at **NSF's ChemMatCARS, Sector 15** at the Advanced Photon Source (APS), Argonne National Laboratory (ANL).

NSF's ChemMatCARS is supported by the Divisions of Chemistry (CHE) and Materials Research (DMR), National Science Foundation, under grant number **NSF/CHE-2335833**.

Developed with assistance from [Claude](https://claude.ai) (Anthropic).

## License

BSD 3-Clause License
