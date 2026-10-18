"""ebplan_import.py — convert classic Visual Composer .ebplan data to a Blockly workspace.

Every classic block kind maps to a Blockly block; anything without a direct
equivalent (e.g. a plan block with per-step actions) falls back to a Python block
holding the classic generator's output, so the converted plan always behaves the same.
"""

from .plan_builder import (
    FlowNode, _gen_node,
    KIND_MOVE, KIND_RMOVE, KIND_MOVE_MULTI, KIND_SLEEP, KIND_SET_ATTR,
    KIND_OPEN_SHUTTER, KIND_CLOSE_SHUTTER, KIND_TRIGGER_READ, KIND_STAGE,
    KIND_UNSTAGE, KIND_PLAN, KIND_CUSTOM_PY, KIND_REPEAT, KIND_FOR_EACH,
    KIND_WHILE, KIND_IF_ELSE, KIND_STATS, _split_csv,
)


def _value(v) -> dict:
    """A literal number becomes a number block, anything else a Python-expression block."""
    text = str(v).strip()
    try:
        float(text)
        return {"block": {"type": "math_number", "fields": {"NUM": float(text)}}}
    except ValueError:
        return {"block": {"type": "eb_python_expr", "fields": {"CODE": text or "0"}}}


def _chain(blocks: list):
    head = None
    for b in reversed(blocks):
        if head is not None:
            b["next"] = {"block": head}
        head = b
    return head


def _body(nodes: list) -> dict:
    head = _chain([_convert(n) for n in nodes])
    return {"block": head} if head else {}


def _fallback(node: FlowNode) -> dict:
    return {"type": "eb_python", "fields": {"CODE": _gen_node(node, 0)}}


def _convert(node: FlowNode) -> dict:
    k, p = node.kind, node.params
    dev = p.get("device", "")

    if k == KIND_MOVE:
        return {"type": "eb_mv", "fields": {"DEV": dev or "motor"},
                "inputs": {"VALUE": _value(p.get("position", 0))}}
    if k == KIND_RMOVE:
        return {"type": "eb_mvr", "fields": {"DEV": dev or "motor"},
                "inputs": {"VALUE": _value(p.get("delta", 0))}}
    if k == KIND_MOVE_MULTI:
        motors = _split_csv(p.get("motors", ""))
        positions = _split_csv(p.get("positions", ""))
        positions += ["0"] * (len(motors) - len(positions))
        n = max(len(motors), 1)
        fields = {f"DEV{i}": m for i, m in enumerate(motors)}
        inputs = {f"POS{i}": _value(v) for i, v in enumerate(positions[:len(motors)])}
        return {"type": "eb_mv_multi", "extraState": {"n": n}, "fields": fields, "inputs": inputs}
    if k == KIND_SLEEP:
        return {"type": "eb_sleep", "inputs": {"SECONDS": _value(p.get("seconds", 1))}}
    if k == KIND_SET_ATTR:
        return {"type": "eb_set_attr",
                "fields": {"DEV": dev or "device", "ATTR": p.get("attribute", "attr")},
                "inputs": {"VALUE": _value(p.get("value", "0"))}}
    if k == KIND_OPEN_SHUTTER:
        return {"type": "eb_mv", "fields": {"DEV": dev or "shutter"},
                "inputs": {"VALUE": {"block": {"type": "text", "fields": {"TEXT": "open"}}}}}
    if k == KIND_CLOSE_SHUTTER:
        return {"type": "eb_mv", "fields": {"DEV": dev or "shutter"},
                "inputs": {"VALUE": {"block": {"type": "text", "fields": {"TEXT": "closed"}}}}}
    if k == KIND_STAGE:
        return {"type": "eb_stage", "fields": {"DEV": dev or "device"}}
    if k == KIND_UNSTAGE:
        return {"type": "eb_unstage", "fields": {"DEV": dev or "device"}}
    if k == KIND_TRIGGER_READ:
        return {"type": "eb_trigger_read", "fields": {"DETS": p.get("detectors", "") or "[det]"}}
    if k == KIND_CUSTOM_PY:
        return {"type": "eb_python", "fields": {"CODE": p.get("code", "pass") or "pass"}}
    if k == KIND_PLAN:
        if node.per_step:
            return _fallback(node)
        return {"type": "eb_plan", "fields": {"PLAN": p.get("plan_name", "plan"), "ARGS": ""}}
    if k == KIND_REPEAT:
        return {"type": "controls_repeat_ext",
                "inputs": {"TIMES": _value(p.get("n", 1)), "DO": _body(node.children)}}
    if k == KIND_FOR_EACH:
        mode = {"list": "list", "range": "range", "csv": "csv"}.get(p.get("iter_type", "list"), "list")
        fields = {
            "VARS": p.get("varname", "_val"), "MODE": mode,
            "VALUES": p.get("values", ""),
            "START": float(p.get("start", 0)), "STOP": float(p.get("stop", 1)),
            "NUM": int(p.get("num", 10)),
            "FILE": p.get("csv_file", "data.csv"), "COLS": str(p.get("csv_col", "0")),
            "MOTORS": p.get("motor", ""),
        }
        return {"type": "eb_foreach", "extraState": {"mode": mode}, "fields": fields,
                "inputs": {"DO": _body(node.children)}}
    if k == KIND_WHILE:
        return {"type": "controls_whileUntil", "fields": {"MODE": "WHILE"},
                "inputs": {"BOOL": _value(p.get("condition", "True")), "DO": _body(node.children)}}
    if k == KIND_IF_ELSE:
        block = {"type": "controls_if",
                 "inputs": {"IF0": _value(p.get("condition", "True")), "DO0": _body(node.children)}}
        if node.children_else:
            block["extraState"] = {"hasElse": True}
            block["inputs"]["ELSE"] = _body(node.children_else)
        return block
    if k == KIND_STATS:
        return {"type": "eb_stats",
                "fields": {"X": p.get("x", ""), "Y": p.get("y", ""),
                           "PREFIX": p.get("prefix", "stat"),
                           "MOVE": p.get("move_to", "none"), "MOTOR": p.get("motor", "") or "motor"},
                "inputs": {"DO": _body(node.children)}}
    return _fallback(node)


def classic_to_blockly(nodes_data: list) -> dict:
    """Convert a list of classic node dicts (as stored in .ebplan) to Blockly workspace JSON."""
    nodes = [FlowNode.from_dict(d) for d in nodes_data]
    head = _chain([_convert(n) for n in nodes])
    blocks = []
    if head:
        head.update({"x": 30, "y": 30})
        blocks.append(head)
    return {"blocks": {"languageVersion": 0, "blocks": blocks}}
