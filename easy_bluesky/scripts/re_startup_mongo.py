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
        elif isinstance(_obj, _oph.Signal):
            # Plain Signal — either EPICS (EpicsSignal/RO with a real PV) or
            # simulated (SynSignal/SynSignalRO/SynNoise/SynPeriodicSignal with
            # no PV).  Real signals get their pvname; sim signals get an empty
            # dict (same as SynAxis via the Device branch) so they are NOT
            # mistaken for an EPICS device and don't show "○ Connecting…".
            try:
                pv = getattr(_obj, 'pvname', '') or ''
                out[_n] = {_n: pv} if pv else {}
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

    def _trigger_if_func(obj):
        """Call trigger() for func-based sim signals (SynSignal, SynNoise, SynGauss…).

        In many ophyd versions SynSignal.get() returns a stale cached _readback;
        only trigger() re-evaluates _func().  We detect these objects by the
        presence of a callable _func attribute so real EPICS devices are never
        accidentally triggered.
        """
        if not callable(getattr(obj, '_func', None)):
            return
        try:
            _st = obj.trigger()
            if hasattr(_st, 'wait'):
                _st.wait(timeout=2)
        except Exception:
            pass

    out = {}
    for _n, _obj in list(globals().items()):
        if _n.startswith('_'):
            continue
        if isinstance(_obj, _oph.Device):
            _d = {'connected': False, 'kind': str(_obj.kind.name), 'reading': {}, 'error': None}
            try:
                _d['connected'] = bool(_obj.connected)
                _trigger_if_func(_obj)
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
        elif isinstance(_obj, _oph.Signal):
            # Plain Signal subclasses (EpicsSignal standalone, or SynSignal if
            # it is NOT a Device subclass in this ophyd version).
            _d = {'connected': True, 'kind': str(_obj.kind.name), 'reading': {}, 'error': None}
            try:
                _trigger_if_func(_obj)
                for _sn, _sd in _obj.read().items():
                    _d['reading'][_sn] = {'value': _ser(_sd.get('value')), 'units': ''}
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
    bec = None
    print("[re_startup_mongo] BestEffortCallback unavailable (no matplotlib)")
except Exception as _e:
    bec = None
    print(f"[re_startup_mongo] WARNING: BestEffortCallback not subscribed: {_e}")

# Fallback LiveTable: fires only when the descriptor's hints are empty
# (e.g. older ophyd where SynGauss/SynAxis don't expose .hints).
# When BEC gets proper hints it already prints a full table; this avoids duplication.
# The RunRouter factory receives the START document; we must defer table creation
# until the DESCRIPTOR document arrives (where data_keys and hints actually live).
try:
    from bluesky.callbacks import LiveTable as _LiveTable
    from event_model import RunRouter as _RRtbl

    class _DeferredLiveTable:
        """Creates a LiveTable on the descriptor if BEC has no hinted columns."""

        def __init__(self):
            self._table = None

        def __call__(self, name, doc):
            return getattr(self, name)(doc)

        def start(self, doc):
            pass

        def descriptor(self, doc):
            hinted = []
            for obj_h in doc.get("hints", {}).values():
                if isinstance(obj_h, dict):
                    hinted.extend(obj_h.get("fields", []))
            if hinted:
                return  # BEC will show proper columns
            keys = [k for k in doc.get("data_keys", {})
                    if not k.endswith("_setpoint")]
            if keys:
                self._table = _LiveTable(keys)
                self._table.descriptor(doc)

        def event(self, doc):
            if self._table is not None:
                self._table.event(doc)

        def event_page(self, doc):
            if self._table is not None:
                try:
                    self._table.event_page(doc)
                except AttributeError:
                    pass

        def stop(self, doc):
            if self._table is not None:
                self._table.stop(doc)

    def _fallback_table_factory(name, doc):
        return [_DeferredLiveTable()], []

    RE.subscribe(_RRtbl([_fallback_table_factory]))
    print("[re_startup_mongo] fallback LiveTable subscribed")
except Exception as _e2:
    print(f"[re_startup_mongo] WARNING: fallback LiveTable not subscribed: {_e2}")

# ── JSONL per-run writer ─────────────────────────────────────────────────────
# Writes one <uid>.jsonl file per run into <exp_dir>/runs/ (from the start doc).
# Falls back to ~/.easy_bluesky/data/runs/ when exp_dir is missing or inaccessible.
import json as _j
from pathlib import Path as _P
from event_model import RunRouter as _RR

_FALLBACK_RUNS_DIR = _P.home() / ".easy_bluesky" / "data" / "runs"


class _JEnc(_j.JSONEncoder):
    """JSON encoder that handles numpy arrays and scalars."""
    def default(self, obj):
        try:
            import numpy as _np_j
            if isinstance(obj, _np_j.ndarray):
                return obj.tolist()
            if isinstance(obj, _np_j.generic):
                return obj.item()
        except ImportError:
            pass
        return super().default(obj)


class _JSONLRunWriter:
    """Write one JSONL file per run — one [doc_type, doc_body] JSON line per document."""

    def __init__(self, runs_dir, uid):
        _P(runs_dir).mkdir(parents=True, exist_ok=True)
        self._path = _P(runs_dir) / f"{uid}.jsonl"
        self._fh   = open(self._path, "w")

    def _write(self, name, doc):
        self._fh.write(_j.dumps([name, dict(doc)], cls=_JEnc) + "\n")
        self._fh.flush()

    def start(self, doc):       self._write("start",      doc)
    def descriptor(self, doc):  self._write("descriptor", doc)
    def event(self, doc):       self._write("event",      doc)
    def event_page(self, doc):  self._write("event_page", doc)

    def stop(self, doc):
        self._write("stop", doc)
        try:
            self._fh.close()
        except Exception:
            pass


def _jsonl_run_factory(name, doc):
    uid     = doc.get("uid", "unknown")
    exp_dir = doc.get("exp_dir", "")
    if exp_dir:
        try:
            runs_dir = _P(exp_dir) / "runs"
            writer   = _JSONLRunWriter(runs_dir, uid)
        except Exception as _fe:
            print(f"[re_startup_mongo] JSONL fallback ({_fe}): writing to {_FALLBACK_RUNS_DIR}")
            writer = _JSONLRunWriter(_FALLBACK_RUNS_DIR, uid)
    else:
        writer = _JSONLRunWriter(_FALLBACK_RUNS_DIR, uid)
    return ([writer], [])


try:
    RE.subscribe(_RR([_jsonl_run_factory]))
    print("[re_startup_mongo] JSONL run writer subscribed")
except Exception as _e:
    print(f"[re_startup_mongo] WARNING: JSONL run writer not subscribed: {_e}")

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
_mongo_peak_stats_update = None   # set below when MongoDB is available

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

        def _make_peak_stats_updater(_client, _db_name):
            _db = _client[_db_name]
            def _do_update(uid, stats):
                try:
                    _db["run_start"].update_one(
                        {"uid": uid}, {"$set": {"peak_stats": stats}}
                    )
                except Exception as _ue:
                    print(f"[PeakStats] MongoDB update error: {_ue}")
            return _do_update

        _mongo_write = _make_mongo_write(_mongo_client, _MONGO_DB)
        _mongo_peak_stats_update = _make_peak_stats_updater(_mongo_client, _MONGO_DB)
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
    import numpy as _np

    # sanitize_doc available in event_model ≥1.19; fall back to identity for older envs
    try:
        from event_model import sanitize_doc as _sanitize
    except ImportError:
        def _sanitize(d):
            return d

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

    _zmq_ctx  = _zmq.Context()
    _zmq_sock = _zmq_ctx.socket(_zmq.PUB)
    _zmq_sock.setsockopt(_zmq.LINGER, 0)
    _zmq_sock.setsockopt(_zmq.SNDHWM, 100)
    _zmq_sock.bind(f"tcp://*:{_ZMQ_PUB_PORT}")

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

# ── PeakStats per-run ─────────────────────────────────────────────────────────
# Computes center, FWHM, COM, max/min for each scan's primary signals.
# Results are printed to the RE console and (if MongoDB is active) stored as
# peak_stats on the run_start document for use as fitting initial guesses.

import numpy as _psn


def _compute_peak_stats(x, y):
    """Compute scan statistics for a peak or step profile.

    Returns a dict with: max_pos, max_val, min_pos, min_val, com, cen, fwhm.
    cen / fwhm use the half-max crossing (works for both peaks and step edges).
    """
    i_max = int(_psn.argmax(y))
    i_min = int(_psn.argmin(y))

    # Center of mass (shift y to be non-negative first)
    y_pos = y - y.min()
    denom = float(_psn.sum(y_pos))
    com = float(_psn.sum(x * y_pos) / denom) if denom > 0 else float(x[i_max])

    # Half-max crossings (interpolated)
    half = (float(y.max()) + float(y.min())) / 2.0
    above = y >= half
    edges = _psn.where(_psn.diff(above.astype(int)))[0]

    def _interp(i):
        x0, x1 = float(x[i]), float(x[i + 1])
        y0, y1 = float(y[i]), float(y[i + 1])
        if y1 == y0:
            return (x0 + x1) / 2.0
        return x0 + (half - y0) / (y1 - y0) * (x1 - x0)

    if len(edges) >= 2:
        xl    = _interp(edges[0])
        xr    = _interp(edges[-1])
        cen   = (xl + xr) / 2.0
        fwhm  = abs(xr - xl)
    else:
        cen  = None
        fwhm = None

    return {
        "max_pos": float(x[i_max]),
        "max_val": float(y[i_max]),
        "min_pos": float(x[i_min]),
        "min_val": float(y[i_min]),
        "com":     com,
        "cen":     cen,
        "fwhm":    fwhm,
    }


class _RunPeakStats:
    """Per-run callback that computes peak/step statistics on the stop document."""

    def __init__(self, x_field, y_fields, uid, update_fn=None):
        self._x_field  = x_field
        self._y_fields = list(y_fields)
        self._uid      = uid
        self._update   = update_fn   # callable(uid, stats_dict) or None
        self._x        = []
        self._y        = {f: [] for f in y_fields}

    def event(self, doc):
        d  = doc.get("data", {})
        xv = d.get(self._x_field)
        if xv is None:
            return
        try:
            self._x.append(float(xv))
        except (TypeError, ValueError):
            return
        for f in self._y_fields:
            yv = d.get(f)
            if yv is not None:
                try:
                    self._y[f].append(float(yv))
                except (TypeError, ValueError):
                    pass

    def event_page(self, doc):
        data = doc.get("data", {})
        xs   = data.get(self._x_field, [])
        for i, xv in enumerate(xs):
            try:
                self._x.append(float(xv))
            except (TypeError, ValueError):
                pass
            for f in self._y_fields:
                col = data.get(f, [])
                if i < len(col):
                    try:
                        self._y[f].append(float(col[i]))
                    except (TypeError, ValueError):
                        pass

    def stop(self, doc):
        x = _psn.asarray(self._x)
        all_stats = {}
        for f in self._y_fields:
            y = _psn.asarray(self._y.get(f, []))
            n = min(len(x), len(y))
            if n < 3:
                continue
            try:
                st = _compute_peak_stats(x[:n], y[:n])
                all_stats[f] = st
                parts = [f"[PeakStats/{f}]"]
                if st["cen"] is not None:
                    parts.append(f"cen={st['cen']:.5g}")
                if st["fwhm"] is not None:
                    parts.append(f"FWHM={st['fwhm']:.4g}")
                parts.append(f"COM={st['com']:.5g}")
                parts.append(f"max={st['max_val']:.4g}@{st['max_pos']:.5g}")
                print("  " + "  ".join(parts))
            except Exception as _se:
                print(f"[PeakStats] Error ({f}): {_se}")

        if all_stats and self._update is not None:
            self._update(self._uid, all_stats)


def _peak_stats_factory(name, doc):
    motors    = [str(m) for m in (doc.get("motors",    []) or [])]
    detectors = [str(d) for d in (doc.get("detectors", []) or [])]
    if not motors or not detectors:
        return [], []
    cb = _RunPeakStats(
        x_field  = motors[0],
        y_fields = detectors[:4],
        uid      = doc.get("uid", ""),
        update_fn= _mongo_peak_stats_update,
    )
    return [cb], []


try:
    RE.subscribe(_RR([_peak_stats_factory]))
    print("[re_startup_mongo] PeakStats subscribed")
except Exception as _e:
    print(f"[re_startup_mongo] WARNING: PeakStats not subscribed: {_e}")
