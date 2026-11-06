"""One-line plan summaries and full-argument tooltips for queue / history lists.

Shared by ``experiments_tab`` and ``queue_manager`` (previously duplicated).

``plan_summary(name, kwargs, args, spec)`` returns the text appended after the plan
name in a list row: detectors, motors with range, point count, ``delay`` and every
other parameter the user set (``key=value``).  When the RE Manager plan spec is
supplied, parameters equal to their default are left out and the rest follow the
plan's declared parameter order.

``plan_tooltip(item, spec)`` returns an HTML table with every argument in full.
"""

import html as _html

# kwargs consumed by the structured part of the summary — not repeated as key=value
_HANDLED = {"detectors", "detector_list", "motor", "motors", "start", "stop", "num",
            "delay", "md"}
_MAX_STR = 32


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt_value(v) -> str:
    """Compact, single-line rendering of a parameter value."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, str):
        return v if len(v) <= _MAX_STR else v[:_MAX_STR - 1] + "…"
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        if len(v) > 4 and all(_is_num(x) for x in v):
            return f"[{_fmt_value(v[0])} … {_fmt_value(v[-1])}] ({len(v)})"
        shown = ", ".join(_fmt_value(x) for x in v[:4])
        return f"[{shown}{f', … +{len(v) - 4}' if len(v) > 4 else ''}]"
    if isinstance(v, dict):
        return f"{{{len(v)} keys}}"
    return str(v)


def _is_default(value, spec_param: dict) -> bool:
    """True when ``value`` equals the plan spec's default (a repr string)."""
    if not spec_param or "default" not in spec_param:
        return False
    dflt = spec_param["default"]
    if dflt == repr(value) or dflt == str(value):
        return True
    try:                                   # '1.0' vs 1, '0' vs 0.0 …
        return float(dflt) == float(value)
    except (TypeError, ValueError):
        return False


def _spec_params(spec) -> list:
    return (spec or {}).get("parameters", []) or []


def plan_summary(name: str, kwargs: dict, args: list = None, spec: dict = None) -> str:  # noqa: C901
    kwargs  = dict(kwargs or {})
    args    = list(args or [])
    parts   = []
    name_lc = name.lower()

    # ── mv / mvr: flat [motor, pos, motor, pos, ...] ─────────────────────────
    if name_lc in ("mv", "mvr"):
        pairs = [(args[i], args[i + 1]) for i in range(0, len(args) - 1, 2)]
        if pairs:
            mot_strs = []
            for mot, pos in pairs:
                try:
                    mot_strs.append(f"{mot} → {float(pos):.4g}")
                except (TypeError, ValueError):
                    mot_strs.append(f"{mot} → {pos}")
            parts.append("mot: " + ",  ".join(mot_strs))
        return ("  " + "  |  ".join(parts)) if parts else ""

    # ── Detectors ────────────────────────────────────────────────────────────
    dets = kwargs.get("detectors") or kwargs.get("detector_list", [])
    if isinstance(dets, str):
        dets = [dets]
    if not dets and args and isinstance(args[0], list):
        dets = args[0]
    # Flat positional format: [det_str, motor, start, stop, ...] or [det_str, motor, [pos...]]
    if not dets and len(args) >= 3 and isinstance(args[0], str) and isinstance(args[1], str):
        if isinstance(args[2], (list, tuple)):
            dets = [args[0]]
            args = args[1:]
        else:
            try:
                float(args[2])
                dets = [args[0]]
                args = args[1:]
            except (TypeError, ValueError):
                pass
    if dets:
        parts.append("det: [" + ", ".join(str(d) for d in dets) + "]")

    # ── Motor(s) with range ──────────────────────────────────────────────────
    motor  = kwargs.get("motor")
    motors = kwargs.get("motors")
    start  = kwargs.get("start")
    stop   = kwargs.get("stop")
    num    = kwargs.get("num")
    mot_parts = []

    if motor and _is_num(start) and _is_num(stop):
        s = f"{motor} [{start:.4g} → {stop:.4g}"
        if num is not None:
            s += f", {num} pts"
            num = None
        mot_parts.append(s + "]")
    elif motor:
        mot_parts.append(str(motor))
    elif isinstance(motors, list) and motors:
        mot_parts = [str(m) for m in motors]
    else:
        # Positional: [dets_list, motor, start, stop, ..., num?]  (scan)
        #             [dets_list, motor, start, stop, n, ...]     (grid_scan)
        #             [dets_list, motor, [positions], ...]        (list_scan)
        rest = args[1:] if (args and isinstance(args[0], list)) else args
        is_grid = "grid" in name_lc
        chunk = 4 if is_grid else 3
        i = 0
        while i < len(rest):
            mot_candidate = rest[i]
            if not isinstance(mot_candidate, str):
                break
            if i + 1 < len(rest) and isinstance(rest[i + 1], (list, tuple)):
                pos = rest[i + 1]
                if pos and all(_is_num(x) for x in pos):
                    seg = (f"{mot_candidate} [{pos[0]:.4g} → {pos[-1]:.4g}, {len(pos)} pts]"
                           if len(pos) > 1 else f"{mot_candidate} [{pos[0]:.4g}]")
                else:
                    seg = f"{mot_candidate} [{len(pos)} pts]"
                mot_parts.append(seg)
                i += 2
                continue
            if i + chunk > len(rest):
                break
            try:
                s_val = float(rest[i + 1])
                e_val = float(rest[i + 2])
                seg = f"{mot_candidate} [{s_val:.4g} → {e_val:.4g}"
                if is_grid:
                    seg += f", {int(rest[i + 3])} pts"
                seg += "]"
                mot_parts.append(seg)
                i += chunk
            except (TypeError, ValueError, IndexError):
                break
        # Remaining single item is the global num for non-grid scans
        if not is_grid and i < len(rest) and not any("pts" in m for m in mot_parts):
            try:
                num = int(rest[i])
            except (TypeError, ValueError):
                pass
        elif not is_grid and mot_parts and i < len(rest):
            try:
                num = int(rest[i])
            except (TypeError, ValueError):
                pass

    if mot_parts:
        parts.append("mot: " + ",  ".join(mot_parts))

    # ── num pts / delay ──────────────────────────────────────────────────────
    num_shown = any("pts" in p for p in parts)
    if num is not None and not num_shown:
        parts.append(f"{num} pts")
    delay = kwargs.get("delay")
    if delay is not None:
        parts.append(f"delay={delay:.4g} s" if _is_num(delay) else f"delay={delay}")

    # ── Everything else the user set (non-default), in plan-declared order ───
    by_name = {p.get("name"): p for p in _spec_params(spec)}
    order   = [p.get("name") for p in _spec_params(spec)]
    extra   = [k for k in kwargs if k not in _HANDLED]
    extra.sort(key=lambda k: order.index(k) if k in order else len(order))
    for k in extra:
        v = kwargs[k]
        if _is_default(v, by_name.get(k)):
            continue
        parts.append(f"{k}={_fmt_value(v)}")

    return "  " + "  |  ".join(parts) if parts else ""


# ── Tooltip ──────────────────────────────────────────────────────────────────

def _tip_value(v, limit: int = 400) -> str:
    if isinstance(v, float):
        s = f"{v:.6g}"
    elif isinstance(v, (list, tuple)) and v and all(_is_num(x) for x in v):
        s = "[" + ", ".join(f"{x:.6g}" if isinstance(x, float) else str(x) for x in v) + "]"
        if len(v) > 8:
            s = f"{len(v)} values: {s}"
    else:
        s = repr(v) if not isinstance(v, str) else v
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return _html.escape(s)


def plan_tooltip(item: dict, spec: dict = None, extra_note: str = "") -> str:
    """HTML tooltip: plan name, every parameter (with defaults flagged), metadata."""
    name   = item.get("name", "?")
    args   = list(item.get("args", []) or [])
    kwargs = dict(item.get("kwargs", {}) or {})
    md     = kwargs.pop("md", None) or {}
    params = _spec_params(spec)
    by_name = {p.get("name"): p for p in params}

    rows = []

    def row(k, v, dim=False):
        style = ' style="color:#888"' if dim else ""
        rows.append(f"<tr><td{style}><b>{_html.escape(str(k))}</b>&nbsp;&nbsp;</td>"
                    f"<td{style}>{_tip_value(v)}</td></tr>")

    # positional args — label with the plan's parameter names when they line up
    pos_names = [p.get("name") for p in params
                 if (p.get("kind") or {}).get("name") in ("POSITIONAL_OR_KEYWORD", "POSITIONAL_ONLY")]
    var_pos = next((p.get("name") for p in params
                    if (p.get("kind") or {}).get("name") == "VAR_POSITIONAL"), "args")
    for i, a in enumerate(args):
        row(pos_names[i] if i < len(pos_names) else f"{var_pos}[{i - len(pos_names)}]", a)

    order = [p.get("name") for p in params]
    for k in sorted(kwargs, key=lambda k: order.index(k) if k in order else len(order)):
        row(k, kwargs[k], dim=_is_default(kwargs[k], by_name.get(k)))

    # parameters the user left at their default — listed dimmed for completeness
    given = set(kwargs) | set(pos_names[:len(args)])
    for p in params:
        pn = p.get("name")
        if pn in given or pn == "md" or "default" not in p:
            continue
        if (p.get("kind") or {}).get("name") in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            continue
        rows.append(f"<tr><td style='color:#888'><b>{_html.escape(str(pn))}</b>&nbsp;&nbsp;</td>"
                    f"<td style='color:#888'>{_html.escape(str(p['default']))} <i>(default)</i></td></tr>")

    if md:
        rows.append("<tr><td colspan='2'><hr></td></tr>")
        for k, v in md.items():
            row(f"md.{k}", v, dim=True)

    note = f"<p style='color:#e8a44a'>{_html.escape(extra_note)}</p>" if extra_note else ""
    return (f"<html><b>{_html.escape(name)}</b>{note}"
            f"<table cellspacing='2'>{''.join(rows)}</table></html>")
