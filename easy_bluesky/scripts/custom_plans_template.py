"""custom_plans_template.py — starting point for a new plans file.

Copy to ``custom_plans.py`` (or any ``*.py`` in the profile's plans folder).
Scan numbers are handled by ``easy_bluesky_common`` (uploaded automatically with
re_startup_mongo.py); do not compute them in the plan.

* Take ``md=None`` as the last parameter and pass ``md=md`` to the scan -- the RE
  preprocessor then replaces a stale re-queued number at ``open_run``.
* Decorate the plan with ``@eb_plan`` only if it needs the final number *before*
  ``open_run`` (e.g. to build a file name): ``md["scan_num"]`` is then already resolved.
"""

import bluesky.plans as bp

try:
    from easy_bluesky_common import eb_plan
except Exception:          # module not on the RE machine: plain plan, queued number as is
    def eb_plan(fn):
        return fn


@eb_plan
def example_scan(detectors, motor, start: float, stop: float, num: int, md=None):
    """Example plan using the standard metadata conventions."""
    md = md or {}
    file_suffix = f"_S_{md.get('scan_num', 0):04d}"   # final number, safe to use here
    yield from bp.scan(detectors, motor, start, stop, num, md=md)
