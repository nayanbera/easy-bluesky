"""Load and save ESAF server configuration from ~/.easy_bluesky/esaf_server/config.json."""

from __future__ import annotations

import copy
import json
import os
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "backend": "sqlite",
    "mongodb": {
        "uri": "mongodb://localhost:27017",
        "database": "esaf_db",
    },
    "sqlite": {
        "db_path": "~/.easy_bluesky/esaf_server/esaf.db",
        "pdf_dir": "~/.easy_bluesky/esaf_server/pdfs/",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "api_key": "",  # empty = no auth required for reads; writes need key if set
    },
}

_CONFIG_PATH = os.path.expanduser("~/.easy_bluesky/esaf_server/config.json")


def load_config(config_path: str = _CONFIG_PATH) -> dict:
    """Load config from disk, merging with defaults for any missing keys."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            _deep_merge(config, on_disk)
        except (OSError, json.JSONDecodeError):
            pass  # fall back to defaults
    return config


def save_config(config: dict, config_path: str = _CONFIG_PATH) -> None:
    """Save config to disk, creating parent directories as needed."""
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base in place."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
