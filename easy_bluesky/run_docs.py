"""run_docs.py — helpers for reading bluesky document streams.

Runs carry extra streams besides ``primary`` (notably ``baseline``: positioner values read
once before and once after the scan).  Plots, tables and progress counters must only use
the primary stream, so readers feed each document through a ``PrimaryOnly`` filter.
"""


class PrimaryOnly:
    """Stateful filter: ``keep(name, doc)`` is False for descriptors of non-primary streams
    and for the events/event pages that belong to them."""

    def __init__(self):
        self._skip: set = set()

    def reset(self):
        self._skip.clear()

    def keep(self, name: str, doc: dict) -> bool:
        if name == "descriptor":
            if doc.get("name", "primary") != "primary":
                self._skip.add(doc.get("uid", ""))
                return False
            return True
        if name in ("event", "event_page"):
            return doc.get("descriptor", "") not in self._skip
        return True
