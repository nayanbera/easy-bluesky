"""run_sync.py — helpers for copying run JSONL files from the beamline to the local experiment.

Design (see CLAUDE.md "JSONL sync"):
  * Local presence is checked with ONE directory listing, never one stat per run
    (per-file stats on an NFS experiment folder froze the UI for large experiments).
  * Runs that could not be found are remembered (negative cache) so they are not
    re-requested on every history update.
  * The beamline is searched only in a few known folders (shallow listing); the slow
    deep search is opt-in (manual "Sync" button).
  * Files are transferred as one streamed tar.gz per chunk instead of one SFTP get each.
"""

import json
import os
import time
from pathlib import Path

from .config import _USER_DIR   # ~/.easy_bluesky

_NEG_CACHE = Path(_USER_DIR) / "data" / "jsonl_not_on_remote.json"
_NEG_TTL = 24 * 3600          # seconds before a not-found run is retried automatically
AUTO_LIMIT = 40               # most runs fetched automatically per sync; the rest via the Sync button


def missing_run_uids(runs_dir: str, uids) -> list:
    """UIDs from *uids* without a local <uid>.jsonl — a single listdir, not N stats."""
    try:
        have = set(os.listdir(runs_dir))
    except OSError:
        have = set()
    seen, out = set(), []
    for u in uids:
        if u and u not in seen and f"{u}.jsonl" not in have:
            seen.add(u)
            out.append(u)
    return out


def _load_neg() -> dict:
    try:
        return json.loads(_NEG_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def drop_known_missing(host: str, uids: list) -> list:
    """Remove UIDs that were recently not found on *host* (negative cache)."""
    entries = _load_neg().get(host, {})
    now = time.time()
    return [u for u in uids if now - entries.get(u, 0) > _NEG_TTL]


def record_missing(host: str, uids: list) -> None:
    data = _load_neg()
    entries = data.setdefault(host, {})
    now = time.time()
    entries.update({u: now for u in uids})
    try:
        _NEG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _NEG_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def forget_missing(host: str) -> None:
    """Manual Sync: clear the negative cache so everything is retried."""
    data = _load_neg()
    if data.pop(host, None) is not None:
        try:
            _NEG_CACHE.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
