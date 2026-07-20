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
- **Sim Mode** — Toggle between real hardware and a simulated RE Manager instance with one click. Ports switch automatically.
- **Remote Control** — Start, stop, and restart the RE Manager on a remote host via SSH key authentication (no passwords stored).

---

## Architecture

EasyBluesky separates the **client** (this app) from the **RE Manager host**:

```
┌─────────────────────────────┐          ┌───────────────────────────────┐
│   Client machine            │          │   RE Manager host             │
│   (your laptop/workstation) │          │   (beamline control computer) │
│                             │  ZMQ/TCP │                               │
│   EasyBluesky app  ─────────┼──────────┼──► start-re-manager (real)   │
│                             │          │   start-re-manager (sim)      │
│                             │          │                               │
│   Needs:                    │          │   Needs:                      │
│   • easy-bluesky            │          │   • bluesky-queueserver       │
│   • Python ≥ 3.10           │          │   • hardware ophyd drivers    │
│                             │          │   • startup scripts           │
└─────────────────────────────┘          └───────────────────────────────┘
```

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

### 1. Initialize scripts

Run the app once to create `~/.easy_bluesky/scripts/` with default startup scripts:

```bash
easy-bluesky
```

### 2. Edit the startup script

Open `~/.easy_bluesky/scripts/re_startup_mongo.py` and add your devices:

```python
from ophyd import EpicsMotor

# Your real hardware devices
m1 = EpicsMotor("IOC:m1", name="m1")
```

### 3. Start the RE Manager

```bash
start-re-manager \
  --zmq-control-addr tcp://*:60615 \
  --zmq-info-addr    tcp://*:60625 \
  --zmq-publish-console ON \
  --startup-script   ~/.easy_bluesky/scripts/re_startup_mongo.py \
  --existing-plans-devices ~/.easy_bluesky/scripts/existing_plans_and_devices.yaml \
  --user-group-permissions ~/.easy_bluesky/scripts/user_group_permissions.yaml
```

### 4. Launch the app and connect

```bash
easy-bluesky
```

The app connects to `localhost:60615` by default. Use **File → Connection Settings** to change the host or ports.

---

## Toolbar Overview

The persistent toolbar at the top provides:

| Button | Action |
|--------|--------|
| ▶ Start | Start the plan queue |
| ⏸ Pause / ▶▶ Resume | Pause / resume running plan |
| ✕ Abort / ⬛ Stop | Abort or stop the running plan |
| Open Env / Close Env | Open or close the RE worker environment |
| ⚡ Start RE Mgr | Start (or restart) the RE Manager process |
| ⏹ Stop RE Mgr | Stop the RE Manager process |
| ↺ Reconnect | Reconnect ZMQ without restarting RE Manager |
| 🔬 Real / 🧪 Sim | Toggle simulation mode |

---

## Sim Mode

Sim mode connects to a **second RE Manager instance** running a simulated startup script, so you can test plans without touching real hardware. Both instances run simultaneously on different ports.

### Port layout

| Instance | Control | Info  | Doc stream | procServ mgmt |
|----------|---------|-------|------------|---------------|
| Real     | 60615   | 60625 | 60630      | 60635         |
| Sim      | 60616   | 60626 | 60631      | 60636         |

All ports are configurable in **File → Connection Settings**.

### Generate the sim startup script

**File → Generate Sim Script…** reads your real `re_startup_mongo.py` and auto-generates `re_startup_sim.py`:

- `EpicsMotor` → `SynAxis`
- Area detectors → `SimAreaDetector` (Poisson-noise images)
- Scalers/counters → `SynGauss`
- Generic test devices always included: `motor1`, `motor2`, `det`, `det1`, `det2`, `sim_ad`
- ZMQ doc stream set to sim port 60631
- Separate device list file (`existing_plans_and_devices_sim.yaml`) so real and sim don't overwrite each other

When the host is **remote**, the dialog offers to **copy the generated script directly to the RE Manager host** via SFTP — no manual `scp` needed.

### Starting sim mode

1. Click **🔬 Real** to toggle to **🧪 Sim** — the app attempts to connect to the sim instance. If it is not running, a message appears but the toggle stays in sim mode.
2. Click **⚡ Start RE Mgr** — this starts the sim instance on port 60616 (using procServ on remote hosts).
3. The app auto-reconnects once the port opens.

### Start both instances at once (local)

A convenience script is provided at `~/.easy_bluesky/scripts/start_re_managers.sh`:

```bash
# Start both real and sim instances
~/.easy_bluesky/scripts/start_re_managers.sh

# Start only one
~/.easy_bluesky/scripts/start_re_managers.sh --real-only
~/.easy_bluesky/scripts/start_re_managers.sh --sim-only

# Stop all instances
~/.easy_bluesky/scripts/stop_re_managers.sh
```

Logs are written to `/tmp/re-manager-real.log` and `/tmp/re-manager-sim.log`.

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
- **Control / Info / Doc ports** — match what the RE Manager was started with
- **Sim ports** — same, for the sim instance

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
- Logs RE Manager output to `/tmp/re-manager-real.log` (or `-sim.log`)
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

1. Writes a launcher shell script to `/tmp/_easy_bluesky_start_{real|sim}.sh` via SFTP
2. Kills the existing instance for that mode only (via procServ PID file)
3. Launches `procServ ... /bin/bash /tmp/_easy_bluesky_start_{real|sim}.sh`
4. Waits (polling every 2 s) until the ZMQ control port opens, then reconnects

**⏹ Stop RE Mgr** kills only the instance for the current mode (real or sim), leaving the other running.

#### Service name field (systemd alternative)

If you have a systemd user service set up, enter its name (e.g. `re-manager-real`) in the **Service name** field. The app will use `systemctl --user restart/stop <service>` instead of procServ.

#### Remote startup scripts

The startup scripts must exist on the RE Manager host at `~/.easy_bluesky/scripts/`. Copy them once:

```bash
# From the client machine
scp ~/.easy_bluesky/scripts/re_startup_mongo.py \
    ~/.easy_bluesky/scripts/existing_plans_and_devices.yaml \
    ~/.easy_bluesky/scripts/user_group_permissions.yaml \
    user@your-beamline-host:~/.easy_bluesky/scripts/
```

The sim startup script can be copied automatically via **File → Generate Sim Script… → Copy to Remote?**.

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

### `re_startup_mongo.py` — real hardware

```python
from ophyd import EpicsMotor

# Devices
m1 = EpicsMotor("IOC:m1", name="m1")
```

The script also sets up suitcase.jsonl data routing and ZMQ doc publishing on port 60630. Edit the devices section — the rest is handled automatically.

### `re_startup_sim.py` — simulation

Auto-generated by **File → Generate Sim Script…**. Contains simulated equivalents of your real devices plus generic test devices:

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

ZMQ doc stream is published on sim port 60631.

### YAML permission files

- `existing_plans_and_devices.yaml` — device/plan list for the real instance
- `existing_plans_and_devices_sim.yaml` — device/plan list for the sim instance (written separately so the two instances don't overwrite each other)
- `user_group_permissions.yaml` — controls which user groups can run which plans

---

## Configuration Reference

Connection settings are stored in `~/.easy_bluesky/connection.json` (local only, never committed to git):

```json
{
  "host": "192.168.1.50",
  "control_port": 60615,
  "info_port": 60625,
  "doc_port": 60630,
  "sim_control_port": 60616,
  "sim_info_port": 60626,
  "sim_doc_port": 60631,
  "ssh_user": "beamline",
  "ssh_port": 22,
  "ssh_key_path": "~/.ssh/id_ed25519",
  "ssh_service": "",
  "conda_env": "easy-bluesky",
  "conda_path": "~/anaconda3",
  "procserv_port": 60635,
  "sim_procserv_port": 60636
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
│   ├── connection_settings.py# Connection dialog + settings I/O
│   ├── ssh_manager.py        # SSH-based remote RE Manager control (procServ)
│   ├── sim_generator.py      # Auto-generate sim startup script from real one
│   ├── re_control_bar.py     # RE control toolbar (status + buttons + sim toggle)
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
│       ├── re_startup_mongo.py
│       ├── re_startup_sim.py
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
