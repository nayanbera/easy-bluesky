# Architecture

## Overview

```
bluesky-app
├── UI Layer (PyQt6)
│   ├── QueueManager    — queue control, RE buttons, history
│   ├── PlanBuilder     — visual canvas + code editor
│   ├── LiveViewer      — Kafka streaming + pyqtgraph
│   └── DataBrowser     — Databroker historical runs
│
├── Communication Layer
│   ├── ZMQWorker       — bluesky-queueserver-api ZMQ transport
│   ├── KafkaThread     — confluent-kafka consumer (QThread)
│   └── DataBrowser     — databroker catalog access
│
└── Config Layer
    └── config.py       — all constants, env-overridable
```

## Thread Model

```
Main Thread (Qt event loop)
    ├── UI updates (all Qt widget changes)
    └── ZMQ calls (blocking — called from poll thread via signals)

poll_thread (daemon thread)
    └── ZMQWorker.poll() — polls RE Manager every 1s
        └── emits Qt signals → main thread updates UI

KafkaThread (QThread)
    └── polls Kafka every 0.5s
        └── emits doc_received signal → LiveViewer._on_doc()
```

## Data Flow

### Live scan
```
RE Manager → Kafka publisher (startup script)
           → Kafka topic (bluesky.runengine.documents)
           → KafkaThread.doc_received signal
           → LiveViewer._on_doc()
           → pyqtgraph plot update
```

### Queue control
```
User clicks button
→ ZMQWorker method (e.g. queue_start())
→ bluesky-queueserver-api ZMQ call
→ RE Manager acts
→ ZMQWorker.poll() detects state change
→ status_updated signal
→ QueueManager.update_status()
```

### Historical data
```
DataBrowser.run_list click
→ databroker.catalog[uid].primary.read()
→ xarray Dataset → pandas DataFrame
→ pyqtgraph plot
```

## Adding New Features

### New RE Manager command
Add a method to `ZMQWorker` in `worker.py`:
```python
def my_command(self, arg):
    try:
        r = self.rm.my_command(arg=arg)
        return r.get("success", False), r.get("msg", "")
    except Exception as e:
        return False, str(e)
```

### New plan parameter type
Add a branch in `ParamForm._make_widget()` in `widgets.py`.

### New tab
1. Create `bluesky_app/my_tab.py` with a `QWidget` subclass
2. In `main.py`: `from .my_tab import MyTab`
3. In `MainWindow._setup_ui()`: `self.tabs.addTab(MyTab(), "My Tab")`
