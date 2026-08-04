"""plan_builder.py — Plan Composer: visual sequence builder + code editor."""

import uuid
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QPlainTextEdit, QComboBox, QLineEdit, QMessageBox,
    QFormLayout, QDoubleSpinBox, QSpinBox, QFrame, QScrollArea, QTabWidget,
    QFileDialog, QCheckBox, QInputDialog, QMenu,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QMimeData, QThread
from .widgets import NoScrollSpinBox, NoScrollDoubleSpinBox
from PyQt6.QtGui import QFont, QColor, QDrag

from .highlighter import PythonHighlighter
from .code_editor import CodeEditor
from .widgets import ParamForm


# ── RE Console Monitor Dialog ──────────────────────────────────────────────────

# ── Block type registry ────────────────────────────────────────────────────────

BLOCK_DEFS = {
    "move": {
        "label": "Move", "category": "Motion", "icon": "→",
        "params": [
            {"name": "device",   "type": "str",   "default": "", "hint": "motor name",        "widget": "device_single"},
            {"name": "position", "type": "float", "default": 0.0, "hint": "target position"},
        ],
    },
    "rel_move": {
        "label": "Relative Move", "category": "Motion", "icon": "↔",
        "params": [
            {"name": "device", "type": "str",   "default": "", "hint": "motor name",        "widget": "device_single"},
            {"name": "delta",  "type": "float", "default": 0.0, "hint": "relative distance"},
        ],
    },
    "sleep": {
        "label": "Sleep", "category": "Timing", "icon": "⏱",
        "params": [
            {"name": "seconds", "type": "float", "default": 1.0, "hint": "delay in seconds"},
        ],
    },
    "set_attr": {
        "label": "Set Attribute", "category": "Device", "icon": "⚙",
        "params": [
            {"name": "device",    "type": "str", "default": "", "hint": "device name",            "widget": "device_any"},
            {"name": "attribute", "type": "str", "default": "", "hint": "e.g. cam.acquire_time"},
            {"name": "value",     "type": "str", "default": "", "hint": "value to set"},
        ],
    },
    "set_exposure": {
        "label": "Set Exposure", "category": "Detector", "icon": "⏲",
        "params": [
            {"name": "detectors",     "type": "str",   "default": "",  "hint": "detector names",          "widget": "device_multi"},
            {"name": "exposure_attr", "type": "str",   "default": "cam.acquire_time", "hint": "attribute path on each detector"},
            {"name": "exposure_time", "type": "float", "default": 1.0, "hint": "exposure time in seconds"},
        ],
    },
    "set_file": {
        "label": "Set AD File", "category": "Detector", "icon": "🗂",
        "params": [
            {"name": "detector",   "type": "str", "default": "",         "hint": "AreaDetector device name", "widget": "device_multi"},
            {"name": "plugin",     "type": "str", "default": "hdf1",     "hint": "file plugin (hdf1, tiff1, etc.)"},
            {"name": "file_path",  "type": "str", "default": "/data/",   "hint": "save directory"},
            {"name": "file_name",  "type": "str", "default": "scan",     "hint": "file name prefix"},
        ],
    },
    "stage": {
        "label": "Stage Device", "category": "Device", "icon": "▲",
        "params": [
            {"name": "device", "type": "str", "default": "", "hint": "device to stage",   "widget": "device_any"},
        ],
    },
    "unstage": {
        "label": "Unstage Device", "category": "Device", "icon": "▼",
        "params": [
            {"name": "device", "type": "str", "default": "", "hint": "device to unstage", "widget": "device_any"},
        ],
    },
    "open_shutter": {
        "label": "Open Shutter", "category": "Shutter", "icon": "◉",
        "params": [
            {"name": "shutter", "type": "str", "default": "", "hint": "shutter device name", "widget": "device_single"},
        ],
    },
    "close_shutter": {
        "label": "Close Shutter", "category": "Shutter", "icon": "○",
        "params": [
            {"name": "shutter", "type": "str", "default": "", "hint": "shutter device name", "widget": "device_single"},
        ],
    },
    "trigger_read": {
        "label": "Trigger & Read", "category": "Detector", "icon": "📷",
        "params": [
            {"name": "detectors", "type": "str", "default": "", "hint": "detector names", "widget": "device_multi"},
        ],
    },
    "scan": {
        "label": "Scan", "category": "Plans", "icon": "⟳",
        "params": [
            {"name": "detectors", "type": "str", "default": "",    "hint": "detectors",                                         "widget": "device_multi"},
            {"name": "motor",     "type": "str", "default": "",    "hint": "motor name(s)",                                     "widget": "device_single"},
            {"name": "start",     "type": "str", "default": "0.0", "hint": "start position(s) — comma-separated if multi-motor"},
            {"name": "stop",      "type": "str", "default": "1.0", "hint": "stop position(s)  — comma-separated if multi-motor"},
            {"name": "num",       "type": "int", "default": 11,    "hint": "number of points"},
        ],
    },
    "count": {
        "label": "Count", "category": "Plans", "icon": "●",
        "params": [
            {"name": "detectors", "type": "str",   "default": "", "hint": "detectors",   "widget": "device_multi"},
            {"name": "num",       "type": "int",   "default": 1,  "hint": "number of acquisitions"},
            {"name": "delay",     "type": "float", "default": 0.0, "hint": "delay between acquisitions"},
        ],
    },
    "rel_scan": {
        "label": "Relative Scan", "category": "Plans", "icon": "↔",
        "params": [
            {"name": "detectors", "type": "str", "default": "",    "hint": "detectors",                                          "widget": "device_multi"},
            {"name": "motor",     "type": "str", "default": "",    "hint": "motor name(s)",                                      "widget": "device_single"},
            {"name": "start",     "type": "str", "default": "-1.0","hint": "start (relative to current), comma-sep if multi-motor"},
            {"name": "stop",      "type": "str", "default": "1.0", "hint": "stop (relative to current),  comma-sep if multi-motor"},
            {"name": "num",       "type": "int", "default": 11,    "hint": "number of points"},
        ],
    },
    "grid_scan": {
        "label": "Grid Scan", "category": "Plans", "icon": "⊞",
        "params": [
            {"name": "detectors",    "type": "str",  "default": "",         "hint": "detectors",                                     "widget": "device_multi"},
            {"name": "motor",        "type": "str",  "default": "",         "hint": "spatial motors, comma-sep (≥2)",                "widget": "device_single"},
            {"name": "start",        "type": "str",  "default": "0.0, 0.0", "hint": "start positions, comma-sep per motor"},
            {"name": "stop",         "type": "str",  "default": "1.0, 1.0", "hint": "stop positions,  comma-sep per motor"},
            {"name": "num",          "type": "str",  "default": "5, 5",     "hint": "num points,       comma-sep per motor"},
            {"name": "snake_axes",   "type": "bool", "default": False,      "hint": "snake-boustrophedon scan axes"},
            {"name": "energy_motor", "type": "str",  "default": "",         "hint": "energy motor — added as innermost axis (leave blank to skip)", "widget": "device_single"},
            {"name": "energy_start", "type": "str",  "default": "7000",     "hint": "energy start (eV)"},
            {"name": "energy_stop",  "type": "str",  "default": "7100",     "hint": "energy stop (eV)"},
            {"name": "energy_num",   "type": "int",  "default": 10,         "hint": "energy points"},
        ],
    },
    "list_scan": {
        "label": "List Scan", "category": "Plans", "icon": "≣",
        "params": [
            {"name": "detectors",    "type": "str", "default": "",               "hint": "detectors",                                            "widget": "device_multi"},
            {"name": "motor",        "type": "str", "default": "",               "hint": "spatial motor",                                        "widget": "device_single"},
            {"name": "positions",    "type": "str", "default": "0.0, 1.0, 2.0", "hint": "explicit positions, comma-sep"},
            {"name": "energy_motor", "type": "str", "default": "",               "hint": "energy motor — inner loop at each position (leave blank to skip)", "widget": "device_single"},
            {"name": "energy_start", "type": "str", "default": "7000",           "hint": "energy start (eV)"},
            {"name": "energy_stop",  "type": "str", "default": "7100",           "hint": "energy stop (eV)"},
            {"name": "energy_num",   "type": "int", "default": 10,               "hint": "energy points"},
        ],
    },
    "adaptive_scan": {
        "label": "Adaptive Scan", "category": "Plans", "icon": "≈",
        "params": [
            {"name": "detectors",    "type": "str",   "default": "",    "hint": "detectors",                                             "widget": "device_multi"},
            {"name": "target_field", "type": "str",   "default": "",    "hint": "field to adapt on, e.g. Pil300K_stats1_total"},
            {"name": "motor",        "type": "str",   "default": "",    "hint": "motor to scan",                                         "widget": "device_single"},
            {"name": "start",        "type": "float", "default": 0.0,   "hint": "start position"},
            {"name": "stop",         "type": "float", "default": 1.0,   "hint": "stop position"},
            {"name": "min_step",     "type": "float", "default": 0.001, "hint": "minimum step size"},
            {"name": "max_step",     "type": "float", "default": 0.1,   "hint": "maximum step size"},
            {"name": "target_delta", "type": "float", "default": 0.05,  "hint": "target fractional change in target_field per step"},
            {"name": "backstep",     "type": "bool",  "default": True,  "hint": "allow motor to step back toward better signal"},
            {"name": "threshold",    "type": "float", "default": 0.8,   "hint": "fraction of target_delta that triggers a backstep"},
        ],
    },
    "flyscan": {
        "label": "Fly Scan", "category": "Plans", "icon": "✈",
        "params": [
            {"name": "flyer",    "type": "str",   "default": "",   "hint": "flyer device (must implement Flyable protocol)", "widget": "device_any"},
            {"name": "motor",    "type": "str",   "default": "",   "hint": "motor to position/configure before fly (optional)", "widget": "device_single"},
            {"name": "start",    "type": "float", "default": 0.0,  "hint": "move motor to this position before triggering fly"},
            {"name": "velocity", "type": "float", "default": 0.5,  "hint": "motor.velocity to set before fly (units/s)"},
        ],
    },
    "plan_stub": {
        "label": "Plan Stub", "category": "Plans", "icon": "⋯",
        "params": [
            {"name": "stub_name", "type": "str", "default": "", "hint": "e.g. bps.abs_set"},
            {"name": "args",      "type": "str", "default": "", "hint": "comma-separated args"},
        ],
    },
    # ── Flow control ─────────────────────────────────────────────────────────────
    "repeat_n": {
        "label": "Repeat N Times", "category": "Flow", "icon": "↻",
        "params": [
            {"name": "count", "type": "int", "default": 2, "hint": "number of repetitions"},
        ],
    },
    "for_each_position": {
        "label": "For Each Position", "category": "Flow", "icon": "⟳",
        "params": [
            {"name": "motor",      "type": "str",   "default": "",               "hint": "motor name",                  "widget": "device_single"},
            {"name": "detectors",  "type": "str",   "default": "",               "hint": "detectors to read",           "widget": "device_multi"},
            {"name": "positions",  "type": "str",   "default": "0.0, 1.0, 2.0",  "hint": "comma-separated positions"},
            {"name": "num",        "type": "int",   "default": 1,                "hint": "acquisitions per position"},
            {"name": "delay",      "type": "float", "default": 0.0,              "hint": "delay between acquisitions (s)"},
        ],
    },
    "custom_python": {
        "label": "Custom Python", "category": "Flow", "icon": "⌨",
        "params": [
            {"name": "code", "type": "str", "default": "# custom code\npass", "hint": "Python code", "widget": "code"},
        ],
    },
}

_PLAN_BLOCKS = {"scan", "count", "rel_scan", "grid_scan", "list_scan"}   # blocks that support per-step injection
_FLOW_BLOCKS = {"repeat_n", "for_each_position", "custom_python"}

_CATEGORY_ORDER = ["Motion", "Timing", "Detector", "Shutter", "Device", "Plans", "Flow"]

# Convenience groups shown as workflow hints in the palette tooltip
_WORKFLOW_HINT = (
    "Typical scan workflow:\n"
    "  Main:     Set Exposure → Set AD File → Scan\n"
    "  Per-step: Open Shutter → Trigger & Read → Close Shutter → Sleep\n"
    "\n"
    "Flow blocks (purple):\n"
    "  Repeat N Times — wraps whole sequence in a loop\n"
    "  For Each Position — move motor, repeat at each\n"
    "  Custom Python — inject arbitrary code"
)


# ── Block helpers ──────────────────────────────────────────────────────────────

def _new_block(btype: str) -> dict:
    defn = BLOCK_DEFS[btype]
    return {
        "id":     str(uuid.uuid4())[:8],
        "type":   btype,
        "params": {p["name"]: p["default"] for p in defn["params"]},
    }


def _block_summary(block: dict) -> str:
    defn = BLOCK_DEFS[block["type"]]
    p = block["params"]
    icon = defn["icon"]
    name = defn["label"]
    btype = block["type"]
    if btype == "move":
        return f"{icon}  Move  {p['device']} → {p['position']}"
    if btype == "rel_move":
        return f"{icon}  Rel Move  {p['device']} ±{p['delta']}"
    if btype == "sleep":
        return f"{icon}  Sleep  {p['seconds']} s"
    if btype == "set_attr":
        return f"{icon}  Set  {p['device']}.{p['attribute']} = {p['value']}"
    if btype == "open_shutter":
        return f"{icon}  Open  {p['shutter']}"
    if btype == "close_shutter":
        return f"{icon}  Close  {p['shutter']}"
    if btype == "set_exposure":
        return f"{icon}  Set Exposure  [{p['detectors']}]  {p['exposure_attr']}={p['exposure_time']}s"
    if btype == "set_file":
        return f"{icon}  Set AD File  {p['detector']}.{p['plugin']}  {p['file_path']}{p['file_name']}"
    if btype == "stage":
        return f"{icon}  Stage  {p['device']}"
    if btype == "unstage":
        return f"{icon}  Unstage  {p['device']}"
    if btype == "trigger_read":
        return f"{icon}  Trigger & Read  [{p['detectors']}]"
    if btype == "scan":
        return f"{icon}  Scan  {p['motor']}  {p['start']}→{p['stop']}  ×{p['num']}"
    if btype == "count":
        return f"{icon}  Count  [{p['detectors']}]  ×{p['num']}"
    if btype == "rel_scan":
        return f"{icon}  RelScan  {p['motor']}  {p['start']}→{p['stop']}  ×{p['num']}"
    if btype == "grid_scan":
        snake  = " ~" if p.get("snake_axes") else ""
        e_part = f"  ⚡{p['energy_motor']} {p['energy_start']}→{p['energy_stop']}×{p['energy_num']}" if p.get("energy_motor") else ""
        return f"{icon}  GridScan  {p['motor']}  [{p['start']}]→[{p['stop']}]  {p['num']}{snake}{e_part}"
    if btype == "list_scan":
        e_part = f"  ⚡{p['energy_motor']} {p['energy_start']}→{p['energy_stop']}×{p['energy_num']}" if p.get("energy_motor") else ""
        return f"{icon}  ListScan  {p['motor']}  [{p['positions']}]{e_part}"
    if btype == "adaptive_scan":
        return (f"{icon}  AdaptScan  {p['motor']}  {p['start']}→{p['stop']}"
                f"  Δ{p['target_delta']}  [{p['min_step']},{p['max_step']}]")
    if btype == "flyscan":
        motor_part = f"  {p['motor']}@{p['velocity']}/s→{p['start']}" if p.get("motor") else ""
        return f"{icon}  FlyScan  {p['flyer']}{motor_part}"
    if btype == "plan_stub":
        return f"{icon}  {p['stub_name']}({p['args']})"
    if btype == "repeat_n":
        return f"{icon}  Repeat  ×{p['count']}"
    if btype == "for_each_position":
        return f"{icon}  For  {p['motor']} in [{p['positions']}]  ×{p.get('num', 1)}"
    if btype == "custom_python":
        first = str(p.get("code", "")).split("\n")[0].strip()
        return f"{icon}  {first[:50]}" if first else f"{icon}  Custom Python"
    return f"{icon}  {name}"


def _block_to_code(block: dict, indent: int = 4, per_step_name: str = None,
                   param_map: dict = None) -> str:
    """Generate code for one block.

    param_map maps semantic names ("detectors", "motor", "shutter", …) to the
    variable name to emit.  When a key is present the parameter variable is used
    instead of the hardcoded value from Block Properties.
    """
    p   = block["params"]
    pm  = param_map or {}
    pad = " " * indent
    btype = block["type"]

    if btype == "move":
        return f"{pad}yield from bps.mv({p['device']}, {p['position']})"
    if btype == "rel_move":
        return f"{pad}yield from bps.mvr({p['device']}, {p['delta']})"
    if btype == "sleep":
        return f"{pad}yield from bps.sleep({p['seconds']})"
    if btype == "set_attr":
        return f"{pad}yield from bps.mv({p['device']}.{p['attribute']}, {p['value']})"
    if btype == "open_shutter":
        shutter = pm.get("shutter", p["shutter"])
        return f"{pad}yield from bps.mv({shutter}, 'open')"
    if btype == "close_shutter":
        shutter = pm.get("shutter", p["shutter"])
        return f"{pad}yield from bps.mv({shutter}, 'closed')"
    if btype == "set_exposure":
        exp_time = pm.get("exposure_time", p["exposure_time"])
        attr     = p["exposure_attr"]
        if "detectors" in pm:
            dets_var = pm["detectors"]
            return (f"{pad}for _det in {dets_var}:\n"
                    f"{pad}    yield from bps.mv(_det.{attr}, {exp_time})")
        dets = [d.strip() for d in p["detectors"].split(",") if d.strip()]
        lines = [f"{pad}yield from bps.mv({d}.{attr}, {exp_time})" for d in dets]
        return "\n".join(lines) if lines else f"{pad}pass  # no detectors specified"
    if btype == "set_file":
        det, plug = p["detector"], p["plugin"]
        return (
            f"{pad}yield from bps.abs_set({det}.{plug}.file_path, '{p['file_path']}', wait=True)\n"
            f"{pad}yield from bps.abs_set({det}.{plug}.file_name, '{p['file_name']}', wait=True)"
        )
    if btype == "stage":
        return f"{pad}yield from bps.stage({p['device']})"
    if btype == "unstage":
        return f"{pad}yield from bps.unstage({p['device']})"
    if btype == "trigger_read":
        # detectors is already a list variable; no extra brackets needed
        dets = pm["detectors"] if "detectors" in pm else f"[{p['detectors']}]"
        return f"{pad}yield from bps.trigger_and_read({dets})"
    if btype == "scan":
        dets    = pm["detectors"] if "detectors" in pm else f"[{p['detectors']}]"
        mot_raw = pm.get("motor", p["motor"])
        start   = pm.get("start", p["start"])
        stop    = pm.get("stop",  p["stop"])
        num     = pm.get("num",   p["num"])
        ps_arg  = f", per_step={per_step_name}" if per_step_name else ""

        if "," in str(mot_raw) and "motor" not in pm:
            # Multi-motor hardcoded: zip each motor with its own start/stop.
            # start/stop may be comma-separated too; pad to motor count if fewer.
            motors = [m.strip() for m in str(mot_raw).split(",") if m.strip()]
            starts = [s.strip() for s in str(start).split(",") if s.strip()]
            stops  = [s.strip() for s in str(stop).split(",")  if s.strip()]
            while len(starts) < len(motors):
                starts.append(starts[-1] if starts else "0.0")
            while len(stops)  < len(motors):
                stops.append(stops[-1]  if stops  else "1.0")
            triplets = ", ".join(
                f"{m}, {s0}, {s1}" for m, s0, s1 in zip(motors, starts, stops)
            )
            return f"{pad}yield from bp.scan({dets}, {triplets}, {num}{ps_arg})"
        elif mot_raw == "motor":
            # Parametric single motor — list-unpack handles Movable or List[Movable]
            return (f"{pad}yield from bp.scan("
                    f"{dets}, *[x for _m in (motor if isinstance(motor, list) else [motor])"
                    f" for x in (_m, {start}, {stop})], {num}{ps_arg})")
        else:
            return (f"{pad}yield from bp.scan({dets}, {mot_raw}, "
                    f"{start}, {stop}, {num}{ps_arg})")
    if btype == "count":
        dets  = pm["detectors"] if "detectors" in pm else f"[{p['detectors']}]"
        num   = pm.get("num",   p["num"])
        delay = pm.get("delay", p["delay"])
        ps_arg = f", per_step={per_step_name}" if per_step_name else ""
        return (f"{pad}yield from bp.count({dets}, num={num}, delay={delay}{ps_arg})")
    if btype == "rel_scan":
        dets    = pm["detectors"] if "detectors" in pm else f"[{p['detectors']}]"
        mot_raw = pm.get("motor", p["motor"])
        start   = pm.get("start", p["start"])
        stop    = pm.get("stop",  p["stop"])
        num     = pm.get("num",   p["num"])
        ps_arg  = f", per_step={per_step_name}" if per_step_name else ""
        if "," in str(mot_raw) and "motor" not in pm:
            motors = [m.strip() for m in str(mot_raw).split(",") if m.strip()]
            starts = [s.strip() for s in str(start).split(",") if s.strip()]
            stops  = [s.strip() for s in str(stop).split(",")  if s.strip()]
            while len(starts) < len(motors): starts.append(starts[-1] if starts else "-1.0")
            while len(stops)  < len(motors): stops.append(stops[-1]   if stops  else "1.0")
            triplets = ", ".join(f"{m}, {s0}, {s1}" for m, s0, s1 in zip(motors, starts, stops))
            return f"{pad}yield from bp.rel_scan({dets}, {triplets}, {num}{ps_arg})"
        elif mot_raw == "motor":
            return (f"{pad}yield from bp.rel_scan("
                    f"{dets}, *[x for _m in (motor if isinstance(motor, list) else [motor])"
                    f" for x in (_m, {start}, {stop})], {num}{ps_arg})")
        else:
            return f"{pad}yield from bp.rel_scan({dets}, {mot_raw}, {start}, {stop}, {num}{ps_arg})"

    if btype == "grid_scan":
        dets   = pm["detectors"] if "detectors" in pm else f"[{p['detectors']}]"
        motors = [m.strip() for m in str(p["motor"]).split(",") if m.strip()]
        starts = [s.strip() for s in str(p["start"]).split(",") if s.strip()]
        stops  = [s.strip() for s in str(p["stop"]).split(",")  if s.strip()]
        nums   = [n.strip() for n in str(p["num"]).split(",")   if n.strip()]
        while len(starts) < len(motors): starts.append(starts[-1] if starts else "0.0")
        while len(stops)  < len(motors): stops.append(stops[-1]   if stops  else "1.0")
        while len(nums)   < len(motors): nums.append(nums[-1]     if nums   else "5")
        args = ", ".join(
            f"{m}, {s0}, {s1}, {n}"
            for m, s0, s1, n in zip(motors, starts, stops, nums)
        )
        # Optional energy inner axis: appended last so it varies fastest
        e_motor = p.get("energy_motor", "")
        if e_motor:
            e_start = p.get("energy_start", "7000")
            e_stop  = p.get("energy_stop",  "7100")
            e_num   = p.get("energy_num",   10)
            args += f", {e_motor}, {e_start}, {e_stop}, {e_num}"
        snake  = p.get("snake_axes", False)
        ps_arg = f", per_step={per_step_name}" if per_step_name else ""
        return f"{pad}yield from bp.grid_scan({dets}, {args}, snake_axes={snake}{ps_arg})"

    if btype == "list_scan":
        dets   = pm["detectors"] if "detectors" in pm else f"[{p['detectors']}]"
        motor  = pm.get("motor", p.get("motor", ""))
        pos    = [x.strip() for x in str(p.get("positions", "")).split(",") if x.strip()]
        ps_arg = f", per_step={per_step_name}" if per_step_name else ""
        e_motor = p.get("energy_motor", "")
        if e_motor:
            # Nested loop: move to each spatial position, then do an energy scan
            e_start = p.get("energy_start", "7000")
            e_stop  = p.get("energy_stop",  "7100")
            e_num   = p.get("energy_num",   10)
            inner   = pad + "    "
            return "\n".join([
                f"{pad}for _pos in [{', '.join(pos)}]:",
                f"{inner}yield from bps.mv({motor}, _pos)",
                f"{inner}yield from bp.scan({dets}, {e_motor}, {e_start}, {e_stop}, {e_num}{ps_arg})",
            ])
        return f"{pad}yield from bp.list_scan({dets}, {motor}, [{', '.join(pos)}]{ps_arg})"

    if btype == "adaptive_scan":
        dets     = pm["detectors"]    if "detectors"    in pm else f"[{p['detectors']}]"
        motor    = pm.get("motor",        p.get("motor",        ""))
        start    = pm.get("start",        p.get("start",        0.0))
        stop     = pm.get("stop",         p.get("stop",         1.0))
        min_step = pm.get("min_step",     p.get("min_step",   0.001))
        max_step = pm.get("max_step",     p.get("max_step",     0.1))
        t_delta  = pm.get("target_delta", p.get("target_delta", 0.05))
        backstep = p.get("backstep", True)
        threshold= pm.get("threshold",    p.get("threshold",    0.8))
        field    = p.get("target_field", "")
        return (
            f"{pad}yield from bp.adaptive_scan(\n"
            f"{pad}    {dets}, \"{field}\",\n"
            f"{pad}    {motor}, {start}, {stop},\n"
            f"{pad}    {min_step}, {max_step},\n"
            f"{pad}    {t_delta}, {backstep},\n"
            f"{pad}    threshold={threshold})"
        )
    if btype == "flyscan":
        flyer    = p.get("flyer", "")
        motor    = p.get("motor", "")
        start    = p.get("start", 0.0)
        velocity = p.get("velocity", 0.5)
        lines = []
        if motor:
            lines.append(f"{pad}yield from bps.abs_set({motor}.velocity, {velocity}, wait=True)")
            lines.append(f"{pad}yield from bps.mv({motor}, {start})")
        lines.append(f"{pad}yield from bp.fly([{flyer}])")
        return "\n".join(lines)
    if btype == "plan_stub":
        return f"{pad}yield from {p['stub_name']}({p['args']})"
    if btype == "repeat_n":
        return ""   # handled at generate_plan_code level as a body wrapper
    if btype == "for_each_position":
        motor = pm.get("motor", p.get("motor", "motor"))
        dets  = pm.get("detectors", p.get("detectors", ""))
        dets_expr = f"[{dets}]" if dets else "detectors"
        raw_pos = str(p.get("positions", ""))
        pos_list = "[" + ", ".join(
            x.strip() for x in raw_pos.split(",") if x.strip()
        ) + "]"
        num   = p.get("num", 1)
        delay = p.get("delay", 0.0)
        inner = f"{pad}    "
        lines = [
            f"{pad}for _pos in {pos_list}:",
            f"{inner}yield from bps.mv({motor}, _pos)",
            f"{inner}yield from bp.count({dets_expr}, num={num}, delay={delay})",
        ]
        return "\n".join(lines)
    if btype == "custom_python":
        code = p.get("code", "pass")
        lines = []
        for line in code.splitlines():
            lines.append(pad + line if line.strip() else "")
        return "\n".join(lines) if lines else f"{pad}pass"
    return f"{pad}pass  # unknown: {btype}"


# ── Sequence list widget ───────────────────────────────────────────────────────

class SequenceList(QListWidget):
    """Drag-to-reorder list of plan blocks."""
    block_selected = pyqtSignal(object)   # emits block dict or None
    sequence_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._blocks = []   # authoritative list; avoids item.data() copy issues
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMinimumHeight(120)
        self.currentItemChanged.connect(self._on_selection)
        self.model().rowsMoved.connect(self._on_rows_moved)

    def _on_rows_moved(self, *_):
        # Re-sync _blocks to match the new visual order after a drag.
        self._blocks = [self.item(i).data(Qt.ItemDataRole.UserRole)
                        for i in range(self.count())]
        self.sequence_changed.emit()

    def _make_item(self, block: dict) -> QListWidgetItem:
        item = QListWidgetItem(self._make_label(block))
        item.setData(Qt.ItemDataRole.UserRole, block)
        if block["type"] in _PLAN_BLOCKS:
            item.setForeground(QColor("#1f77b4"))
            f = QFont(); f.setBold(True)
            item.setFont(f)
        elif block["type"] in _FLOW_BLOCKS:
            item.setForeground(QColor("#9467bd"))
            f = QFont(); f.setItalic(True)
            item.setFont(f)
        return item

    def add_block(self, block: dict):
        self._blocks.append(block)
        self.addItem(self._make_item(block))
        self.setCurrentItem(self.item(self.count() - 1))
        self.sequence_changed.emit()

    def insert_block(self, row: int, block: dict):
        row = max(0, min(row, self.count()))
        self._blocks.insert(row, block)
        self.insertItem(row, self._make_item(block))
        self.setCurrentItem(self.item(row))
        self.sequence_changed.emit()

    def remove_selected(self):
        row = self.currentRow()
        if row >= 0:
            self._blocks.pop(row)
            self.takeItem(row)
            self.sequence_changed.emit()

    def get_blocks(self) -> list:
        return list(self._blocks)

    def refresh_labels(self):
        for i in range(self.count()):
            item = self.item(i)
            if i < len(self._blocks):
                item.setText(self._make_label(self._blocks[i]))

    def _make_label(self, block: dict) -> str:
        return _block_summary(block)

    def _on_selection(self, current, _prev):
        if current is None:
            self.block_selected.emit(None)
            return
        row = self.row(current)
        block = self._blocks[row] if 0 <= row < len(self._blocks) else None
        self.block_selected.emit(block)

    # ── External drag-drop (from Block Palette) ────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and event.mimeData().text() in BLOCK_DEFS:
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)  # internal reorder

    def dragMoveEvent(self, event):
        if event.mimeData().hasText() and event.mimeData().text() in BLOCK_DEFS:
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasText() and event.mimeData().text() in BLOCK_DEFS:
            btype = event.mimeData().text()
            target = self.itemAt(event.position().toPoint())
            row = self.row(target) if target else self.count()
            self.insert_block(row, _new_block(btype))
            event.acceptProposedAction()
        else:
            super().dropEvent(event)  # internal reorder

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.remove_selected()
        else:
            super().keyPressEvent(event)


# ── Block palette tree (with drag support) ────────────────────────────────────

class _PaletteTree(QTreeWidget):
    """Block palette that encodes the block type as mime text on drag-start."""

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if not item:
            return
        btype = item.data(0, Qt.ItemDataRole.UserRole)
        if not btype:
            return   # category header — not draggable
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(btype)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


# ── Device picker widget ───────────────────────────────────────────────────────

class DevicePickerWidget(QWidget):
    """Scrollable device list with a live selection-summary label.

    Replaces the fragile closure-based approach so that selection changes
    reliably propagate back to PropertyPanel via a proper Qt signal.
    """
    value_changed = pyqtSignal(str)   # emits comma-sep string (or single name)

    def __init__(self, value, multi: bool, devices: list, parent=None):
        super().__init__(parent)
        self._multi = multi
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._lw = QListWidget()
        self._lw.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection if multi
            else QAbstractItemView.SelectionMode.SingleSelection)
        self._lw.setMaximumHeight(90)
        self._lw.addItems(devices)

        current = ({x.strip() for x in str(value).split(",") if x.strip()}
                   if multi else ({str(value).strip()} if value else set()))
        for i in range(self._lw.count()):
            if self._lw.item(i).text() in current:
                self._lw.item(i).setSelected(True)

        self._summary = QLabel("None selected")
        self._summary.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")
        self._summary.setWordWrap(True)

        lay.addWidget(self._lw)
        lay.addWidget(self._summary)

        self._lw.itemSelectionChanged.connect(self._on_changed)
        self._on_changed()   # initialise summary without emitting (block not set yet)

    def _on_changed(self):
        sel = [self._lw.item(i).text() for i in range(self._lw.count())
               if self._lw.item(i).isSelected()]
        if sel:
            self._summary.setText("✓  " + ",   ".join(sel))
            self._summary.setStyleSheet(
                "color: #2ca02c; font-size: 11px; font-weight: bold; padding: 1px 2px;")
        else:
            self._summary.setText("None selected")
            self._summary.setStyleSheet(
                "color: #888; font-size: 11px; font-style: italic; padding: 1px 2px;")
        self.value_changed.emit(
            ", ".join(sel) if self._multi else (sel[0] if sel else ""))

    def get_value(self) -> str:
        sel = [self._lw.item(i).text() for i in range(self._lw.count())
               if self._lw.item(i).isSelected()]
        return ", ".join(sel) if self._multi else (sel[0] if sel else "")


# ── Property panel ─────────────────────────────────────────────────────────────

class PropertyPanel(QWidget):
    """Dynamic parameter form for the selected block."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._block     = None
        self._widgets   = {}   # param_name → widget, for direct value reads
        self._handlers  = []   # strong refs to lambdas so Qt doesn't GC them
        self._loading   = False
        self._devices   = []
        self._motors    = []
        self._detectors = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self._title = QLabel("Select a block to edit")
        self._title.setObjectName("section_title")
        lay.addWidget(self._title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._form.setHorizontalSpacing(12)
        self._form.setVerticalSpacing(6)
        scroll.setWidget(self._form_host)
        lay.addWidget(scroll, 1)

    def set_devices(self, devices: dict):
        devices = devices or {}
        self._devices   = sorted(devices.keys())
        self._motors    = sorted(k for k, v in devices.items()
                                 if isinstance(v, dict) and v.get("is_movable", False))
        self._detectors = sorted(k for k, v in devices.items()
                                 if isinstance(v, dict) and v.get("is_readable", False))
        if self._block:
            self.load_block(self._block)

    def load_block(self, block):
        self._block    = block
        self._widgets  = {}
        self._handlers = []   # drop old lambdas; keep strong refs to new ones
        self._loading  = True  # suppress changed() during widget init
        while self._form.rowCount():
            self._form.removeRow(0)

        if not block:
            self._loading = False
            self._title.setText("Select a block to edit")
            return

        defn = BLOCK_DEFS[block["type"]]
        self._title.setText(f"{defn['icon']}  {defn['label']}")

        for param in defn["params"]:
            name  = param["name"]
            ptype = param["type"]
            value = block["params"].get(name, param["default"])
            hint  = param.get("hint", "")
            wtype = param.get("widget", "")

            if ptype == "float":
                w = NoScrollDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(4)
                w.setSingleStep(0.1)
                # Connect BEFORE setValue so the initial value is written into
                # block params; store strong ref so the lambda isn't GC'd.
                handler = lambda v, n=name: self._update(n, v)
                self._handlers.append(handler)
                w.valueChanged.connect(handler)
                w.setValue(float(value))

            elif ptype == "int":
                w = NoScrollSpinBox()
                w.setRange(1, 1000000)
                handler = lambda v, n=name: self._update(n, v)
                self._handlers.append(handler)
                w.valueChanged.connect(handler)
                w.setValue(int(value))

            elif ptype == "bool":
                w = QCheckBox()
                checked = (value if isinstance(value, bool)
                           else str(value).lower() in ("true", "1", "yes"))
                handler = lambda v, n=name: self._update(n, bool(v))
                self._handlers.append(handler)
                w.stateChanged.connect(handler)
                w.setChecked(checked)

            elif wtype == "code":
                w = QPlainTextEdit()
                w.setPlainText(str(value))
                w.setMinimumHeight(80)
                w.setMaximumHeight(160)
                f2 = QFont("Courier New", 10)
                w.setFont(f2)
                handler = lambda n=name, ww=w: self._update(n, ww.toPlainText())
                self._handlers.append(handler)
                w.textChanged.connect(handler)
                block["params"][name] = w.toPlainText()

            elif wtype in ("device_single", "device_multi", "device_any") and self._devices:
                if wtype == "device_multi":
                    devs, multi = self._detectors or self._devices, True
                elif wtype == "device_single":
                    devs, multi = self._motors or self._devices, True
                else:  # device_any
                    devs, multi = self._devices, True
                picker = DevicePickerWidget(value, multi, devs)
                handler = lambda v, n=name: self._update(n, v)
                self._handlers.append(handler)
                picker.value_changed.connect(handler)
                # Flush initial selection into block params now — picker's
                # _on_changed() fired during __init__ before we connected.
                block["params"][name] = picker.get_value()
                w = picker

            else:
                w = QLineEdit(str(value))
                w.setPlaceholderText(hint)
                handler = lambda v, n=name: self._update(n, v)
                self._handlers.append(handler)
                w.textChanged.connect(handler)

            self._widgets[name] = w
            label = name.replace("_", " ").title() + ":"
            self._form.addRow(label, w)

        self._loading = False  # widget init done; user changes now emit changed()

    def sync_to_block(self):
        """Read each widget's current value directly into the active block params.

        Bypasses the value_changed signal so initial picker selections (set
        before the signal is connected) are always flushed before code gen.
        """
        if not self._block:
            return
        for name, w in self._widgets.items():
            if isinstance(w, DevicePickerWidget):
                self._block["params"][name] = w.get_value()
            elif isinstance(w, QDoubleSpinBox):
                self._block["params"][name] = w.value()
            elif isinstance(w, QSpinBox):
                self._block["params"][name] = w.value()
            elif isinstance(w, QCheckBox):
                self._block["params"][name] = w.isChecked()
            elif isinstance(w, QPlainTextEdit):
                self._block["params"][name] = w.toPlainText()
            elif isinstance(w, QLineEdit):
                self._block["params"][name] = w.text()

    def _update(self, name, value):
        if self._block:
            self._block["params"][name] = value
            if not self._loading:
                self.changed.emit()


# ── Code generation ────────────────────────────────────────────────────────────

def generate_plan_code(main_blocks: list, ps_blocks: list, plan_name: str = "") -> tuple:
    """Return (code_str, plan_name).

    Generates a fully parametric plan: scan/count blocks contribute
    detectors, motor, start/stop/num as function parameters; shutter and
    exposure blocks contribute shutter / exposure_time parameters.
    Block-Properties selections become the default values shown in the
    docstring and used at runtime when callers omit the argument.
    """
    import re
    name = re.sub(r"[^a-zA-Z0-9_]", "_", plan_name.strip()) if plan_name.strip() else ""
    if not name:
        name = "composed_plan"

    # ── 1. Collect parameterisable values from all blocks ─────────────────────
    # func_params: ordered dict  param_name → {ann, default, desc}
    func_params = {}

    def _first(val):
        """Return first comma-separated value (for single-device params)."""
        s = str(val).strip()
        return s.split(",")[0].strip() if "," in s else s

    def _add(pname, ann, default, desc):
        # For Movable params that may have multiple devices selected (the picker
        # is now multi-select), store the full comma-separated string in
        # 'all_defaults' for the docstring and only the first device as
        # 'default' for the runtime fallback assignment.
        if ann == "Movable":
            first = _first(default)
        else:
            first = default
        if pname not in func_params:
            func_params[pname] = {
                "ann": ann, "default": first,
                "all_defaults": str(default).strip(), "desc": desc,
            }
        elif not func_params[pname]["default"] and first:
            func_params[pname]["default"]     = first
            func_params[pname]["all_defaults"] = str(default).strip()

    for block in main_blocks + ps_blocks:
        btype, p = block["type"], block["params"]
        if btype == "scan":
            _add("detectors", "List[Readable]", p.get("detectors", ""), "Detectors to read")
            motor_val = p.get("motor", "")
            # Multi-motor: keep motor/start/stop hardcoded — they can't map to
            # a single typed parameter.  Single motor: parametrize all three.
            if "," not in str(motor_val):
                _add("motor", "Movable", motor_val, "Motor to scan")
                try:
                    _add("start", "float", float(str(p.get("start", "0.0")).split(",")[0]), "Scan start position")
                    _add("stop",  "float", float(str(p.get("stop",  "1.0")).split(",")[0]), "Scan stop position")
                except ValueError:
                    pass
            _add("num", "int", p.get("num", 11), "Number of scan points")
        elif btype == "count":
            _add("detectors", "List[Readable]", p.get("detectors", ""), "Detectors to read")
            _add("num",       "int",            p.get("num",    1),     "Number of acquisitions")
            _add("delay",     "float",          p.get("delay",  0.0),   "Delay between acquisitions (s)")
        elif btype == "rel_scan":
            _add("detectors", "List[Readable]", p.get("detectors", ""), "Detectors to read")
            motor_val = p.get("motor", "")
            if "," not in str(motor_val):
                _add("motor", "Movable", motor_val, "Motor to scan (relative)")
                try:
                    _add("start", "float", float(str(p.get("start", "-1.0")).split(",")[0]), "Relative start position")
                    _add("stop",  "float", float(str(p.get("stop",  "1.0")).split(",")[0]),  "Relative stop position")
                except ValueError:
                    pass
            _add("num", "int", p.get("num", 11), "Number of scan points")
        elif btype == "adaptive_scan":
            _add("detectors",    "List[Readable]", p.get("detectors",    ""),    "Detectors to read")
            motor_val = p.get("motor", "")
            if "," not in str(motor_val):
                _add("motor",        "Movable",        motor_val,               "Motor to scan")
                _add("start",        "float",          p.get("start",     0.0), "Scan start position")
                _add("stop",         "float",          p.get("stop",      1.0), "Scan stop position")
                _add("min_step",     "float",          p.get("min_step",  0.001),"Minimum step size")
                _add("max_step",     "float",          p.get("max_step",  0.1), "Maximum step size")
                _add("target_delta", "float",          p.get("target_delta", 0.05), "Target fractional change per step")
                _add("threshold",    "float",          p.get("threshold", 0.8), "Backstep threshold")
        elif btype == "grid_scan":
            _add("detectors", "List[Readable]", p.get("detectors", ""), "Detectors to read")
            # All grid axes are hardcoded (per-motor); only detectors parametrized
        elif btype == "list_scan":
            _add("detectors", "List[Readable]", p.get("detectors", ""), "Detectors to read")
            motor_val = p.get("motor", "")
            if "," not in str(motor_val):
                _add("motor", "Movable", motor_val, "Motor to scan")
        elif btype in ("set_exposure", "trigger_read"):
            _add("detectors", "List[Readable]", p.get("detectors", ""), "Detectors to read")
            if btype == "set_exposure":
                _add("exposure_time", "float", p.get("exposure_time", 1.0), "Exposure time (s)")
        elif btype in ("open_shutter", "close_shutter"):
            _add("shutter", "Movable", p.get("shutter", ""), "Shutter device")

    # param_map: block-level param name → variable name in generated code
    param_map = {k: k for k in func_params}

    # ── 2. Function signature ─────────────────────────────────────────────────
    sig_lines = []
    for pname, info in func_params.items():
        ann, d = info["ann"], info["default"]
        if ann in ("List[Readable]", "Movable"):
            sig_lines.append(f"        {pname}: {ann} = None,")
        elif ann == "float":
            sig_lines.append(f"        {pname}: float = {d},")
        elif ann == "int":
            sig_lines.append(f"        {pname}: int = {d},")
    if sig_lines:
        sig_lines[-1] = sig_lines[-1].rstrip(",")   # no trailing comma on last param

    # ── 3. Docstring ──────────────────────────────────────────────────────────
    # Detect repeat_n wrapper; filter it out of the actual code blocks
    repeat_block = next((b for b in main_blocks if b["type"] == "repeat_n"), None)
    repeat_count = int(repeat_block["params"].get("count", 1)) if repeat_block else None
    code_main_blocks = [b for b in main_blocks if b["type"] != "repeat_n"]

    seq_blocks = [b for b in code_main_blocks if b["type"] != "custom_python"]
    main_seq = " → ".join(BLOCK_DEFS[b["type"]]["label"] for b in seq_blocks)
    if repeat_count and repeat_count > 1:
        main_seq = f"[×{repeat_count}] {main_seq}"
    ps_seq   = (" → ".join(BLOCK_DEFS[b["type"]]["label"] for b in ps_blocks)
                if ps_blocks else "")

    doc = ['    """']
    doc.append(f"    Plan generated by EasyBluesky Visual Composer.")
    doc.append("")
    doc.append("    Sequence")
    doc.append("    --------")
    doc.append(f"    Main     : {main_seq}")
    if ps_seq:
        doc.append(f"    Per-step : {ps_seq}")
    if func_params:
        doc.append("")
        doc.append("    Parameters")
        doc.append("    ----------")
        for pname, info in func_params.items():
            d_runtime = info["default"]
            d_display = info.get("all_defaults", d_runtime)
            desc = info["desc"]
            show = d_display if (d_display and d_display != "0.0" and d_display != "0") else ""
            note = f"  Default selection: {show}." if show else ""
            doc.append(f"    {pname} : {info['ann']}")
            doc.append(f"        {desc}.{note}")
    doc.append('    """')

    # ── 4. Assemble code ──────────────────────────────────────────────────────
    need_protocols = any(v["ann"] in ("List[Readable]", "Movable")
                         for v in func_params.values())
    lines = []
    if need_protocols:
        lines += ["from typing import List",
                  "from bluesky.protocols import Readable, Movable",
                  "import bluesky.plans as bp",
                  "import bluesky.plan_stubs as bps",
                  ""]
    else:
        lines += ["import bluesky.plans as bp",
                  "import bluesky.plan_stubs as bps",
                  ""]

    if sig_lines:
        lines.append(f"def {name}(")
        lines.extend(sig_lines)
        lines.append("):")
    else:
        lines.append(f"def {name}():")

    lines.extend(doc)

    if not code_main_blocks:
        lines.append("    pass")
        return "\n".join(lines), name

    # Runtime defaults: device params use = None in signature; assign in body.
    device_lines = []
    for pname, info in func_params.items():
        ann, d = info["ann"], info["default"]
        if ann == "List[Readable]":
            device_lines.append(
                f"    {pname} = {pname} or [{d}]" if d
                else f"    if {pname} is None: {pname} = []"
            )
        elif ann == "Movable" and d:
            device_lines.append(f"    {pname} = {pname} or {d}")
    if device_lines:
        lines.extend(device_lines)
        lines.append("")

    # repeat_n: emit the for-loop header; body indented an extra 4 spaces
    body_indent = 4
    if repeat_count is not None and repeat_count > 1:
        lines.append(f"    for _i in range({repeat_count}):")
        body_indent = 8

    has_ps = bool(ps_blocks)
    for block in code_main_blocks:
        if block["type"] in _PLAN_BLOCKS and has_ps:
            ps_name = "_per_step"
            ps_indent = body_indent
            lines.append(f"{' ' * ps_indent}def {ps_name}(detectors, step, pos_cache):")
            lines.append(f"{' ' * (ps_indent + 4)}yield from bps.move_per_step(step, pos_cache)")
            for ps in ps_blocks:
                lines.append(_block_to_code(ps, indent=ps_indent + 4, param_map=param_map))
            lines.append("")
            lines.append(_block_to_code(block, indent=body_indent,
                                         per_step_name=ps_name,
                                         param_map=param_map))
        else:
            code_line = _block_to_code(block, indent=body_indent, param_map=param_map)
            if code_line:   # repeat_n returns "" — skip blank lines
                lines.append(code_line)

    return "\n".join(lines), name


# ── Composer widget ────────────────────────────────────────────────────────────

class ComposerWidget(QWidget):
    """Three-panel visual plan composer."""
    send_to_editor = pyqtSignal(str)   # emits generated code

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self._active_seq = None   # which sequence last had focus
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_palette())
        splitter.addWidget(self._build_sequences())
        splitter.addWidget(self._build_properties())
        splitter.setSizes([190, 440, 280])
        lay.addWidget(splitter)

        # Connect property panel (built last)
        self._main_seq.block_selected.connect(self._props.load_block)
        self._main_seq.block_selected.connect(lambda _: self._update_preview())
        self._main_seq.sequence_changed.connect(self._update_preview)
        self._perstep_seq.block_selected.connect(self._props.load_block)
        self._perstep_seq.block_selected.connect(lambda _: self._update_preview())
        self._perstep_seq.sequence_changed.connect(self._update_preview)
        self._props.changed.connect(self._on_prop_changed)

        # Track active sequence for palette "add" buttons
        self._main_seq.focusInEvent = lambda e: self._set_active(self._main_seq)
        self._perstep_seq.focusInEvent = lambda e: self._set_active(self._perstep_seq)
        self._active_seq = self._main_seq

    def _set_active(self, seq):
        self._active_seq = seq

    # ── Palette panel ──────────────────────────────────────────────────────────

    def _build_palette(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        lbl = QLabel("BLOCK PALETTE")
        lbl.setObjectName("section_title")
        lay.addWidget(lbl)

        self._palette = _PaletteTree()
        self._palette.setHeaderHidden(True)
        self._palette.setRootIsDecorated(True)
        self._palette.setDragEnabled(True)
        self._palette.itemDoubleClicked.connect(self._palette_double_clicked)

        cats = {}
        for btype, defn in BLOCK_DEFS.items():
            cats.setdefault(defn["category"], []).append((btype, defn))

        bold = QFont(); bold.setBold(True)
        for cat in _CATEGORY_ORDER:
            if cat not in cats:
                continue
            cat_item = QTreeWidgetItem([cat])
            cat_item.setFont(0, bold)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for btype, defn in cats[cat]:
                child = QTreeWidgetItem([f"{defn['icon']}  {defn['label']}"])
                child.setData(0, Qt.ItemDataRole.UserRole, btype)
                child.setToolTip(0, "Double-click or use buttons below to add")
                cat_item.addChild(child)
            self._palette.addTopLevelItem(cat_item)

        self._palette.expandAll()
        lay.addWidget(self._palette, 1)

        hint = QLabel(_WORKFLOW_HINT)
        hint.setObjectName("dim_text")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 10px; border: 1px solid #444; padding: 4px; border-radius: 3px;")
        lay.addWidget(hint)

        note = QLabel("Double-click or use buttons below:")
        note.setObjectName("dim_text")
        note.setStyleSheet("font-size: 11px;")
        lay.addWidget(note)

        btn_main = QPushButton("Add to Main ↑")
        btn_main.setToolTip("Add selected block to the Main Sequence")
        btn_main.clicked.connect(lambda: self._add_from_palette(self._main_seq))
        lay.addWidget(btn_main)

        btn_ps = QPushButton("Add to Per-Step ↓")
        btn_ps.setToolTip("Add selected block to the Per-Step Sequence")
        btn_ps.clicked.connect(lambda: self._add_from_palette(self._perstep_seq))
        lay.addWidget(btn_ps)

        return w

    def _palette_double_clicked(self, item, col):
        btype = item.data(0, Qt.ItemDataRole.UserRole)
        if btype and self._active_seq:
            self._active_seq.add_block(_new_block(btype))
            self._active_seq.setFocus()

    def _add_from_palette(self, target_seq: SequenceList):
        item = self._palette.currentItem()
        if not item:
            return
        btype = item.data(0, Qt.ItemDataRole.UserRole)
        if btype:
            target_seq.add_block(_new_block(btype))
            target_seq.setFocus()

    # ── Sequences panel ────────────────────────────────────────────────────────

    def _build_sequences(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Plan name field
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Plan name:"))
        self._plan_name_edit = QLineEdit("my_plan")
        self._plan_name_edit.setPlaceholderText("e.g. xrd_scan")
        self._plan_name_edit.textChanged.connect(self._update_preview)
        name_row.addWidget(self._plan_name_edit, 1)
        lay.addLayout(name_row)

        # Main sequence
        lbl_main = QLabel("MAIN SEQUENCE")
        lbl_main.setObjectName("section_title")
        lay.addWidget(lbl_main)

        hint_main = QLabel("Pre-steps, scan/count block, post-steps. Drag to reorder. Del to remove.")
        hint_main.setObjectName("dim_text")
        hint_main.setStyleSheet("font-size: 11px;")
        lay.addWidget(hint_main)

        self._main_seq = SequenceList()
        lay.addWidget(self._main_seq, 3)

        btn_del_main = QPushButton("Remove selected")
        btn_del_main.clicked.connect(self._main_seq.remove_selected)
        lay.addWidget(btn_del_main)

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #444;")
        lay.addWidget(line)

        # Per-step sequence
        lbl_ps = QLabel("PER-STEP SEQUENCE")
        lbl_ps.setObjectName("section_title")
        lay.addWidget(lbl_ps)

        hint_ps = QLabel("Injected at every point of the scan/count above.")
        hint_ps.setObjectName("dim_text")
        hint_ps.setStyleSheet("font-size: 11px;")
        lay.addWidget(hint_ps)

        self._perstep_seq = SequenceList()
        lay.addWidget(self._perstep_seq, 2)

        btn_del_ps = QPushButton("Remove selected")
        btn_del_ps.clicked.connect(self._perstep_seq.remove_selected)
        lay.addWidget(btn_del_ps)

        # Divider
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("color: #444;")
        lay.addWidget(line2)

        # Code preview
        lbl_prev = QLabel("GENERATED CODE")
        lbl_prev.setObjectName("section_title")
        lay.addWidget(lbl_prev)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMaximumHeight(130)
        f = QFont("Courier New", 9)
        self._preview.setFont(f)
        lay.addWidget(self._preview)

        btn_send = QPushButton("→   Send to Code Editor")
        btn_send.setStyleSheet(
            "QPushButton { background: #1f77b4; color: white; "
            "font-weight: bold; padding: 6px; border-radius: 4px; }"
            "QPushButton:hover { background: #2a8fd4; }"
        )
        btn_send.clicked.connect(self._on_send_to_editor)
        lay.addWidget(btn_send)

        return w

    # ── Properties panel ───────────────────────────────────────────────────────

    def _build_properties(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("BLOCK PROPERTIES")
        lbl.setObjectName("section_title")
        lbl.setContentsMargins(8, 8, 8, 4)
        lay.addWidget(lbl)

        self._props = PropertyPanel()
        lay.addWidget(self._props, 1)

        return w

    # ── Updates ────────────────────────────────────────────────────────────────

    def _on_prop_changed(self):
        self._main_seq.refresh_labels()
        self._perstep_seq.refresh_labels()
        self._update_preview()

    def _update_preview(self):
        code, _ = generate_plan_code(
            self._main_seq.get_blocks(),
            self._perstep_seq.get_blocks(),
            self._plan_name_edit.text(),
        )
        self._preview.setPlainText(code)

    # ── Send to editor ─────────────────────────────────────────────────────────

    def _on_send_to_editor(self):
        main_blocks = self._main_seq.get_blocks()
        if not main_blocks:
            QMessageBox.warning(self, "Empty", "Add at least one block to the main sequence.")
            return
        code, _ = generate_plan_code(
            main_blocks, self._perstep_seq.get_blocks(), self._plan_name_edit.text()
        )
        self.send_to_editor.emit(code)

    def set_devices(self, devices: dict):
        self._props.set_devices(devices)

    def set_plans(self, plans: dict):
        pass


# ── Main PlanBuilder widget (two tabs) ─────────────────────────────────────────

# ── Plan file tree (drag-and-drop aware) ──────────────────────────────────────

class _PlanFileTree(QTreeWidget):
    """QTreeWidget that accepts folder drops from the OS file manager."""
    folder_dropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if Path(url.toLocalFile()).is_dir():
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).is_dir():
                self.folder_dropped.emit(p)
        event.acceptProposedAction()


# ── SFTP background threads ────────────────────────────────────────────────────

class _RemoteFileLister(QThread):
    result = pyqtSignal(list)
    error  = pyqtSignal(str)

    def __init__(self, conn_settings, profile, parent=None):
        super().__init__(parent)
        self._cs, self._pr = conn_settings, profile

    def run(self):
        from .ssh_manager import list_remote_plan_files
        try:
            self.result.emit(list_remote_plan_files(self._cs, self._pr))
        except Exception as e:
            self.error.emit(str(e))


class _RemoteFileReader(QThread):
    result = pyqtSignal(str, str)   # (filename, content)
    error  = pyqtSignal(str, str)   # (filename, msg)

    def __init__(self, conn_settings, profile, filename, parent=None):
        super().__init__(parent)
        self._cs, self._pr, self._fn = conn_settings, profile, filename

    def run(self):
        from .ssh_manager import read_remote_plan_file
        try:
            self.result.emit(self._fn, read_remote_plan_file(self._cs, self._pr, self._fn))
        except Exception as e:
            self.error.emit(self._fn, str(e))


class _RemoteFileSaver(QThread):
    done = pyqtSignal(bool, str)   # (success, message)

    def __init__(self, conn_settings, profile, filename, content, parent=None):
        super().__init__(parent)
        self._cs, self._pr = conn_settings, profile
        self._fn, self._content = filename, content

    def run(self):
        from .ssh_manager import write_remote_plan_file
        ok, msg = write_remote_plan_file(self._cs, self._pr, self._fn, self._content)
        self.done.emit(ok, msg)


# ── PlanFileTreePanel ─────────────────────────────────────────────────────────

class PlanFileTreePanel(QWidget):
    """Reusable file-tree panel for browsing remote and local plan .py files.

    Emits ``file_open_requested(tier, name_or_path)`` when the user clicks a
    file.  The caller decides what to do with it (open in editor, switch tabs,
    etc.).  ``output_message(str)`` carries status/error text for the caller to
    display however it likes.
    """

    file_open_requested = pyqtSignal(str, str)   # (tier, name_or_path)
    output_message      = pyqtSignal(str)
    local_plans_added   = pyqtSignal()           # folder added — upload immediately
    local_plans_removed = pyqtSignal(str)        # folder removed (path) — env restart needed

    def __init__(self, show_new_remote_btn: bool = True, parent=None):
        super().__init__(parent)
        self._conn_settings:  dict = {}
        self._profile_name:   str  = ""
        self._active_threads: list = []
        self._show_new_remote = show_new_remote_btn
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lbl = QLabel("PLAN FILES")
        lbl.setObjectName("section_title")
        lay.addWidget(lbl)

        self._tree = _PlanFileTree()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.folder_dropped.connect(self._do_add_folder)
        lay.addWidget(self._tree, 1)

        btns = QHBoxLayout()
        btn_add = QPushButton("＋ Add Folder")
        btn_add.clicked.connect(self._on_add_folder_clicked)
        btns.addWidget(btn_add)
        if self._show_new_remote:
            btn_new = QPushButton("＋ Remote File")
            btn_new.clicked.connect(self._on_new_remote_file)
            btns.addWidget(btn_new)
        lay.addLayout(btns)

    # ── profile ────────────────────────────────────────────────────────────────

    def set_profile(self, conn_settings: dict) -> None:
        from .connection_settings import get_active_profile
        self._conn_settings = conn_settings
        self._profile_name  = get_active_profile(conn_settings).get("name", "")
        self._refresh()

    def clear_profile(self) -> None:
        self._conn_settings = {}
        self._profile_name  = ""
        self._tree.clear()

    def _refresh(self) -> None:
        from .plans_manager import get_user_dirs
        self._tree.clear()
        bold = QFont()
        bold.setBold(True)

        self._remote_header = QTreeWidgetItem(
            [f"📁 {self._profile_name} Plans  (remote)"])
        self._remote_header.setFont(0, bold)
        self._remote_header.setData(
            0, Qt.ItemDataRole.UserRole, {"tier": "header_remote"})
        self._tree.addTopLevelItem(self._remote_header)
        self._remote_header.setExpanded(True)

        ph = QTreeWidgetItem(["  ⏳ Loading…"])
        ph.setFlags(ph.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._remote_header.addChild(ph)

        if self._conn_settings:
            self._start_remote_list()

        for d in get_user_dirs(self._profile_name):
            self._add_local_dir(d)

    def _start_remote_list(self) -> None:
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileLister(self._conn_settings, profile, self)
        t.result.connect(self._on_remote_listed)
        t.error.connect(self._on_remote_error)
        t.start()
        self._active_threads.append(t)

    def _on_remote_listed(self, files: list) -> None:
        self._remote_header.takeChildren()
        self._remote_header.setText(
            0, f"📁 {self._profile_name} Plans  (remote, {len(files)} files)")
        if not files:
            empty = QTreeWidgetItem(["  (empty — use ＋ Remote File to create)"])
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._remote_header.addChild(empty)
            return
        for fn in files:
            item = QTreeWidgetItem([f"  {fn}"])
            item.setData(0, Qt.ItemDataRole.UserRole, {"tier": "remote", "name": fn})
            self._remote_header.addChild(item)

    def _on_remote_error(self, msg: str) -> None:
        self._remote_header.takeChildren()
        err = QTreeWidgetItem([f"  ⚠ {msg}"])
        err.setFlags(err.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._remote_header.addChild(err)

    def _add_local_dir(self, dir_path: str) -> None:
        bold = QFont()
        bold.setBold(True)
        p = Path(dir_path)
        header = QTreeWidgetItem([f"📂 {p.name}  ({dir_path})"])
        header.setFont(0, bold)
        header.setData(0, Qt.ItemDataRole.UserRole,
                       {"tier": "header_local", "path": dir_path})
        self._tree.addTopLevelItem(header)
        header.setExpanded(True)
        py_files = sorted(p.glob("*.py")) if p.is_dir() else []
        if not py_files:
            empty = QTreeWidgetItem(["  (no .py files)"])
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            header.addChild(empty)
        for f in py_files:
            item = QTreeWidgetItem([f"  {f.name}"])
            item.setData(0, Qt.ItemDataRole.UserRole,
                         {"tier": "local", "name": f.name, "path": str(f)})
            header.addChild(item)

    # ── interactions ───────────────────────────────────────────────────────────

    def _on_item_clicked(self, item, _col) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        tier = data.get("tier", "")
        if tier == "remote":
            self.file_open_requested.emit("remote", data["name"])
        elif tier == "local":
            self.file_open_requested.emit("local", data["path"])

    def _on_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        tier = data.get("tier", "")
        menu = QMenu(self)
        if tier == "remote":
            menu.addAction("Delete from remote",
                           lambda: self._delete_remote_file(data["name"]))
        elif tier == "local":
            menu.addAction("Delete file",
                           lambda: self._delete_local_file(data["path"]))
        elif tier == "header_local":
            menu.addAction("Remove folder from list",
                           lambda: self._remove_local_dir(data["path"]))
        if not menu.isEmpty():
            menu.exec(self._tree.mapToGlobal(pos))

    def _delete_remote_file(self, filename: str) -> None:
        from .connection_settings import get_active_profile
        from .ssh_manager import delete_remote_plan_file
        r = QMessageBox.question(
            self, "Delete File",
            f"Delete '{filename}' from the remote plans directory?")
        if r != QMessageBox.StandardButton.Yes:
            return
        profile = get_active_profile(self._conn_settings)
        ok, msg = delete_remote_plan_file(self._conn_settings, profile, filename)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output_message.emit(f"[{ts}] {'✓' if ok else '✗'} {msg}")
        if ok:
            self._start_remote_list()

    def _remove_local_dir(self, dir_path: str) -> None:
        from .plans_manager import remove_user_dir
        remove_user_dir(self._profile_name, dir_path)
        self._refresh()
        ts = datetime.now().strftime("%H:%M:%S")
        self.output_message.emit(
            f"[{ts}] Removed folder: {dir_path}\n"
            f"  Plans from this folder remain in Available Plans until you "
            f"Close Env → Open Env.")
        self.local_plans_removed.emit(dir_path)

    def _delete_local_file(self, file_path: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        name = Path(file_path).name
        if QMessageBox.question(
                self, "Delete file",
                f"Permanently delete '{name}' from disk?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            Path(file_path).unlink()
        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S")
            self.output_message.emit(f"[{ts}] Could not delete {name}: {e}")
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.output_message.emit(f"[{ts}] Deleted: {file_path}")
        self._refresh()
        self.local_plans_removed.emit(str(Path(file_path).parent))

    def _on_add_folder_clicked(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Plan Folder")
        if path:
            self._do_add_folder(path)

    def _do_add_folder(self, path: str) -> None:
        from .plans_manager import add_user_dir
        if add_user_dir(self._profile_name, path):
            self._add_local_dir(path)
            ts = datetime.now().strftime("%H:%M:%S")
            self.output_message.emit(f"[{ts}] Added folder: {path}")
            self.local_plans_added.emit()
        else:
            self.output_message.emit(f"  Folder already in list: {path}")

    def _on_new_remote_file(self) -> None:
        if not self._conn_settings or not self._profile_name:
            QMessageBox.warning(self, "Not Connected", "Connect to a profile first.")
            return
        name, ok = QInputDialog.getText(
            self, "New Remote Plan File", "Filename (e.g. my_plan.py):")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not name.endswith(".py"):
            name += ".py"
        from .connection_settings import get_active_profile
        from .ssh_manager import write_remote_plan_file
        profile = get_active_profile(self._conn_settings)
        ok2, msg = write_remote_plan_file(
            self._conn_settings, profile, name,
            f"# {name}\nimport bluesky.plans as bp\nimport bluesky.plan_stubs as bps\n\n\n"
        )
        ts = datetime.now().strftime("%H:%M:%S")
        if ok2:
            self.output_message.emit(f"[{ts}] ✓ Created {name}")
            self._start_remote_list()
            self.file_open_requested.emit("remote", name)
        else:
            self.output_message.emit(f"[{ts}] ✗ {msg}")


# ── PlanBuilder ────────────────────────────────────────────────────────────────

class PlanBuilder(QWidget):
    plans_uploading = pyqtSignal(str)   # emitted when local plan upload starts (msg)

    def __init__(self, worker=None, parent=None):
        super().__init__(parent)
        self.worker  = worker
        self.plans   = {}
        self.devices = {}
        self._env_reload_attempts = 0
        self._conn_settings: dict = {}
        self._profile_name:  str  = ""
        self._file_tier:     str  = ""
        self._file_name:     str  = ""
        self._dirty:         bool = False
        self._active_threads: list = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        tabs.setTabPosition(QTabWidget.TabPosition.North)

        # Tab 1: Visual Composer
        self._composer = ComposerWidget(worker=self.worker)
        tabs.addTab(self._composer, "🎛  Visual Composer")

        # Tab 2: Code Editor (kept for advanced use)
        self._editor_tab_index = tabs.count()
        tabs.addTab(self._build_code_editor(), "📝  Code Editor")

        self._tabs = tabs
        self._composer.send_to_editor.connect(self._on_send_to_editor)

        lay.addWidget(tabs)

    # ── Code editor tab (unchanged from original) ──────────────────────────────

    def _build_code_editor(self) -> QWidget:
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(8, 8, 8, 8)
        outer_lay.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: editor pane ──────────────────────────────────────────────
        editor_pane = QWidget()
        lay = QVBoxLayout(editor_pane)
        lay.setContentsMargins(0, 0, 4, 0)

        title_row = QHBoxLayout()
        lbl = QLabel("CODE EDITOR")
        lbl.setObjectName("section_title")
        title_row.addWidget(lbl)
        title_row.addStretch()
        self._editor_file_lbl = QLabel("")
        self._editor_file_lbl.setStyleSheet("font-size: 11px; color: #888;")
        title_row.addWidget(self._editor_file_lbl)
        lay.addLayout(title_row)

        self.editor = CodeEditor()
        self.editor.setFont(QFont("Courier New", 13))
        self.editor.setPlaceholderText(
            "# Write a custom plan here — it will be uploaded to the RE Manager\n\n"
            "def my_plan(detector, motor, start, stop, num):\n"
            "    yield from bp.scan([detector], motor, start, stop, num)\n"
        )
        self.highlighter = PythonHighlighter(self.editor.document())
        self.editor.document().contentsChanged.connect(self._on_editor_changed)
        lay.addWidget(self.editor, 1)

        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self.tmpl_combo = QComboBox()
        self.tmpl_combo.addItems([
            "-- select --",
            "Simple scan",
            "Multi-motor scan",
            "Scan with per-step",
            "Scan with shutter + AD",
            "Grid scan",
            "Count",
            "Move and count",
            "Custom loop",
        ])
        self.tmpl_combo.currentTextChanged.connect(self._insert_template)
        tmpl_row.addWidget(self.tmpl_combo, 1)
        lay.addLayout(tmpl_row)

        e_btns = QHBoxLayout()
        btn_open   = QPushButton("📂  Open file")
        btn_save   = QPushButton("💾  Save")
        btn_check  = QPushButton("✔  Check")
        btn_check.setToolTip("Check for syntax errors, undefined names, and missing imports")
        btn_upload = QPushButton("⬆  Upload to RE Manager")
        btn_reload = QPushButton("↺  Reload RE env")
        btn_open.clicked.connect(self._open_script)
        btn_save.clicked.connect(self._save_script)
        btn_check.clicked.connect(self._check_plan)
        btn_upload.clicked.connect(self._upload_script)
        btn_reload.clicked.connect(self._reload_environment)
        e_btns.addWidget(btn_open)
        e_btns.addWidget(btn_save)
        e_btns.addWidget(btn_check)
        e_btns.addWidget(btn_upload)
        e_btns.addWidget(btn_reload)
        lay.addLayout(e_btns)

        lbl_out = QLabel("OUTPUT")
        lbl_out.setObjectName("section_title")
        lay.addWidget(lbl_out)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(100)
        self.output.setFont(QFont("Courier New", 11))
        lay.addWidget(self.output)

        splitter.addWidget(editor_pane)

        # ── Right: file tree pane ──────────────────────────────────────────
        self._file_panel = PlanFileTreePanel(show_new_remote_btn=True)
        self._file_panel.file_open_requested.connect(self._on_file_panel_open)
        self._file_panel.output_message.connect(self.output.appendPlainText)
        self._file_panel.local_plans_added.connect(self.reupload_local_plans)
        self._file_panel.local_plans_removed.connect(self._on_local_plans_removed)
        splitter.addWidget(self._file_panel)
        splitter.setSizes([620, 260])

        outer_lay.addWidget(splitter, 1)
        return outer

    # ── Code checking ──────────────────────────────────────────────────────────

    def _syntax_check(self, content: str) -> tuple:
        try:
            compile(content, "<plan>", "exec")
        except SyntaxError as e:
            return False, f"Syntax error on line {e.lineno}: {e.msg}", e.lineno or 0
        return True, "Syntax OK", 0

    def _import_check(self, content: str) -> list:
        import ast
        import importlib.util
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        missing = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            for mod in mods:
                try:
                    if importlib.util.find_spec(mod) is None:
                        missing.add(mod)
                except (ValueError, ModuleNotFoundError):
                    missing.add(mod)
        return sorted(missing)

    def _pyflakes_check(self, content: str) -> list:
        try:
            import io
            from pyflakes import api as _pf_api
            from pyflakes import reporter as _pf_reporter
        except ImportError:
            return []
        buf = io.StringIO()
        r = _pf_reporter.Reporter(buf, buf)
        _pf_api.check(content, "<plan>", reporter=r)
        lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
        clean = []
        for ln in lines:
            if ":" in ln:
                parts = ln.split(":", 3)
                if len(parts) >= 4:
                    clean.append(f"line {parts[1]}: {parts[3].strip()}")
                    continue
            clean.append(ln)
        return clean

    def _jump_to_line(self, lineno: int):
        if lineno < 1:
            return
        block = self.editor.document().findBlockByLineNumber(lineno - 1)
        if block.isValid():
            cursor = self.editor.textCursor()
            cursor.setPosition(block.position())
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()

    def _check_plan(self) -> bool:
        """Run all checks; log results to output. Returns True if syntax is valid."""
        content = self.editor.toPlainText()
        ts = datetime.now().strftime("%H:%M:%S")

        ok, msg, lineno = self._syntax_check(content)
        if not ok:
            self._jump_to_line(lineno)
            self.output.appendPlainText(f"[{ts}] ✗ {msg}")
            return False

        lines = ["✓ Syntax OK"]

        pf_issues = self._pyflakes_check(content)
        if pf_issues:
            for issue in pf_issues[:5]:
                lines.append(f"       ⚠ {issue}")
            if len(pf_issues) > 5:
                lines.append(f"       … +{len(pf_issues) - 5} more issue(s)")
        else:
            lines.append("       (no undefined names or unused imports)")

        missing = self._import_check(content)
        if missing:
            lines.append(
                f"       ⚠ Modules not found locally (may be fine on RE machine): "
                f"{', '.join(missing)}"
            )

        self.output.appendPlainText(f"[{ts}] " + "\n".join(lines))
        return True

    def _insert_template(self, name):
        _ANN = (
            "from typing import List\n"
            "from bluesky.protocols import Readable, Movable\n"
        )
        templates = {
            "Simple scan": (
                _ANN + "\n"
                "def my_scan(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        start: float,\n"
                "        stop: float,\n"
                "        num: int):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.scan(detectors, motor, start, stop, num)\n"
            ),
            "Multi-motor scan": (
                _ANN + "\n"
                "def multi_motor_scan(\n"
                "        detectors: List[Readable],\n"
                "        motor1: Movable, start1: float, stop1: float,\n"
                "        motor2: Movable, start2: float, stop2: float,\n"
                "        num: int):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.scan(\n"
                "        detectors,\n"
                "        motor1, start1, stop1,\n"
                "        motor2, start2, stop2,\n"
                "        num)\n"
            ),
            "Grid scan": (
                _ANN + "\n"
                "def my_grid_scan(\n"
                "        detectors: List[Readable],\n"
                "        motor1: Movable, start1: float, stop1: float, num1: int,\n"
                "        motor2: Movable, start2: float, stop2: float, num2: int,\n"
                "        snake_axes: bool = False):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.grid_scan(\n"
                "        detectors,\n"
                "        motor1, start1, stop1, num1,\n"
                "        motor2, start2, stop2, num2,\n"
                "        snake_axes=snake_axes)\n"
            ),
            "Count": (
                _ANN + "\n"
                "def my_count(\n"
                "        detectors: List[Readable],\n"
                "        num: int = 10,\n"
                "        delay: float = 0.1):\n"
                "    import bluesky.plans as bp\n"
                "    yield from bp.count(detectors, num=num, delay=delay)\n"
            ),
            "Move and count": (
                _ANN + "\n"
                "def move_and_count(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        position: float,\n"
                "        num: int = 5):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n"
                "    yield from bps.mv(motor, position)\n"
                "    yield from bp.count(detectors, num=num)\n"
            ),
            "Scan with per-step": (
                _ANN + "\n"
                "def scan_with_per_step(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        start: float,\n"
                "        stop: float,\n"
                "        num: int,\n"
                "        exposure_time: float = 1.0,\n"
                "        sleep_time: float = 0.0):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n\n"
                "    for det in detectors:\n"
                "        yield from bps.mv(det.cam.acquire_time, exposure_time)\n\n"
                "    def _per_step(detectors, step, pos_cache):\n"
                "        yield from bps.move_per_step(step, pos_cache)\n"
                "        yield from bps.trigger_and_read(detectors)\n"
                "        if sleep_time > 0:\n"
                "            yield from bps.sleep(sleep_time)\n\n"
                "    yield from bp.scan(\n"
                "        detectors, motor, start, stop, num,\n"
                "        per_step=_per_step)\n"
            ),
            "Scan with shutter + AD": (
                _ANN + "\n"
                "def scan_with_shutter_ad(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        start: float,\n"
                "        stop: float,\n"
                "        num: int,\n"
                "        shutter: Movable,\n"
                "        file_path: str = '/data/',\n"
                "        file_name: str = 'scan',\n"
                "        exposure_time: float = 1.0,\n"
                "        sleep_time: float = 0.5):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n\n"
                "    for det in detectors:\n"
                "        yield from bps.mv(det.cam.acquire_time, exposure_time)\n"
                "        yield from bps.abs_set(det.hdf1.file_path, file_path, wait=True)\n"
                "        yield from bps.abs_set(det.hdf1.file_name, file_name, wait=True)\n\n"
                "    def _per_step(detectors, step, pos_cache):\n"
                "        yield from bps.move_per_step(step, pos_cache)\n"
                "        yield from bps.mv(shutter, 'open')\n"
                "        yield from bps.trigger_and_read(detectors)\n"
                "        yield from bps.mv(shutter, 'closed')\n"
                "        yield from bps.sleep(sleep_time)\n\n"
                "    yield from bp.scan(\n"
                "        detectors, motor, start, stop, num,\n"
                "        per_step=_per_step)\n"
            ),
            "Custom loop": (
                _ANN + "\n"
                "def custom_loop(\n"
                "        detectors: List[Readable],\n"
                "        motor: Movable,\n"
                "        positions: List[float],\n"
                "        num: int = 3):\n"
                "    import bluesky.plans as bp\n"
                "    import bluesky.plan_stubs as bps\n"
                "    for pos in positions:\n"
                "        yield from bps.mv(motor, pos)\n"
                "        yield from bp.count(detectors, num=num)\n"
            ),
        }
        if name in templates:
            self.editor.setPlainText(templates[name])
            self.tmpl_combo.setCurrentIndex(0)

    def _upload_script(self):
        script = self.editor.toPlainText().strip()
        if not script:
            QMessageBox.warning(self, "Empty", "Write a plan before uploading.")
            return
        ok_syntax, err_msg, lineno = self._syntax_check(self.editor.toPlainText())
        if not ok_syntax:
            self._jump_to_line(lineno)
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] ✗ Upload blocked — {err_msg}")
            return

        ts = datetime.now().strftime("%H:%M:%S")
        if not self.worker or not self.worker.rm:
            self.output.appendPlainText(f"[{ts}] ✗ Not connected to RE Manager")
            return

        self.output.appendPlainText(f"[{ts}] Uploading to RE Manager…")
        ok, msg = self.worker.upload_script(script)
        ts = datetime.now().strftime("%H:%M:%S")
        if ok:
            self.output.appendPlainText(
                f"[{ts}] ✓ Plan injected into running session\n"
                f"  (session-only — save to a remote file to persist across restarts)")
            QTimer.singleShot(500, self.worker.reload_plans_devices)
        else:
            self.output.appendPlainText(f"[{ts}] ✗ Upload failed:")
            for line in (msg or "unknown error").splitlines():
                self.output.appendPlainText(f"  {line}")

    def _open_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Plan", str(Path.home()), "Python files (*.py);;All files (*)")
        if path:
            self._open_local_file(path)

    def _save_script(self):
        ts = datetime.now().strftime("%H:%M:%S")
        if self._file_tier == "remote" and self._file_name:
            self._save_remote(self.editor.toPlainText())
        elif self._file_tier == "local" and self._file_name:
            self._save_local(self.editor.toPlainText())
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Plan", str(Path.home()), "Python files (*.py)")
            if path:
                try:
                    Path(path).write_text(self.editor.toPlainText(), encoding="utf-8")
                    self._file_tier = "local"
                    self._file_name = path
                    self._dirty = False
                    self._editor_file_lbl.setText(Path(path).name)
                    self.output.appendPlainText(f"[{ts}] ✓ Saved to {path}")
                except Exception as e:
                    self.output.appendPlainText(f"[{ts}] ✗ Save failed: {e}")

    def _reload_environment(self):
        r = QMessageBox.question(
            self, "Reload Environment",
            "Close and reopen the RE environment?\n"
            "This reloads all startup scripts including your uploaded plan.")
        if r == QMessageBox.StandardButton.Yes:
            self.worker.close_environment()
            QTimer.singleShot(2000, self.worker.open_environment)
            self.output.appendPlainText("Reloading environment…")
            self._env_reload_attempts = 0
            QTimer.singleShot(4000, self._poll_for_env_ready)

    def _poll_for_env_ready(self):
        """Check RE status after reload; refresh plans once environment is idle."""
        self._env_reload_attempts += 1
        try:
            status = self.worker.rm.status() if self.worker.rm else {}
            env_state = status.get("worker_environment_state", "")
            if env_state == "idle":
                self.worker.reload_plans_devices()
                ts = datetime.now().strftime("%H:%M:%S")
                self.output.appendPlainText(f"[{ts}] ✓ Environment ready — plan list refreshed")
                return
        except Exception:
            pass
        # Retry up to 15 times (every 2 s → up to 30 s total)
        if self._env_reload_attempts < 15:
            QTimer.singleShot(2000, self._poll_for_env_ready)
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] ⚠ Environment not ready after 30 s — click Reconnect to refresh plans")

    def _on_send_to_editor(self, code: str):
        self.editor.setPlainText(code)
        self._tabs.setCurrentIndex(self._editor_tab_index)

    # ── File tree ──────────────────────────────────────────────────────────────

    def set_profile(self, conn_settings: dict) -> None:
        from .connection_settings import get_active_profile
        self._conn_settings = conn_settings
        self._profile_name  = get_active_profile(conn_settings).get("name", "")
        self._file_panel.set_profile(conn_settings)

    def clear_profile(self) -> None:
        self._conn_settings = {}
        self._profile_name  = ""
        self._file_panel.clear_profile()

    def _on_file_panel_open(self, tier: str, name_or_path: str) -> None:
        """Check unsaved state then open the file in the code editor."""
        if self._dirty:
            label = (Path(name_or_path).name if tier == "local" else name_or_path)
            r = QMessageBox.question(
                self, "Unsaved Changes",
                f"Discard unsaved changes to '{self._file_name or 'buffer'}' "
                f"and open '{label}'?")
            if r != QMessageBox.StandardButton.Yes:
                return
        if tier == "remote":
            self._open_remote_file(name_or_path)
        else:
            self._open_local_file(name_or_path)

    def open_file(self, tier: str, name_or_path: str) -> None:
        """Open a plan file from an external widget; switches to the code editor."""
        self._tabs.setCurrentIndex(self._editor_tab_index)
        self._on_file_panel_open(tier, name_or_path)

    def _open_remote_file(self, filename: str) -> None:
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileReader(self._conn_settings, profile, filename, self)
        t.result.connect(self._on_remote_file_read)
        t.error.connect(self._on_remote_file_error)
        t.start()
        self._active_threads.append(t)
        self.output.appendPlainText(f"  Loading {filename}…")

    def _on_remote_file_read(self, filename: str, content: str) -> None:
        self.editor.setPlainText(content)
        self._file_tier = "remote"
        self._file_name = filename
        self._dirty = False
        self._editor_file_lbl.setText(filename)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] Opened remote:{filename}")

    def _on_remote_file_error(self, filename: str, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] ✗ Cannot open {filename}: {msg}")

    def _open_local_file(self, path: str) -> None:
        try:
            content = Path(path).read_text(encoding="utf-8")
            self.editor.setPlainText(content)
            self._file_tier = "local"
            self._file_name = path
            self._dirty = False
            self._editor_file_lbl.setText(Path(path).name)
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] Opened {path}")
        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(f"[{ts}] ✗ Cannot open {path}: {e}")

    def _on_editor_changed(self) -> None:
        if not self._dirty:
            self._dirty = True
            base = Path(self._file_name).name if self._file_name else ""
            self._editor_file_lbl.setText((base + " *") if base else "*")

    def _save_remote(self, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] Saving {self._file_name} to remote…")
        from .connection_settings import get_active_profile
        profile = get_active_profile(self._conn_settings)
        t = _RemoteFileSaver(self._conn_settings, profile,
                             self._file_name, content, self)
        t.done.connect(self._on_remote_save_done)
        t.start()
        self._active_threads.append(t)

    def _on_remote_save_done(self, ok: bool, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] {'✓' if ok else '✗'} {msg}")
        if ok:
            self._dirty = False
            self._editor_file_lbl.setText(self._file_name)
            self._file_panel._start_remote_list()
            self.worker.close_environment()
            self.output.appendPlainText(f"[{ts}] ↻ Reloading RE environment…")
            self._env_reload_attempts = 0
            QTimer.singleShot(2000, self.worker.open_environment)
            QTimer.singleShot(4000, self._poll_for_env_ready)

    def _save_local(self, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            Path(self._file_name).write_text(content, encoding="utf-8")
            self._dirty = False
            self._editor_file_lbl.setText(Path(self._file_name).name)
            self.output.appendPlainText(f"[{ts}] ✓ Saved to {self._file_name}")
        except Exception as e:
            self.output.appendPlainText(f"[{ts}] ✗ Save failed: {e}")
            return
        if self.worker and self.worker.rm:
            ok, msg = self.worker.upload_script(content)
            self.output.appendPlainText(
                f"[{ts}] {'✓ Uploaded' if ok else '✗ Upload failed'}: {msg}")

    def reupload_local_plans(self, _attempt: int = 0) -> None:
        """Re-upload all local user-dir plan files after an env restart.

        Connected to worker.env_opened and local_plans_added.  Retries up to
        3 times (2 s apart) when the RE Manager is temporarily busy (e.g.
        fetch_device_pvnames running function_execute in parallel).
        """
        if not self.worker or not self.worker.rm:
            return
        from .plans_manager import get_user_dirs, get_catalog, PLAN_TYPE_SESSION
        scripts = []
        paths   = []
        for d in get_user_dirs(self._profile_name):
            p = Path(d)
            if p.is_dir():
                for f in sorted(p.glob("*.py")):
                    try:
                        code = f.read_text(encoding="utf-8")
                        scripts.append(code)
                        paths.append((str(f), code))
                    except Exception:
                        pass
        if not scripts:
            return
        if _attempt == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            n = len(scripts)
            self.output.appendPlainText(f"[{ts}] ↻ Uploading {n} local plan file(s)…")
            self.plans_uploading.emit(
                f"uploading {n} plan file{'s' if n > 1 else ''}")
        results = self.worker.upload_scripts(scripts)
        catalog = get_catalog()
        n_ok    = 0
        busy    = False
        _TRANSIENT = ("not idle", "environment is not open", "executing_task",
                      "must be in idle")
        for (path, code), (ok, msg) in zip(paths, results):
            fname = Path(path).name
            if ok:
                n_ok += 1
                self.output.appendPlainText(f"  ✓ {fname}")
                if catalog:
                    catalog.register_code(code, path, PLAN_TYPE_SESSION)
            elif any(t in msg.lower() for t in _TRANSIENT):
                busy = True   # transient — manager was mid-task; retry shortly
            elif msg != "skipped":
                self.output.appendPlainText(f"  ✗ {fname}: {msg}")

        if n_ok:
            # script_upload is synchronous — by the time we reach here the RE
            # Manager is already back to idle.  Reload the plan list immediately
            # so the new plans appear without waiting for the next poll cycle.
            self.worker.reload_plans_devices()

        if busy and _attempt < 3:
            delay = (1 + _attempt) * 2000   # 2 s, 4 s, 6 s
            ts = datetime.now().strftime("%H:%M:%S")
            self.output.appendPlainText(
                f"  [busy] RE Manager not idle — retry #{_attempt + 1} in {delay // 1000} s")
            QTimer.singleShot(delay, lambda: self.reupload_local_plans(_attempt + 1))

    def _on_local_plans_removed(self, dir_path: str) -> None:
        """Remove catalog entries for session plans from the removed folder,
        then close and reopen the RE environment to unload them from the Worker.
        """
        from .plans_manager import get_catalog, PLAN_TYPE_SESSION
        import ast as _ast
        catalog = get_catalog()
        if catalog:
            p = Path(dir_path)
            for f in p.glob("*.py"):
                try:
                    code = f.read_text(encoding="utf-8")
                    tree = _ast.parse(code)
                    for node in _ast.walk(tree):
                        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                            if catalog.get_type(node.name) == PLAN_TYPE_SESSION:
                                catalog._types.pop(node.name, None)
                                catalog._sources.pop(node.name, None)
                except Exception:
                    pass

        if not self.worker or not self.worker.rm:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(
            f"[{ts}] Closing RE environment to unload removed plans…")
        # Reopen as soon as the env_closed signal fires, then env_opened will
        # trigger reupload_local_plans for any remaining local-folder plans.
        self.worker.env_closed.connect(self._reopen_env_after_close)
        self.worker.close_environment()

    def _reopen_env_after_close(self) -> None:
        self.worker.env_closed.disconnect(self._reopen_env_after_close)
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.appendPlainText(f"[{ts}] Reopening RE environment…")
        self.worker.open_environment()

    # ── Public update slots ────────────────────────────────────────────────────

    def update_plans(self, plans: dict):
        self.plans = plans

    def update_devices(self, devices: dict):
        self.devices = devices
        self._composer.set_devices(devices)
