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
- **Sim Mode** — Toggle between real hardware and a simulated RE Manager instance with one click.
- **Remote Restart** — Restart the RE Manager on a remote machine via SSH key authentication (no passwords).

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

Only `bluesky-queueserver` needs to be installed — not the full EasyBluesky app:

```bash
pip install bluesky-queueserver
```

> **Startup scripts** (`re_startup_mongo.py`, YAML permission files) must also be present on the RE Manager host. See [Startup Scripts](#startup-scripts) below.

---

## Quick Start (local — same machine)

### 1. Initialize scripts

Run the app once to create `~/.easy_bluesky/scripts/` with default startup scripts:

```bash
easy-bluesky
```

Or create the scripts directory directly without launching the UI:

```bash
python -c "from easy_bluesky.worker import _get_scripts_dir; _get_scripts_dir()"
```

### 2. Edit the startup script

Open `~/.easy_bluesky/scripts/re_startup_mongo.py` and add your devices:

```python
from ophyd import EpicsMotor, Component as Cpt
from bluesky.callbacks.zmq import Publisher

# Your real hardware devices
m1 = EpicsMotor("IOC:m1", name="m1")
det = ...

# ZMQ doc stream — must match the doc port in Connection Settings
pub = Publisher("localhost:60630")
RE.subscribe(pub)
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

## Sim Mode

Sim mode connects to a **second RE Manager instance** running a simulated startup script, so you can test plans without touching real hardware. Both instances can run simultaneously.

### Port layout

| Instance | Control | Info | Doc stream |
|----------|---------|------|------------|
| Real     | 60615   | 60625 | 60630     |
| Sim      | 60616   | 60626 | 60631     |

These are the defaults; all six ports are configurable in **File → Connection Settings → Sim Mode Ports**.

### Generate the sim startup script

**File → Generate Sim Script…** reads your real `re_startup_mongo.py`, auto-replaces `EpicsMotor` with `SynAxis`, area detectors with `SimAreaDetector`, and scalers with `SynGauss`. Review and edit the generated `re_startup_sim.py` before use.

The sim startup script must publish to the sim doc port:

```python
# In re_startup_sim.py — note port 60631, not 60630
pub = Publisher("localhost:60631")
RE.subscribe(pub)
```

### Start both instances at once

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

Port numbers can be overridden with environment variables:

```bash
REAL_CTRL_PORT=60615 SIM_CTRL_PORT=60616 \
  ~/.easy_bluesky/scripts/start_re_managers.sh
```

Logs are written to `/tmp/re-manager-real.log` and `/tmp/re-manager-sim.log`.

### Toggle sim mode in the app

Click **🔬 Real** in the toolbar to toggle to **🧪 Sim**. The app immediately reconnects the ZMQ worker and the live doc stream to the sim instance ports. Toggling back reconnects to the real ports. No manual port changes needed.

### Running as a systemd service (recommended for production)

Systemd keeps the RE Manager running across reboots and restarts it automatically on failure. Service templates are provided at `~/.easy_bluesky/scripts/`.

**1. Find the full path to `start-re-manager` in your environment:**

```bash
conda activate bluesky        # or your environment name
which start-re-manager        # copy this path
```

**2. Edit the template — replace the two placeholders:**

```bash
# Edit both files
nano ~/.easy_bluesky/scripts/re-manager-real.service
nano ~/.easy_bluesky/scripts/re-manager-sim.service
```

Replace `YOUR_USER` with your Linux username and `/path/to/start-re-manager` with the path from step 1.

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

**Using systemd with the EasyBluesky SSH restart:**

In **Connection Settings → Service name**, enter `re-manager-real` (or `re-manager-sim`). The **⚡ Start RE Mgr** button will SSH in and run `systemctl --user restart <service>` instead of killing and relaunching the process directly. This is cleaner and respects the `Restart=on-failure` policy.

> **Note:** The SSH user must be the same user who owns the systemd service. No `sudo` is needed for `--user` services.

---

## Remote RE Manager

When the RE Manager runs on a different machine, you only need network access (ZMQ/TCP) and optionally SSH for remote restarts.

### Connection settings

Open **File → Connection Settings** and set:
- **Host / IP** — hostname or IP of the RE Manager machine
- **Control / Info / Doc ports** — match what the RE Manager was started with
- **Sim ports** — same, for the sim instance

The app will reconnect immediately after clicking OK.

### Remote restart via SSH (key auth — no passwords)

The **⚡ Start RE Mgr** button can SSH into the remote host and restart the RE Manager. No passwords are stored or committed to git — only the **path** to your private key is saved in `~/.easy_bluesky/connection.json` (a local file, never in the repo).

#### One-time setup

**1. Generate an SSH key pair** on your client machine (skip if you already have one):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

**2. Copy the public key to the RE Manager host:**

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-beamline-host
```

**3. Verify you can log in without a password:**

```bash
ssh -i ~/.ssh/id_ed25519 user@your-beamline-host echo ok
```

**4. In the app**, open **File → Connection Settings → Remote SSH Management**:

| Field | Example | Notes |
|-------|---------|-------|
| SSH user | `beamline` | Username on the remote machine |
| SSH port | `22` | Default SSH port |
| Private key | `~/.ssh/id_ed25519` | Local path only — never committed |
| Service name | `re-manager` | systemd service name, or leave empty |

Click **Test SSH Connection** to verify before saving.

#### Service name field

- **Empty** (default): uses `pkill` + `nohup start-re-manager` — works without systemd.
- **Service name** (e.g. `re-manager`): runs `sudo systemctl restart re-manager` — requires the service to be set up and the SSH user to have passwordless sudo for that command.

#### Remote startup scripts

The startup scripts must exist on the RE Manager host at `~/.easy_bluesky/scripts/`. Copy them once:

```bash
# From the client machine
scp ~/.easy_bluesky/scripts/* user@your-beamline-host:~/.easy_bluesky/scripts/
```

Or initialize them directly on the host:

```bash
# On the RE Manager host (no display needed)
pip install easy-bluesky
python -c "from easy_bluesky.worker import _get_scripts_dir; _get_scripts_dir()"
# Then edit ~/.easy_bluesky/scripts/re_startup_mongo.py with your hardware
```

#### Using it

With the host set to a non-localhost IP and SSH configured, clicking **⚡ Start RE Mgr** will:
1. SSH into the remote host
2. Stop any running RE Manager process
3. Start the correct instance (real or sim, based on the toolbar toggle)
4. Auto-reconnect the app after 8 seconds

---

## Startup Scripts

Scripts live at `~/.easy_bluesky/scripts/` and are auto-created on first run.

### `re_startup_mongo.py` — real hardware

```python
from ophyd import EpicsMotor
from bluesky.callbacks.zmq import Publisher

# Devices
m1 = EpicsMotor("IOC:m1", name="m1")

# ZMQ doc publisher — port must match Connection Settings → Doc stream port
pub = Publisher("localhost:60630")
RE.subscribe(pub)
```

### `re_startup_sim.py` — simulation

Auto-generated by **File → Generate Sim Script…**. Uses `ophyd.sim.SynAxis`, `SynGauss`, and a full `SimAreaDetector` that produces Poisson-noise images. Review after generation — devices can be renamed or adjusted.

```python
from ophyd.sim import SynAxis, SynGauss
from bluesky.callbacks.zmq import Publisher

m1 = SynAxis(name="m1")
det = SynGauss("det", motor=m1, motor_field="m1", center=0, Imax=1, sigma=1)

# Sim doc port — different from real port
pub = Publisher("localhost:60631")
RE.subscribe(pub)
```

### YAML permission files

- `existing_plans_and_devices.yaml` — declares which plans and devices are visible to the queue server. Add custom plans here.
- `user_group_permissions.yaml` — controls which user groups can run which plans.

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
  "ssh_service": ""
}
```

These can also be set via environment variables (useful for scripting):

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
│   ├── ssh_manager.py        # SSH-based remote RE Manager restart
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
