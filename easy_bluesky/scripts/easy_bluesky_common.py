"""easy_bluesky_common.py — shared RE-side helpers for EasyBluesky.

Uploaded next to re_startup_mongo.py on every RE Manager restart, so plan files
never carry their own copy of the scan-number logic.

Scan numbers
------------
The app stamps ``md["scan_num"]`` when a plan is queued.  That number can be stale
when the queueserver re-queues a finished plan (loop mode).  A number is *stale*
when a finished run already used it.  Everything below works on files in the
experiment directory (shared with the app over NFS):

    scan_nums.jsonl    one {"scan_num", "uid"} line per started run (written by re_startup_mongo)
    queued_scans.jsonl numbers reserved by the app at queue time (+ replacements made here)
    scans_log.json     one entry per finished run (legacy entries have no scan_num)
    plans_log.jsonl    the app's plan log

Use ``@eb_plan`` on plans that need the final number before ``open_run`` (e.g. to
build a file name).  Plans that only pass ``md`` on to a scan need nothing: the
RE preprocessor in re_startup_mongo.py corrects the number at ``open_run``.
"""

import functools
import inspect
import json
import os
import time


def exp_dir_from_md(md: dict) -> str:
    """RE-side experiment directory: remote_exp_dir, then exp_dir, then a local fallback."""
    md = md or {}
    return (md.get("remote_exp_dir") or md.get("exp_dir")
            or os.path.expanduser("~/.easy_bluesky/data"))


def _jsonl(path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except Exception:
        return


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def used_scan_nums(exp_dir: str) -> set:
    """Scan numbers already consumed by a started run in this experiment."""
    used = set()
    for e in _jsonl(os.path.join(exp_dir, "scan_nums.jsonl")):
        n = _as_int(e.get("scan_num"))
        if n is not None:
            used.add(n)
    for e in _jsonl(os.path.join(exp_dir, "plans_log.jsonl")):
        n = _as_int(e.get("scan_num"))
        if n is not None:
            used.add(n)
    # scans_log.json is a JSON list; entries written before scan_num was stored
    # count as having used 1..N (what the old len(entries)+1 counter produced).
    legacy = 0
    try:
        with open(os.path.join(exp_dir, "scans_log.json"), encoding="utf-8") as fh:
            entries = json.load(fh)
        for e in entries if isinstance(entries, list) else []:
            n = _as_int(e.get("scan_num")) if isinstance(e, dict) else None
            if n is None:
                legacy += 1
            else:
                used.add(n)
    except Exception:
        pass
    used.update(range(1, legacy + 1))
    return used


def reserved_scan_nums(exp_dir: str) -> set:
    out = set()
    for e in _jsonl(os.path.join(exp_dir, "queued_scans.jsonl")):
        n = _as_int(e.get("scan_num"))
        if n is not None:
            out.add(n)
    return out


def _append_jsonl(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def record_used(exp_dir: str, scan_num, uid: str = "") -> None:
    """Mark *scan_num* as consumed by the run *uid* (called when the run starts)."""
    n = _as_int(scan_num)
    if n is not None and exp_dir:
        _append_jsonl(os.path.join(exp_dir, "scan_nums.jsonl"),
                      {"scan_num": n, "uid": uid, "time": time.time()})


def fresh_scan_num(md: dict, exp_dir: str = None):
    """Return the scan number this run should use.

    The queued ``md["scan_num"]`` is kept unless a finished run already used it (or
    none was given); then the next free number is taken and reserved so neither the
    app nor another client hands it out again.  Never raises: on any error the
    queued number is returned unchanged.
    """
    md = md or {}
    queued = _as_int(md.get("scan_num"))
    try:
        d = exp_dir or exp_dir_from_md(md)
        used = used_scan_nums(d)
        if queued is not None and queued not in used:
            return queued
        new = max(used | reserved_scan_nums(d), default=0) + 1
        _append_jsonl(os.path.join(d, "queued_scans.jsonl"),
                      {"scan_num": new, "name": md.get("_queue_plan_name", ""),
                       "client_host": "re-manager", "queued_at": time.time()})
        return new
    except Exception:
        return queued


def eb_plan(fn):
    """Decorator for plans: resolve ``md["scan_num"]`` before the plan body runs.

    Keeps the plan's signature (queueserver reads parameters from it) and stays a
    generator function.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            bound = sig.bind_partial(*args, **kwargs)
            md = dict(bound.arguments.get("md") or {})
            md["scan_num"] = fresh_scan_num(md)
            bound.arguments["md"] = md
            args, kwargs = bound.args, bound.kwargs
        except Exception:
            pass
        return (yield from fn(*args, **kwargs))

    return wrapper
