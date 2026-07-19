"""
re_startup_mongo.py
-------------------
Bluesky RE startup script: subscribes TiledWriter so every run is saved
to the local tiled/MongoDB backend automatically.

Add this file to your queue server's profile startup directory, e.g.:
    ~/.ipython/profile_qs_sim/startup/
or wherever your start-re-manager loads startup scripts from.

Environment variables:
    BLUESKY_TILED_URI     (default: http://localhost:8000)
    BLUESKY_TILED_API_KEY (default: bluesky)
"""

import os

_TILED_URI     = os.getenv("BLUESKY_TILED_URI",     "http://localhost:8000")
_TILED_API_KEY = os.getenv("BLUESKY_TILED_API_KEY", "bluesky")

try:
    from tiled.client import from_uri
    from bluesky.callbacks.tiled_writer import TiledWriter

    _tiled_client = from_uri(_TILED_URI, api_key=_TILED_API_KEY)
    _writer = TiledWriter(_tiled_client)
    RE.subscribe(_writer)
    print(f"[re_startup_mongo] TiledWriter subscribed → {_TILED_URI}")
except Exception as e:
    print(f"[re_startup_mongo] WARNING: could not subscribe TiledWriter: {e}")
