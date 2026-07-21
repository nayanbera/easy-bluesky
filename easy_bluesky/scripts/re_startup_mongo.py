"""
re_startup_mongo.py
-------------------
Bluesky RE startup script loaded by the queue server worker process.

IMPORTANT: This script must define `RE` (a RunEngine instance) — the queue
server uses whatever `RE` it finds in the module namespace.

Defines:
  - RE: RunEngine instance
  - Simulated ophyd devices (motor1, motor2, det1, det2, det, motor)
  - Standard bluesky plans (scan, count, rel_scan, etc.)
  - Subscribes suitcase.jsonl serializer; routes each run's JSONL data to
    <active_experiment>/runs/ (reads data/active_experiment.json per run).
    Falls back to data/runs/ when no experiment is active.
  - Publishes documents on ZMQ PUB port 60630 for the Live Viewer

Environment variables:
    BLUESKY_DATA_DIR      (default: <project_root>/data/runs)
    BLUESKY_ZMQ_PUB_PORT  (default: 60630)
"""

import os
import sys
from pathlib import Path

# ── Run Engine ─────────────────────────────────────────────────────────────────
from bluesky import RunEngine
RE = RunEngine({})

# ── Hardware devices (from devices.py) ─────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
try:
    from devices import *
    print("[re_startup_mongo] devices.py loaded")
except ImportError as _e:
    print(f"[re_startup_mongo] WARNING: devices.py not found ({_e})")
except Exception as _e:
    print(f"[re_startup_mongo] ERROR loading devices.py: {_e}")
    raise

# ── Standard bluesky plans ─────────────────────────────────────────────────────
from bluesky.plans import (
    count, scan, rel_scan, grid_scan, rel_grid_scan,
    adaptive_scan, tune_centroid,
    spiral, spiral_fermat,
)
from bluesky.plan_stubs import mv, mvr, sleep, rd

print("[re_startup_mongo] RE created, devices and plans loaded")

# ── suitcase.jsonl serializer ──────────────────────────────────────────────────
_script_dir = Path(__file__).parent
_default_data_dir = _script_dir.parent / "data" / "runs"
_DATA_DIR = Path(os.getenv("BLUESKY_DATA_DIR", str(_default_data_dir)))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_ACTIVE_EXP_FILE = _script_dir.parent / "data" / "active_experiment.json"

try:
    import suitcase.jsonl
    from event_model import RunRouter

    def _jsonl_factory(name, doc):
        runs_dir = _DATA_DIR  # fallback
        try:
            if _ACTIVE_EXP_FILE.exists():
                import json as _j
                info = _j.loads(_ACTIVE_EXP_FILE.read_text())
                candidate = Path(info["path"]) / "runs"
                candidate.mkdir(parents=True, exist_ok=True)
                runs_dir = candidate
                print(f"[re_startup_mongo] run → {runs_dir}")
            else:
                print(f"[re_startup_mongo] no active experiment — run → {runs_dir}")
        except Exception as e:
            print(f"[re_startup_mongo] routing error ({e}) — falling back to {runs_dir}")
        return [suitcase.jsonl.Serializer(str(runs_dir))], []

    RE.subscribe(RunRouter([_jsonl_factory]))
    print(f"[re_startup_mongo] suitcase.jsonl (RunRouter) ready"
          f" — fallback dir: {_DATA_DIR}")
except Exception as e:
    print(f"[re_startup_mongo] WARNING: suitcase.jsonl not subscribed: {e}")

# ── ZMQ PUB for Live Viewer ────────────────────────────────────────────────────
_ZMQ_PUB_PORT = int(os.getenv("BLUESKY_ZMQ_PUB_PORT", "60630"))
try:
    import zmq as _zmq
    import json as _json
    from event_model import sanitize_doc as _sanitize

    _zmq_ctx  = _zmq.Context()
    _zmq_sock = _zmq_ctx.socket(_zmq.PUB)
    _zmq_sock.bind(f"tcp://*:{_ZMQ_PUB_PORT}")

    def _zmq_publish(name, doc):
        try:
            _zmq_sock.send_string(_json.dumps([name, dict(_sanitize(doc))]))
        except Exception:
            pass

    RE.subscribe(_zmq_publish)
    print(f"[re_startup_mongo] ZMQ PUB → tcp://*:{_ZMQ_PUB_PORT}")
except Exception as e:
    print(f"[re_startup_mongo] WARNING: ZMQ PUB not started: {e}")
