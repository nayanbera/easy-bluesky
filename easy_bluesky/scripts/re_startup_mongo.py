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
try:
    from bluesky.utils import PersistentDict as _PD
    _re_md_dir = Path.home() / ".easy_bluesky" / "re_md"
    _re_md_dir.mkdir(parents=True, exist_ok=True)
    RE = RunEngine(_PD(str(_re_md_dir)))
    print(f"[re_startup_mongo] RE scan_id persists in {_re_md_dir}")
except Exception as _e_pd:
    print(f"[re_startup_mongo] PersistentDict unavailable ({_e_pd}), scan_id will reset on restart")
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
    # Relative filename — look in the same directory as this startup script.
    # Use spec_from_file_location (not importlib.import_module) so the search
    # is by path rather than by module name; this avoids case-sensitivity issues
    # on Linux (e.g. env var "devices_aswaxs.py" finding "devices_ASWAXS.py").
    import importlib.util as _ilu2
    _scripts_dir = Path(__file__).parent
    _target = _scripts_dir / _devices_file
    if not _target.exists():
        # Case-insensitive fallback: find any .py file with the same stem
        _stem_lower = Path(_devices_file).stem.lower()
        _candidates = [p for p in _scripts_dir.glob("*.py")
                       if p.stem.lower() == _stem_lower]
        if _candidates:
            _target = _candidates[0]
    try:
        if not _target.exists():
            raise ImportError(
                f"No module named '{Path(_devices_file).stem}'"
            )
        _spec2 = _ilu2.spec_from_file_location("_easy_bluesky_devices", str(_target))
        _mod   = _ilu2.module_from_spec(_spec2)
        _spec2.loader.exec_module(_mod)
        print(f"[re_startup_mongo] {_target.name} loaded")
    except ImportError as _e:
        print(f"[re_startup_mongo] WARNING: {_devices_file} not found ({_e})")
        _mod = None
    except Exception as _e:
        print(f"[re_startup_mongo] ERROR loading {_devices_file}: {_e}")
        raise

if _mod is not None:
    globals().update({k: v for k, v in vars(_mod).items() if not k.startswith('_')})

# ── Profile plans (from EASY_BLUESKY_PLANS_DIR or legacy user_plans.py) ──────
_plans_dir = os.getenv("EASY_BLUESKY_PLANS_DIR", "").strip()
if _plans_dir and os.path.isdir(_plans_dir):
    # New: load every .py file in the plans directory
    import importlib.util as _ilu_pd
    for _pf in sorted(Path(_plans_dir).glob("*.py")):
        try:
            _spec_pd = _ilu_pd.spec_from_file_location(_pf.stem, str(_pf))
            _mod_pd  = _ilu_pd.module_from_spec(_spec_pd)
            _spec_pd.loader.exec_module(_mod_pd)
            globals().update({k: v for k, v in vars(_mod_pd).items()
                              if not k.startswith('_')})
            # Inject device objects so _resolve_device() in plan modules can
            # look up device-name strings via globals().get(name).
            import ophyd as _oph_pd
            for _dk, _dv in list(globals().items()):
                if not _dk.startswith('_') and isinstance(_dv, (_oph_pd.Device,
                                                                  _oph_pd.Signal)):
                    vars(_mod_pd)[_dk] = _dv
            # Inject RE so plan helpers can read RE.md["scan_id"].
            vars(_mod_pd)['RE'] = RE
            print(f"[re_startup_mongo] {_pf.name} loaded")
        except Exception as _e_pd:
            print(f"[re_startup_mongo] WARNING: {_pf.name} failed to load: {_e_pd}")
else:
    # Backward compat: load user_plans.py from the scripts directory if present
    _user_plans_file = str(Path(__file__).parent / "user_plans.py")
    if os.path.exists(_user_plans_file):
        try:
            import importlib.util as _ilu_up
            _spec_up = _ilu_up.spec_from_file_location("_easy_bluesky_user_plans",
                                                        _user_plans_file)
            _mod_up  = _ilu_up.module_from_spec(_spec_up)
            _spec_up.loader.exec_module(_mod_up)
            globals().update({k: v for k, v in vars(_mod_up).items()
                              if not k.startswith('_')})
            vars(_mod_up)['RE'] = RE
            print("[re_startup_mongo] user_plans.py loaded (legacy)")
        except Exception as _e_up:
            print(f"[re_startup_mongo] WARNING: user_plans.py failed to load: {_e_up}")

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


def reset_scan_id():
    """Reset the RunEngine scan counter to 0 so the next scan gets scan_id=1.

    Call this via function_execute when a new experiment is opened in EasyBluesky.
    Safe to call at any time (does not require an active run or environment state).
    """
    RE.md['scan_id'] = 0


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


class _CallableCB:
    """Mixin that makes a callback class callable as cb(name, doc).
    Required by event-model >= 1.14.0 when a RunRouter factory returns
    callback instances (the RunRouter calls them as callables, not via
    named methods).  Uses getattr with a default so callbacks that only
    implement a subset of document types (e.g. no 'start' method) are
    silently ignored for the missing types.
    """
    def __call__(self, name, doc):
        handler = getattr(self, name, None)
        if handler is not None:
            return handler(doc)


# ── BestEffortCallback / LiveTable (live scan table in console) ────────────────
# Subscribe BEC for live plots (if matplotlib is available) but disable its
# built-in table — we use our own RunRouter-based LiveTable that always shows
# all data_keys regardless of whether devices expose .hints.
try:
    from bluesky.callbacks.best_effort import BestEffortCallback as _BEC
    bec = _BEC()
    try:
        bec.disable_table()
    except AttributeError:
        bec.table_enabled = False
    RE.subscribe(bec)
    print("[re_startup_mongo] BestEffortCallback subscribed (table disabled; using custom LiveTable)")
except ImportError:
    bec = None
    print("[re_startup_mongo] BestEffortCallback unavailable (no matplotlib)")
except Exception as _e:
    bec = None
    print(f"[re_startup_mongo] WARNING: BestEffortCallback not subscribed: {_e}")

# Custom scan table: prints motor + detector values per event using print()
# directly (bluesky's LiveTable does not reliably reach stdout in this env).
try:
    import time as _time_mod
    from event_model import RunRouter as _RRtbl

    class _ConsoleScanTable(_CallableCB):
        """Prints a plain-text scan table to stdout using print()."""

        _W = 14   # column width

        def __init__(self):
            self._fields = []

        def start(self, doc):
            pass

        def descriptor(self, doc):
            keys = [k for k in doc.get("data_keys", {})
                    if not k.endswith("_setpoint")]
            self._fields = keys
            if not keys:
                return
            w = self._W
            cols = ["seq_num", "time"] + keys
            header = " | ".join(f"{c:>{w}}" for c in cols)
            sep    = "-+-".join("-" * w for _ in cols)
            print(header)
            print(sep)

        def _print_row(self, seq, t, data):
            w    = self._W
            t_str = _time_mod.strftime("%H:%M:%S", _time_mod.localtime(t))
            vals = [f"{seq:>{w}}", f"{t_str:>{w}}"]
            for f in self._fields:
                v = data.get(f, "")
                try:
                    vals.append(f"{float(v):>{w}.5g}")
                except (TypeError, ValueError):
                    vals.append(f"{str(v):>{w}}")
            print(" | ".join(vals))

        def event(self, doc):
            if not self._fields:
                return
            self._print_row(doc.get("seq_num", ""),
                            doc.get("time", 0),
                            doc.get("data", {}))

        def event_page(self, doc):
            if not self._fields:
                return
            data    = doc.get("data", {})
            seq_arr = doc.get("seq_num", [])
            t_arr   = doc.get("time", [])
            for i, seq in enumerate(seq_arr):
                t    = t_arr[i] if i < len(t_arr) else 0
                row  = {f: data[f][i] for f in self._fields
                        if f in data and i < len(data[f])}
                self._print_row(seq, t, row)

        def stop(self, doc):
            if self._fields:
                cols = 2 + len(self._fields)
                print("-+-".join("-" * self._W for _ in range(cols)))

    def _scan_table_factory(name, doc):
        return [_ConsoleScanTable()], []

    RE.subscribe(_RRtbl([_scan_table_factory]))
    print("[re_startup_mongo] ConsoleScanTable subscribed")
except Exception as _e2:
    print(f"[re_startup_mongo] WARNING: ConsoleScanTable not subscribed: {_e2}")

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


class _JSONLRunWriter(_CallableCB):
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


class _RunPeakStats(_CallableCB):
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
        y_fields = detectors,
        uid      = doc.get("uid", ""),
        update_fn= _mongo_peak_stats_update,
    )
    return [cb], []


try:
    RE.subscribe(_RR([_peak_stats_factory]))
    print("[re_startup_mongo] PeakStats subscribed")
except Exception as _e:
    print(f"[re_startup_mongo] WARNING: PeakStats not subscribed: {_e}")

# ── scans_log.json — one summary entry per run ────────────────────────────────
# Written to <remote_exp_dir>/scans_log.json (preferred) or <exp_dir>/scans_log.json.
# The app fetches this via SFTP to populate the Plan Log panel instead of reading
# local JSONL run files, so the RE machine is the single source of truth for history.

import json as _j_sl
from pathlib import Path as _P_sl


class _ScanLogCallback(_CallableCB):
    """Append one summary entry to scans_log.json when a run completes."""

    def __init__(self, exp_path: str, start_doc: dict):
        self._exp_path   = exp_path
        self._uid        = start_doc.get("uid", "unknown")
        self._scan_id    = start_doc.get("scan_id", 0)
        self._plan_name  = start_doc.get("plan_name", "") or ""
        self._sample     = start_doc.get("sample_name", "")
        self._start_time = start_doc.get("time", 0.0)
        self._motors     = list(start_doc.get("motors",    []) or [])
        self._detectors  = list(start_doc.get("detectors", []) or [])
        self._plan_args  = list(start_doc.get("plan_args",   []) or [])
        self._plan_kwargs = dict(start_doc.get("plan_kwargs", {}) or {})
        self._num_events = 0

    def event(self, doc):
        self._num_events += 1

    def event_page(self, doc):
        self._num_events += len(doc.get("seq_num", []))

    def stop(self, doc):
        stop_time   = doc.get("time", 0.0)
        exit_status = doc.get("exit_status", "unknown")
        dur = round(stop_time - self._start_time, 2) if (stop_time and self._start_time) else None

        entry = {
            "uid":         self._uid,
            "scan_id":     self._scan_id,
            "plan_name":   self._plan_name,
            "sample_name": self._sample,
            "start_time":  self._start_time,
            "stop_time":   stop_time,
            "exit_status": exit_status,
            "num_events":  self._num_events,
            "duration_s":  dur,
            "motors":      self._motors,
            "detectors":   self._detectors,
            "plan_args":   self._plan_args,
            "plan_kwargs": self._plan_kwargs,
        }

        log_path = _P_sl(self._exp_path) / "scans_log.json"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if log_path.exists():
                try:
                    existing = _j_sl.loads(log_path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append(entry)
            tmp = log_path.with_suffix(".json.tmp")
            tmp.write_text(_j_sl.dumps(existing, indent=2), encoding="utf-8")
            tmp.replace(log_path)
        except Exception as _e_sl:
            print(f"[re_startup_mongo] scans_log write error: {_e_sl}")


def _scan_log_factory(name, doc):
    exp_path = doc.get("remote_exp_dir") or doc.get("exp_dir") or ""
    if not exp_path:
        return [], []
    return [_ScanLogCallback(exp_path, doc)], []


try:
    RE.subscribe(_RR([_scan_log_factory]))
    print("[re_startup_mongo] scans_log writer subscribed")
except Exception as _e:
    print(f"[re_startup_mongo] WARNING: scans_log writer not subscribed: {_e}")
