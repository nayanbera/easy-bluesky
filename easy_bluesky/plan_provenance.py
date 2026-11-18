"""plan_provenance.py — where and when plans were uploaded, and who they would replace.

Uploads (script_upload) carry no information about the sender, so the app appends a
call to ``easy_bluesky_common.record_plan_upload`` to every script it uploads.  The RE
side keeps the records (memory + ``plan_uploads.jsonl``) and returns them through
``get_plan_origins``; the helpers here build that call, turn a record into text for
tooltips, the plan table and the plan dialog, and find the plans an upload would replace.
"""

import ast
import getpass
import socket
from datetime import datetime


def this_host() -> str:
    return socket.gethostname()


def this_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def plan_names_in(code: str) -> list:
    """Top-level functions (the plans) that *code* defines; [] if it does not parse."""
    try:
        tree = ast.parse(code)
    except Exception:
        return []
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")]


def with_provenance(code: str, source: str) -> str:
    """*code* followed by a call that records this upload on the RE side."""
    names = plan_names_in(code)
    if not names:
        return code
    return (
        f"{code.rstrip()}\n\n\n"
        "try:\n"
        "    from easy_bluesky_common import record_plan_upload as _eb_record_upload\n"
        f"    _eb_record_upload({names!r}, host={this_host()!r}, user={this_user()!r}, "
        f"source={source!r})\n"
        "except Exception:\n"
        "    pass\n"
    )


def _fmt_time(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


def _who(rec: dict) -> str:
    host, user = rec.get("host") or "?", rec.get("user") or ""
    mine = ", this computer" if rec.get("host") == this_host() else ""
    return f"{host} ({user}{mine})" if user else f"{host}{' (this computer)' if mine else ''}"


def origin_columns(rec) -> tuple:
    """(origin, updated) strings for the plan table."""
    if not rec:
        return "", ""
    if "file" in rec:
        saved = rec.get("saved_by")
        who = (saved.get("host") or "") if saved else ""
        return (f"{who} · " if who else "") + rec["file"].rsplit("/", 1)[-1], \
            _fmt_time(saved.get("time") if saved else rec.get("file_mtime"))
    src = (rec.get("source") or "").replace("\\", "/").rsplit("/", 1)[-1]
    return f"{rec.get('host') or '?'} · {src}" if src else rec.get("host") or "?", \
        _fmt_time(rec.get("time"))


def describe_origin(rec, builtin: bool = False) -> str:
    """Multi-line plain text for a tooltip or label."""
    if builtin:
        return "Bluesky library plan"
    if not rec:
        return "Origin not recorded (uploaded before tracking, or restart the RE Manager)"
    if "file" in rec:
        lines = [f"Defined in a file on the RE computer: {rec['file']}",
                 f"File modified: {_fmt_time(rec.get('file_mtime'))}"]
        saved = rec.get("saved_by")
        if saved:
            lines.append(f"Saved from {_who(saved)} at {_fmt_time(saved.get('time'))}")
        return "\n".join(lines)
    lines = [f"Uploaded from {_who(rec)} at {_fmt_time(rec.get('time'))}",
             f"Source: {rec.get('source') or '?'}"]
    prev = rec.get("previous")
    if prev:
        lines.append(f"Replaced an upload from {_who(prev)} at {_fmt_time(prev.get('time'))} "
                     f"(source: {prev.get('source') or '?'})")
    return "\n".join(lines)


def find_overwrites(names, plans: dict, origins, catalog=None) -> list:
    """Plans among *names* that already exist in the environment, with their origin.

    Each entry: {"name", "rec", "builtin", "own"}; ``own`` is True when this computer
    uploaded the existing definition itself (replacing it is routine).
    """
    out = []
    host = this_host()
    for n in names:
        if n not in (plans or {}):
            continue
        rec = (origins or {}).get(n)
        builtin = False
        if catalog is not None:
            try:
                from .plans_manager import PLAN_TYPE_BUILTIN
                builtin = catalog.get_type(n) == PLAN_TYPE_BUILTIN
            except Exception:
                pass
        if rec and "file" in rec:        # defined in a file on the RE computer
            own = (rec.get("saved_by") or {}).get("host") == host
        else:                            # injected with script_upload
            own = bool(rec) and rec.get("host") == host
        out.append({"name": n, "rec": rec, "builtin": builtin, "own": own})
    return out


def confirm_overwrite(parent, conflicts: list, origins_known: bool, what: str) -> bool:
    """Ask before an upload replaces plans that this computer did not upload itself.

    Returns True to go ahead.  Routine replacements (own earlier uploads) never ask.
    """
    risky = [c for c in conflicts if not c["own"]]
    if not risky:
        return True
    from qtpy.QtWidgets import QMessageBox
    lines = []
    for c in risky:
        if c["builtin"]:
            why = "Bluesky library plan"
        elif not origins_known:
            why = "origin unknown (restart the RE Manager to record origins)"
        else:
            why = describe_origin(c["rec"]).replace("\n", "\n      ")
        lines.append(f"• {c['name']}\n      {why}")
    mb = QMessageBox(parent)
    mb.setIcon(QMessageBox.Icon.Warning)
    mb.setWindowTitle("Plans will be replaced")
    mb.setText(f"{what} replaces {len(risky)} plan"
               f"{'s' if len(risky) != 1 else ''} that this computer did not upload:")
    mb.setInformativeText("\n".join(lines))
    mb.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
    mb.button(QMessageBox.StandardButton.Ok).setText("Upload and replace")
    mb.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return mb.exec() == QMessageBox.StandardButton.Ok
