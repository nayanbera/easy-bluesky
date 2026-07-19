"""
tiled_adapter.py
----------------
Exposes a MongoAdapter instance for the tiled server.

Start the server with:
    tiled serve pyobject scripts.tiled_adapter:adapter --public --api-key bluesky --port 8000

Or use start_services.sh which does this automatically.

MongoDB URIs are read from environment variables:
    BLUESKY_MONGO_META_URI  (default: mongodb://localhost:27017/bluesky_metadatastore)
    BLUESKY_MONGO_ASSET_URI (default: mongodb://localhost:27017/bluesky_asset_registry)
"""

import os
from databroker.mongo_normalized import MongoAdapter

_META_URI  = os.getenv("BLUESKY_MONGO_META_URI",  "mongodb://localhost:27017/bluesky_metadatastore")
_ASSET_URI = os.getenv("BLUESKY_MONGO_ASSET_URI", "mongodb://localhost:27017/bluesky_asset_registry")

adapter = MongoAdapter.from_uri(
    _META_URI,
    asset_registry_uri=_ASSET_URI,
)
