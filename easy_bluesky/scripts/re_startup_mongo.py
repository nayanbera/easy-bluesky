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
    list_scan, list_grid_scan,
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

    Accepts a single detector or a list of detectors.

    Usage (queue server)::

        prime_detector(Pil300K)
    """
    _file_plugins = ("hdf1", "tiff1", "jpeg1", "netcdf1", "magick1")
    _dets = det if isinstance(det, (list, tuple)) else [det]
    for _d in _dets:
        _primed = False
        for _attr in _file_plugins:
            _plugin = getattr(_d, _attr, None)
            if _plugin is not None and hasattr(_plugin, "warmup"):
                try:
                    _plugin.warmup()
                    _primed = True
                    print(f"[prime_detector] {_d.name}.{_attr} warmed up")
                except Exception as _e:
                    print(f"[prime_detector] Warning: {_d.name}.{_attr}: {_e}")
        if not _primed:
            # Call stage/unstage directly (not via plan stubs) so the
            # RunEngine never sees a list as msg.obj.
            try:
                _d.stage()
                _d.unstage()
                print(f"[prime_detector] {_d.name}: primed via stage/unstage")
            except Exception as _e:
                print(f"[prime_detector] Warning: stage/unstage for {_d.name}: {_e}")
    yield from _bps.null()


def get_device_pvnames():
    """Return {dev_name: {sig_name: pv_name}} for all top-level ophyd devices.

    Handles two cases:
    - Device subclass (EpicsMotor, custom Device): iterates read_attrs for
      all signals that have a pvname.
    - Plain EpicsSignal / EpicsSignalRO (not wrapped in a Device): treated as
      a single-signal device whose signal name equals the variable name.
      Without this, plain signal objects appear in the Devices tree but get no
      CA subscription and the Value column stays blank.
    """
    import ophyd as _oph
    out = {}
    for _n, _obj in list(globals().items()):
        if _n.startswith('_'):
            continue
        if isinstance(_obj, _oph.Device):
            # Full Device subclass — iterate read_attrs
            pvs = {}
            try:
                _read_attrs = list(_obj.read_attrs)
            except Exception:
                _read_attrs = list(getattr(_obj, 'component_names', []))
            for _attr in _read_attrs:
                if '.' in _attr:
                    continue  # skip nested sub-device signals
                try:
                    _sig = getattr(_obj, _attr, None)
                    if (_sig is not None
                            and hasattr(_sig, 'pvname')
                            and not isinstance(_sig, _oph.Device)):
                        pvs[_attr] = _sig.pvname
                except Exception:
                    pass
            # Fallback for devices that create signals manually in __init__
            # instead of via Component — those don't appear in read_attrs.
            if not pvs:
                for _attr, _sig in vars(_obj).items():
                    if _attr.startswith('_'):
                        continue
                    try:
                        if (isinstance(_sig, _oph.Signal)
                                and hasattr(_sig, 'pvname')
                                and _sig.pvname):
                            pvs[_attr] = _sig.pvname
                    except Exception:
                        pass
            out[_n] = pvs
        elif isinstance(_obj, _oph.Signal) and hasattr(_obj, 'pvname'):
            # Plain EpicsSignal / EpicsSignalRO — single PV, signal name = device name
            try:
                pv = _obj.pvname
                if pv:
                    out[_n] = {_n: pv}
            except Exception:
                pass
    return out


def set_sim_device(name: str, value: float):
    """Move a simulated device (SynAxis etc.) to *value*. Blocks until done."""
    import ophyd as _oph
    _obj = globals().get(name)
    if _obj is None or not isinstance(_obj, _oph.Device):
        raise ValueError(f"Device '{name}' not found in RE namespace")
    _st = _obj.set(float(value))
    _st.wait(timeout=10)


def read_devices_status():
    """Return {name: {connected, kind, reading:{sig:{value,units}}, error}} for all top-level devices."""
    import ophyd as _oph

    def _ser(v):
        try:
            import numpy as _np
            if isinstance(v, _np.ndarray):
                return v.tolist()
            if isinstance(v, _np.generic):
                return v.item()
        except ImportError:
            pass
        return v

    out = {}
    for _n, _obj in list(globals().items()):
        if _n.startswith('_') or not isinstance(_obj, _oph.Device):
            continue
        _d = {'connected': False, 'kind': str(_obj.kind.name), 'reading': {}, 'error': None}
        try:
            _d['connected'] = bool(_obj.connected)
            for _sn, _sd in _obj.read().items():
                _units = ''
                try:
                    _comp = _sn[len(_n) + 1:] if _sn.startswith(_n + '_') else _sn
                    _sig = getattr(_obj, _comp, None)
                    if _sig is not None:
                        _units = (getattr(_sig, 'metadata', None) or {}).get('units', '') or ''
                except Exception:
                    pass
                _d['reading'][_sn] = {'value': _ser(_sd.get('value')), 'units': str(_units)}
        except Exception as _e:
            _d['error'] = str(_e)
        out[_n] = _d
    return out


print("[re_startup_mongo] RE created, devices and plans loaded")

# ── BestEffortCallback / LiveTable (live scan table in console) ────────────────
try:
    from bluesky.callbacks.best_effort import BestEffortCallback as _BEC
    bec = _BEC()
    RE.subscribe(bec)
    print("[re_startup_mongo] BestEffortCallback subscribed")
except ImportError:
    # matplotlib not available — subscribe a LiveTable per run instead
    try:
        from bluesky.callbacks import LiveTable as _LiveTable
        from event_model import RunRouter as _RunRouter

        def _live_table_factory(name, doc):
            keys = list(doc.get("data_keys", {}).keys())
            return [_LiveTable(keys)], []

        RE.subscribe(_RunRouter([_live_table_factory]))
        print("[re_startup_mongo] LiveTable subscribed (matplotlib not available)")
    except Exception as _e2:
        print(f"[re_startup_mongo] WARNING: no live table callback: {_e2}")
except Exception as _e:
    print(f"[re_startup_mongo] WARNING: BestEffortCallback not subscribed: {_e}")

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
            # Priority 1: exp_dir passed in plan metadata (works for remote RE Manager
            # because the local app injects it into every plan's md kwargs).
            exp_dir = doc.get("exp_dir", "")
            if exp_dir:
                candidate = Path(exp_dir) / "runs"
                candidate.mkdir(parents=True, exist_ok=True)
                runs_dir = candidate
                print(f"[re_startup_mongo] run → {runs_dir} (from exp_dir md)")
            # Priority 2: active_experiment.json on this machine (local-mode only).
            elif _ACTIVE_EXP_FILE.exists():
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

# ── MongoDB (direct pymongo write — no suitcase dependency) ───────────────────
# Activated only when EASY_BLUESKY_MONGO_DB is set in the profile's Connection
# Settings.  Each profile uses its own database so runs from different profiles
# never share the same MongoDB namespace.
#
# Documents are stored in collections matching the bluesky document names:
#   start → run_start,  stop → run_stop,  descriptor → event_descriptor,
#   event → event,  resource → resource,  datum → datum
# This schema is readable by the MongoDataBrowserTab in EasyBluesky.
_MONGO_DB   = os.getenv("EASY_BLUESKY_MONGO_DB",   "")
_MONGO_HOST = os.getenv("EASY_BLUESKY_MONGO_HOST",  "localhost")
_MONGO_PORT = int(os.getenv("EASY_BLUESKY_MONGO_PORT", "27017"))

if _MONGO_DB:
    try:
        import pymongo as _pymongo

        _mongo_client = _pymongo.MongoClient(_MONGO_HOST, _MONGO_PORT,
                                             serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")

        _MONGO_COLL = {
            'start':      'run_start',
            'stop':       'run_stop',
            'descriptor': 'event_descriptor',
            'event':      'event',
            'resource':   'resource',
            'datum':      'datum',
        }

        def _make_mongo_write(_client, _db_name):
            # Capture the Database object inside a closure so it is NOT a bare
            # module-level variable.  pymongo >= 4 raises NotImplementedError for
            # bool(Database), and bluesky-queueserver calls `if obj:` on every
            # name in the startup-script namespace when building its device list.
            _db = _client[_db_name]
            def _write(name, doc):
                try:
                    doc_copy = dict(doc)
                    if 'uid' in doc_copy:
                        doc_copy['_id'] = doc_copy['uid']
                    _db[_MONGO_COLL.get(name, name)].insert_one(doc_copy)
                except Exception as _we:
                    if 'duplicate key' not in str(_we).lower():
                        print(f"[re_startup_mongo] MongoDB write error ({name}): {_we}")
            return _write

        _mongo_write = _make_mongo_write(_mongo_client, _MONGO_DB)
        del _mongo_client  # remove MongoClient from namespace for the same reason
        RE.subscribe(_mongo_write)
        print(
            f"[re_startup_mongo] MongoDB → {_MONGO_HOST}:{_MONGO_PORT}"
            f"  database: {_MONGO_DB}"
        )
    except ImportError:
        print(
            "[re_startup_mongo] WARNING: MongoDB not subscribed — pymongo not installed.\n"
            "  Install with:  pip install pymongo"
        )
    except Exception as _e:
        print(f"[re_startup_mongo] WARNING: MongoDB not subscribed: {_e}")
else:
    print("[re_startup_mongo] MongoDB disabled (no EASY_BLUESKY_MONGO_DB set)")

# ── ZMQ PUB for Live Viewer ────────────────────────────────────────────────────
_ZMQ_PUB_PORT = int(os.getenv("BLUESKY_ZMQ_PUB_PORT", "60630"))
try:
    import zmq as _zmq
    import json as _json
    from event_model import sanitize_doc as _sanitize

    _zmq_ctx  = _zmq.Context()
    _zmq_sock = _zmq_ctx.socket(_zmq.PUB)
    _zmq_sock.setsockopt(_zmq.LINGER, 0)
    _zmq_sock.setsockopt(_zmq.SNDHWM, 100)
    _zmq_sock.bind(f"tcp://*:{_ZMQ_PUB_PORT}")

    import numpy as _np

    class _NumpyEncoder(_json.JSONEncoder):
        """JSON encoder that converts numpy scalars/arrays to native Python types."""
        def default(self, obj):
            if isinstance(obj, _np.ndarray):
                return obj.tolist()
            if isinstance(obj, _np.generic):
                return obj.item()
            if isinstance(obj, bytes):
                return obj.decode("utf-8", errors="replace")
            return super().default(obj)

    def _zmq_publish(name, doc):
        try:
            _zmq_sock.send_string(
                _json.dumps([name, dict(_sanitize(doc))], cls=_NumpyEncoder)
            )
        except Exception as _ze:
            print(f"[re_startup_mongo] ZMQ publish error ({name}): {_ze}")

    RE.subscribe(_zmq_publish)
    print(f"[re_startup_mongo] ZMQ PUB → tcp://*:{_ZMQ_PUB_PORT}")
except Exception as e:
    print(f"[re_startup_mongo] WARNING: ZMQ PUB not started: {e}")
