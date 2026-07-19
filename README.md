# EasyBluesky

A PyQt6 desktop application for controlling and monitoring Bluesky experiments via the bluesky-queueserver (ZMQ transport).

## Features

- **Queue Manager** — Add, reorder, and delete plans in the queue. Full RE controls (open environment, start, pause, resume, abort, stop). Live status indicator and console output.
- **Plan Builder** — Auto-generated parameter forms for any allowed plan. Upload plans directly to the queue.
- **Live Viewer** — Real-time pyqtgraph plots streamed over ZMQ. Auto-detects motor vs. detector signals, supports `time` and `seq_num` as axis columns, updates point-by-point as the scan runs.
- **Data Browser** — Browse completed runs from the local `data/runs/` directory (suitcase-jsonl format). Multi-column plotting with summary statistics.

## Requirements

- Python ≥ 3.10
- [bluesky-queueserver](https://blueskyproject.io/bluesky-queueserver/) 0.0.25
- [bluesky-queueserver-api](https://blueskyproject.io/bluesky-queueserver-api/) 0.0.13
- Redis (required by the queue server)
- PyQt6, pyqtgraph, suitcase-jsonl, pyzmq

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
conda activate easy-bluesky
bash scripts/start_services.sh
```

This starts:
- **RE Manager** on ZMQ ports 60615 (control) and 60625 (console)
- **Tiled** serving `data/runs/` on port 8000 (read-only, auto-watches for new files)

Or start the RE Manager manually:

```bash
start-re-manager \
  --zmq-publish-console ON \
  --existing-plans-devices scripts/existing_plans_and_devices.yaml \
  --user-group-permissions scripts/user_group_permissions.yaml \
  --startup-script scripts/re_startup_mongo.py
```

### 2. Launch the app

```bash
conda activate easy-bluesky
python -m easy_bluesky.main
```

### 3. Run a scan

1. **Queue Manager** → click **Open Environment** → wait for status to show `idle`
2. **Plan Builder** → select `scan`, fill in detectors/motor/start/stop/points → **Add to Queue**
3. **Queue Manager** → click **Start Queue**
4. **Live Viewer** — plots update in real time as events arrive
5. **Data Browser** → click **Connect** → select the completed run → choose axes → **Plot**

## Data Storage

Runs are written as JSONL files to `data/runs/` using [suitcase-jsonl](https://blueskyproject.io/suitcase-jsonl/). Each file is named by the run UID (e.g. `abc12345-....jsonl`) and contains one bluesky document per line.

The Data Browser reads these files directly — no database required.

## Live Viewer

The startup script (`scripts/re_startup_mongo.py`) binds a ZMQ PUB socket on port **60630** and subscribes it to the RunEngine. The Live Viewer connects a ZMQ SUB socket to this port and receives `start`, `descriptor`, `event`, and `stop` documents in real time.

No Kafka broker or bridge is needed.

## Configuration

All settings can be overridden via environment variables:

| Variable | Default | Description |
|---|---|---|
| `BLUESKY_ZMQ_CONTROL` | `tcp://localhost:60615` | RE Manager control address |
| `BLUESKY_ZMQ_INFO` | `tcp://localhost:60625` | RE Manager console address |
| `BLUESKY_ZMQ_PUB_PORT` | `60630` | ZMQ PUB port for live document streaming |
| `BLUESKY_DATA_DIR` | `data/runs` | Directory for suitcase-jsonl output |

## Project Structure

```
easy-bluesky/
├── easy_bluesky/
│   ├── config.py        # Configuration constants (env-overridable)
│   ├── worker.py        # ZMQ worker thread (RE Manager API)
│   ├── queue_manager.py # Queue Manager tab
│   ├── plan_builder.py  # Plan Builder tab
│   ├── live_viewer.py   # Live Viewer tab (ZMQ + pyqtgraph)
│   ├── data_browser.py  # Data Browser tab (local JSONL files)
│   └── main.py          # MainWindow + entry point
├── scripts/
│   ├── re_startup_mongo.py          # RE startup script (devices, plans, storage)
│   ├── existing_plans_and_devices.yaml
│   ├── user_group_permissions.yaml
│   └── start_services.sh
├── data/
│   └── runs/            # JSONL run files written here
├── pyproject.toml
└── README.md
```

## License

BSD 3-Clause License
