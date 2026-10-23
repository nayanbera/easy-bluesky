"""hdf5_export.py — complete run export to HDF5 (MongoDB or .jsonl source).

File layout (``scan_NNNN`` groups, one per run), a superset of the old export:

    scan_0007/
        <field>                    flat float datasets of the *primary* stream (legacy layout —
                                   the HDF5 Viewer reads these; ``time`` is always present)
        attrs: plan_name, scan_num, uid, exit_status, timestamp, duration_s, sample_name, …
               start_doc  (JSON)   complete start document (metadata, ESAF, md extras, …)
               stop_doc   (JSON)   complete stop document
               peak_stats (JSON)   peak statistics stored with the run (if any)
               streams    (JSON)   list of stream names
        streams/<stream>/          every stream: primary, baseline, …
            attrs: data_keys (JSON)  per-field source PV, units, dtype, shape, precision
            time                   event timestamps
            <field>                numeric → float datasets, text → utf-8 strings

``read_streams(grp)`` is the inverse (used by the HDF5 Viewer).
"""
import json

import numpy as np

try:
    import h5py
except ImportError:          # pragma: no cover
    h5py = None

_STR = None


def _json(obj) -> str:
    def _default(o):
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, np.generic):
            return o.item()
        return str(o)
    return json.dumps(obj, default=_default)


def _safe_name(field: str) -> str:
    return str(field).replace("/", "_")


# ── Sources ───────────────────────────────────────────────────────────────────

def _to_array(vals):
    try:
        return np.array(vals, dtype=float)
    except (TypeError, ValueError):
        return np.array(vals, dtype=object)


def run_from_mongo(db, uid: str) -> dict:
    """{"start", "stop", "streams"} for one run from an open pymongo Database."""
    from .mongo_browser import _fetch_streams
    start = db["run_start"].find_one({"uid": uid}) or {}
    stop  = db["run_stop"].find_one({"run_start": uid}) or {}
    return {"start": start, "stop": stop, "streams": _fetch_streams(db, uid)}


def run_from_jsonl(path) -> dict:
    """Same structure as run_from_mongo, parsed from a ``[name, doc]``-per-line .jsonl run file."""
    start, stop = {}, {}
    descs: dict = {}                      # descriptor uid -> {"name", "data_keys"}
    ev: dict = {}                         # descriptor uid -> {"time": [], field: []}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    name, doc = json.loads(line)
                except Exception:
                    continue
                if name == "start":
                    start = doc
                elif name == "stop":
                    stop = doc
                elif name == "descriptor":
                    descs[doc.get("uid", "")] = {"name": doc.get("name", "primary"),
                                                 "data_keys": doc.get("data_keys", {})}
                elif name == "event":
                    d = ev.setdefault(doc.get("descriptor", ""), {"time": []})
                    d["time"].append(doc.get("time", 0))
                    for k, v in (doc.get("data") or {}).items():
                        d.setdefault(k, []).append(v)
                elif name == "event_page":
                    d = ev.setdefault(doc.get("descriptor", ""), {"time": []})
                    times = doc.get("time", [])
                    d["time"].extend(times)
                    n = len(times)
                    for k, col in (doc.get("data") or {}).items():
                        d.setdefault(k, []).extend(col if isinstance(col, list) else [col] * n)
    except Exception:
        pass

    streams = {}
    for duid, d in ev.items():
        info = descs.get(duid, {"name": "primary", "data_keys": {}})
        keys = info["data_keys"] or {k: {} for k in d if k != "time"}
        s = {"time": np.array(d["time"], dtype=float), "data_keys": keys}
        for k in keys:
            if k in d:
                s[k] = _to_array(d[k])
        streams[info["name"]] = s
    return {"start": start, "stop": stop, "streams": streams}


# ── Writer ────────────────────────────────────────────────────────────────────

def _write_array(grp, name: str, arr):
    name = _safe_name(name)
    if name in grp:
        return
    arr = np.asarray(arr)
    if arr.dtype.kind in "fiub":
        grp.create_dataset(name, data=arr.astype(float), compression="gzip")
    else:
        strs = ["" if v is None else str(v) for v in arr.tolist()]
        grp.create_dataset(name, data=np.array(strs, dtype=object),
                           dtype=h5py.string_dtype(encoding="utf-8"), compression="gzip")


def write_run(grp, run: dict) -> int:
    """Write documents + all streams of ``run`` into the (already created) scan group.

    Returns the number of primary events.
    """
    start  = run.get("start") or {}
    stop   = run.get("stop") or {}
    streams = run.get("streams") or {}

    if start:
        grp.attrs["start_doc"] = _json(start)
        if start.get("peak_stats"):
            grp.attrs["peak_stats"] = _json(start["peak_stats"])
    if stop:
        grp.attrs["stop_doc"] = _json(stop)
    grp.attrs["streams"] = _json(sorted(streams))

    n_events = 0
    primary = streams.get("primary", {})
    for field, arr in primary.items():           # legacy flat layout
        if field == "data_keys":
            continue
        a = np.asarray(arr)
        if a.dtype.kind in "fiub" and _safe_name(field) not in grp:
            grp.create_dataset(_safe_name(field), data=a.astype(float), compression="gzip")
            if field == "time":
                n_events = len(a)
    if n_events:
        grp.attrs["n_events"] = n_events

    sgrp = grp.create_group("streams")
    for sname, sdata in streams.items():
        g = sgrp.create_group(_safe_name(sname))
        g.attrs["data_keys"] = _json(sdata.get("data_keys", {}))
        for field, arr in sdata.items():
            if field == "data_keys":
                continue
            try:
                _write_array(g, field, arr)
            except Exception:
                pass
    return n_events


# ── Reader (HDF5 Viewer) ──────────────────────────────────────────────────────

def read_streams(grp) -> dict:
    """{stream: {"time", "data_keys", field: array}} from a scan group; {} for old files."""
    out = {}
    if "streams" not in grp:
        return out
    for sname, g in grp["streams"].items():
        s = {}
        try:
            s["data_keys"] = json.loads(g.attrs.get("data_keys", "{}"))
        except Exception:
            s["data_keys"] = {}
        for field, ds in g.items():
            if isinstance(ds, h5py.Dataset):
                v = ds[()]
                if v.dtype.kind in "OSU":
                    v = np.array([x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in v],
                                 dtype=object)
                s[field] = v
        out[sname] = s
    return out


def read_docs(attrs) -> tuple:
    """(start_doc, stop_doc) dicts from scan-group attributes ({} when absent)."""
    def _load(key):
        try:
            return json.loads(attrs.get(key, "{}"))
        except Exception:
            return {}
    return _load("start_doc"), _load("stop_doc")
