"""
re_startup_sim.py — AUTO-GENERATED simulated startup script.
Generated from: re_startup_mongo.py

Edit freely.  Re-generate from File → Generate Sim Script to pick up
new real-hardware devices added to the original startup script.
"""

# ── Run Engine ────────────────────────────────────────────────────────────
from bluesky import RunEngine
RE = RunEngine({})

# ── SimAreaDetector ────────────────────────────────────────────────────────
import numpy as _np
import time as _time
from ophyd import Device, Component as Cpt, Signal, DeviceStatus


class _SimCam(Device):
    acquire_time   = Cpt(Signal, value=1.0,        kind='config')
    acquire_period = Cpt(Signal, value=1.1,        kind='config')
    num_images     = Cpt(Signal, value=1,          kind='config')
    image_mode     = Cpt(Signal, value=0,          kind='config')
    gain           = Cpt(Signal, value=1.0,        kind='config')
    trigger_mode   = Cpt(Signal, value=0,          kind='config')


class _SimHDF5Plugin(Device):
    file_path      = Cpt(Signal, value='/tmp/',    kind='config')
    file_name      = Cpt(Signal, value='sim',      kind='config')
    file_number    = Cpt(Signal, value=1,          kind='config')
    num_capture    = Cpt(Signal, value=1,          kind='config')
    enable         = Cpt(Signal, value=1,          kind='config')
    auto_save      = Cpt(Signal, value=1,          kind='config')
    auto_increment = Cpt(Signal, value=1,          kind='config')
    file_template  = Cpt(Signal, value='%s%s_%d.h5', kind='config')


class _SimImagePlugin(Device):
    array_data  = Cpt(Signal, value=0,   kind='normal')
    array_size0 = Cpt(Signal, value=512, kind='config')
    array_size1 = Cpt(Signal, value=512, kind='config')


class SimAreaDetector(Device):
    """
    Simulated area detector mirroring the ophyd SingleTrigger+AreaDetector
    interface.  cam / hdf1 / image sub-devices are all settable via bps.mv /
    bps.abs_set.  trigger() returns an immediately-completed Status.
    read() returns total_counts and mean_intensity as scalar values.
    """
    cam   = Cpt(_SimCam,          '', kind='config')
    hdf1  = Cpt(_SimHDF5Plugin,   '', kind='config')
    image = Cpt(_SimImagePlugin,  '', kind='normal')
    total_counts   = Cpt(Signal, value=0,   kind='hinted')
    mean_intensity = Cpt(Signal, value=0.0, kind='normal')

    def __init__(self, *args, shape=(512, 512), background=100.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._shape      = shape
        self._background = background

    def trigger(self):
        img = _np.random.poisson(self._background, self._shape).astype(_np.float32)
        self.total_counts.put(int(img.sum()))
        self.mean_intensity.put(float(img.mean()))
        self.image.array_data.put(int(img.sum()))
        st = DeviceStatus(self)
        st._finished()
        return st

    def stage(self):
        return super().stage()

    def unstage(self):
        return super().unstage()


# ── Simulated devices (auto-mapped from real script) ───────────────────────
from ophyd.sim import SynAxis, SynGauss, SynNoise

# No devices detected — adding generic defaults
motor  = SynAxis(name='motor')
motor1 = SynAxis(name='motor1')
motor2 = SynAxis(name='motor2')
det    = SynGauss('det', motor, 'motor', center=0, Imax=1000, sigma=0.5)
sim_ad = SimAreaDetector(name='sim_ad')

# ── Standard bluesky plans ─────────────────────────────────────────────────
from bluesky.plans import (
    count, scan, rel_scan, grid_scan, rel_grid_scan,
    adaptive_scan, spiral, spiral_fermat,
)
from bluesky.plan_stubs import mv, mvr, sleep, rd

# ── Data routing + ZMQ (copied from real script) ──────────────────────────
  - Subscribes suitcase.jsonl serializer; routes each run's JSONL data to
    <active_experiment>/runs/ (reads data/active_experiment.json per run).
    Falls back to data/runs/ when no experiment is active.
  - Publishes documents on ZMQ PUB port 60630 for the Live Viewer

Environment variables:
    BLUESKY_DATA_DIR      (default: <project_root>/data/runs)
    BLUESKY_ZMQ_PUB_PORT  (default: 60630)
"""

import os
from pathlib import Path

# ── Run Engine ─────────────────────────────────────────────────────────────────
from bluesky import RunEngine
RE = RunEngine({})

# ── Simulated devices ──────────────────────────────────────────────────────────
from ophyd.sim import motor1, motor2, det1, det2, det, motor

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
