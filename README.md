# EasyBluesky

A PyQt6 desktop application for controlling and monitoring Bluesky experiments via the bluesky-queueserver (ZMQ transport).

## Features

- **Experiments** — Create and manage experiments with sample metadata. Plan log with scan numbers, motor/detector summaries, multi-select overlay plotting, and failed-plan error dialogs.
- **Queue Manager** — Add, reorder, and delete plans. Full RE controls (open environment, start, pause, resume, abort, stop). Live status indicator and console output.
- **Plan Builder** — Auto-generated parameter forms for any allowed plan. Upload plans directly to the queue.
- **Live Viewer** — Real-time pyqtgraph plots streamed over ZMQ. Crosshair cursor, point-hover tooltip, double-click motor move. Auto-detects motor vs. detector signals.
- **History Plot** — Browse completed runs from the experiment folder. Multi-select overlay with common-column intersection.
- **HDF5 Viewer** — Open exported HDF5 archives, browse scans in a list, select and overlay plots, view scan metadata.

## Requirements

- Python ≥ 3.10
- [bluesky-queueserver](https://blueskyproject.io/bluesky-queueserver/) 0.0.25
- [bluesky-queueserver-api](https://blueskyproject.io/bluesky-queueserver-api/) 0.0.13
- PyQt6, pyqtgraph, suitcase-jsonl, pyzmq, h5py, pandas, numpy

## Installation

```bash
git clone https://github.com/nayanbera/easy-bluesky.git
cd easy-bluesky

conda create -n easy-bluesky python=3.11
conda activate easy-bluesky
pip install -e ".[dev]"
```

## Quick Start

### 1. Start the RE Manager

```bash
start-re-manager \
  --zmq-publish-console ON \
  --existing-plans-devices scripts/existing_plans_and_devices.yaml \
  --user-group-permissions scripts/user_group_permissions.yaml \
  --startup-script scripts/re_startup_mongo.py
```

### 2. Launch the app

```bash
# Local RE Manager (default)
./launch.sh

# Remote RE Manager by IP or hostname
./launch.sh 192.168.1.50
./launch.sh beamline-control
```

Or manually:

```bash
conda activate easy-bluesky
python -c "from easy_bluesky.main import main; main()"
```

### 3. Run a scan

1. **Experiments** → New Experiment → enter a sample name
2. Click **Open Environment** in the RE control bar → wait for `idle`
3. **＋ Add** a plan → **Start** the queue
4. **Live** tab plots update in real time; **History** tab shows completed runs
5. **Export HDF5…** to save all scan data to a single portable file

## Remote Connection

All ZMQ addresses are configurable via environment variables or the `launch.sh` script:

| Variable | Default | Description |
|---|---|---|
| `BLUESKY_ZMQ_CONTROL` | `tcp://localhost:60615` | RE Manager control address |
| `BLUESKY_ZMQ_INFO` | `tcp://localhost:60625` | RE Manager info address |
| `BLUESKY_ZMQ_PUB_HOST` | `localhost` | Host for live document streaming |
| `BLUESKY_ZMQ_PUB_PORT` | `60630` | Port for live document streaming |

## Data Storage

Runs are written as JSONL files to `<experiment>/runs/` using [suitcase-jsonl](https://blueskyproject.io/suitcase-jsonl/). Each experiment folder contains:

```
experiments/<timestamp>_<name>/
├── experiment.json       # experiment metadata
├── plans_log.jsonl       # plan execution log (with scan numbers)
├── runs/                 # JSONL run files (one per scan UID)
└── samples/<name>/       # sample-specific data folders
```

## Project Structure

```
easy-bluesky/
├── easy_bluesky/
│   ├── config.py           # Configuration constants (env-overridable)
│   ├── worker.py           # ZMQ worker thread (RE Manager API)
│   ├── main.py             # MainWindow + entry point
│   ├── experiments_tab.py  # Experiments tab (plan log, plots, HDF5 export)
│   ├── hdf5_viewer.py      # HDF5 Viewer tab
│   ├── queue_manager.py    # Queue Manager tab
│   ├── plan_builder.py     # Plan Builder tab
│   ├── live_viewer.py      # Live Viewer (ZMQ + pyqtgraph)
│   ├── plot_tools.py       # Shared crosshair / tooltip helper
│   └── re_control_bar.py   # RE control bar (status + buttons)
├── scripts/
│   ├── re_startup_mongo.py           # RE startup script
│   ├── existing_plans_and_devices.yaml
│   └── user_group_permissions.yaml
├── launch.sh               # Launch script (local or remote)
├── pyproject.toml
└── README.md
```

## Acknowledgements

This application was developed with the assistance of [Claude](https://claude.ai) (Anthropic), an AI assistant, which contributed to the design and implementation of the codebase.

## License

BSD 3-Clause License
