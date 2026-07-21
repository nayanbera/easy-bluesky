"""
re_startup_mongo.py
-------------------
Bluesky RE startup script loaded by the queue server worker process.

IMPORTANT: This script must define `RE` (a RunEngine instance) — the queue
server uses whatever `RE` it finds in the module namespace.

The devices file is selected via the EASY_BLUESKY_DEVICES_FILE environment
variable (default: devices.py). This allows each named profile to load its own
devices file (e.g. devices_sim.py) without a separate startup script.

Defines:
  - RE: RunEngine instance
  - All names exported by the active devices file
  - Standard bluesky plans (scan, count, rel_scan, etc.)
  - Subscribes suitcase.jsonl serializer; routes each run's JSONL data to
    <active_experiment>/runs/ (reads data/active_experiment.json per run).
    Falls back to data/runs/ when no experiment is active.
  - Publishes documents on ZMQ PUB port 60630 for the Live Viewer

Environment variables:
    EASY_BLUESKY_DEVICES_FILE  (default: devices.py)
    BLUESKY_DATA_DIR           (default: <project_root>/data/runs)
    BLUESKY_ZMQ_PUB_PORT       (default: 60630)
"""

import importlib
import os
import sys
from pathlib import Path

# ── Run Engine ─────────────────────────────────────────────────────────────────
from bluesky import RunEngine
RE = RunEngine({})

# ── Hardware devices (from the profile's devices file) ─────────────────────────
_devices_file = os.getenv("EASY_BLUESKY_DEVICES_FILE", "devices.py")

if os.path.isabs(_devices_file):
    # Full absolute path — load directly regardless of sys.path
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_easy_bluesky_devices", _devices_file)
    _mod  = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    print(f"[re_startup_mongo] {_devices_file} loaded")
else:
    # Relative filename — look in the same directory as this startup script
    sys.path.insert(0, str(Path(__file__).parent))
    _devices_module = _devices_file[:-3] if _devices_file.endswith(".py") else _devices_file
    try:
        _mod = importlib.import_module(_devices_module)
        print(f"[re_startup_mongo] {_devices_file} loaded")
    except ImportError as _e:
        print(f"[re_startup_mongo] WARNING: {_devices_file} not found ({_e})")
        _mod = None
    except Exception as _e:
        print(f"[re_startup_mongo] ERROR loading {_devices_file}: {_e}")
        raise

if _mod is not None:
    globals().update({k: v for k, v in vars(_mod).items() if not k.startswith('_')})

# ── Standard bluesky plans ─────────────────────────────────────────────────────
from bluesky.plans import (
    count, scan, rel_scan, grid_scan, rel_grid_scan,
    adaptive_scan, tune_centroid,
    spiral, spiral_fermat,
)
from bluesky.plan_stubs import mv, mvr, sleep, rd
import bluesky.plan_stubs as _bps


def prime_detector(det):
    """
    Prime an area detector's file-writing plugins (HDF5, TIFF, JPEG).

    The plugin must receive one acquisition before it can write files — ophyd
    calls this "priming" or "warming up."  Run this plan once after the IOC
    restarts and before your first scan with an area detector.

    Usage (queue server)::

        prime_detector(Pil300K)
    """
    _file_plugins = ("hdf1", "tiff1", "jpeg1", "netcdf1", "magick1")
    _primed = []
    for _attr in _file_plugins:
        _plugin = getattr(det, _attr, None)
        if _plugin is not None and hasattr(_plugin, "warmup"):
            try:
                _plugin.warmup()
                _primed.append(_attr)
                print(f"[prime_detector] {det.name}.{_attr} warmed up")
            except Exception as _e:
                print(f"[prime_detector] Warning: {det.name}.{_attr} warmup failed: {_e}")
    if not _primed:
        # Fallback: stage→unstage triggers plugin configuration
        yield from _bps.stage(det)
        yield from _bps.unstage(det)
        print(f"[prime_detector] {det.name} staged/unstaged (primed via fallback)")
    else:
        yield from _bps.null()


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
