# Bluesky Desktop App

A PyQt6 desktop application for controlling and monitoring Bluesky experiments via the Run Engine queue server (ZMQ transport).

## Features

- **Queue Manager** — Add, edit, delete, and reorder plans in the queue. Full RE controls (start, pause, resume, abort, stop). Live status indicator and console output.
- **Plan Builder** — Visual drag-and-drop plan canvas with auto-generated parameter forms. Python code editor with syntax highlighting and starter templates. Upload plans directly to the RE or save to file.
- **Live Viewer** — Real-time pyqtgraph plots fed by Kafka. Auto-detects signals, 60fps OpenGL rendering, multi-signal overlay.
- **Data Browser** — Browse historical runs from the Databroker catalog. Multi-run and multi-column plotting, summary statistics.

## Requirements

- Python ≥ 3.10
- conda environment recommended (`conda activate pydm`)
- Running RE Manager (`start-re-manager`)
- Running Kafka + Zookeeper (for live viewer)
- Databroker catalog configured (for data browser)

## Installation

```bash
# Clone the repo
git clone https://github.com/yourorg/bluesky-app.git
cd bluesky-app

# Install in editable mode
conda activate pydm
pip install -e ".[dev]"
```

## Running

```bash
bluesky-app
# or
python -m bluesky_app.main
```

## Configuration

All settings can be overridden via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BLUESKY_ZMQ_CONTROL` | `tcp://localhost:60615` | RE Manager ZMQ control address |
| `BLUESKY_ZMQ_INFO` | `tcp://localhost:60625` | RE Manager ZMQ info address |
| `BLUESKY_KAFKA_SERVER` | `localhost:9092` | Kafka bootstrap server |
| `BLUESKY_KAFKA_TOPIC` | `bluesky.runengine.documents` | Kafka topic |
| `BLUESKY_CATALOG` | `bluesky_local` | Databroker catalog name |

Example — connecting to a remote RE Manager:

```bash
BLUESKY_ZMQ_CONTROL=tcp://192.168.1.100:60615 \
BLUESKY_ZMQ_INFO=tcp://192.168.1.100:60625 \
bluesky-app
```

## Project Structure

```
bluesky-app/
├── bluesky_app/
│   ├── __init__.py      # Package metadata
│   ├── config.py        # Configuration constants (env-overridable)
│   ├── styles.py        # Qt stylesheet
│   ├── worker.py        # ZMQ worker thread (RE Manager API)
│   ├── highlighter.py   # Python syntax highlighter
│   ├── widgets.py       # ParamForm, PlanDialog
│   ├── queue_manager.py # Queue Manager tab
│   ├── plan_builder.py  # Plan Builder tab
│   ├── live_viewer.py   # Live Viewer tab (Kafka + pyqtgraph)
│   ├── data_browser.py  # Data Browser tab (Databroker)
│   └── main.py          # MainWindow + entry point
├── tests/
│   ├── __init__.py
│   └── test_worker.py
├── docs/
│   └── architecture.md
├── scripts/
│   └── start_services.sh
├── pyproject.toml
├── .gitignore
└── README.md
```

## Development

```bash
# Run tests
pytest tests/

# Format code
black bluesky_app/

# Lint
ruff check bluesky_app/
```

## Adding a New Tab

1. Create `bluesky_app/my_tab.py` with a `QWidget` subclass
2. Import and add it in `bluesky_app/main.py`
3. Add any config constants to `bluesky_app/config.py`

## License

BSD 3-Clause License
