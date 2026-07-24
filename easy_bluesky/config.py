"""
config.py
---------
All configuration constants for the EasyBluesky.
Override via environment variables or a local config file.
"""

import os

# ── ZMQ ───────────────────────────────────────────────────────────────────────
ZMQ_CONTROL  = os.getenv("BLUESKY_ZMQ_CONTROL",  "tcp://localhost:60615")
ZMQ_INFO     = os.getenv("BLUESKY_ZMQ_INFO",     "tcp://localhost:60625")
ZMQ_DOC_PORT = int(os.getenv("BLUESKY_ZMQ_PUB_PORT", "60630"))
ZMQ_DOC_HOST = os.getenv("BLUESKY_ZMQ_PUB_HOST", "localhost")
ZMQ_DOC_ADDR = f"tcp://{ZMQ_DOC_HOST}:{ZMQ_DOC_PORT}"

# ── Kafka ──────────────────────────────────────────────────────────────────────
KAFKA_SERVER = os.getenv("BLUESKY_KAFKA_SERVER", "localhost:9092")
KAFKA_TOPIC  = os.getenv("BLUESKY_KAFKA_TOPIC",  "bluesky.runengine.documents")

# ── Databroker / Tiled ────────────────────────────────────────────────────────
CATALOG_NAME = os.getenv("BLUESKY_CATALOG",      "bluesky_local")
TILED_URI    = os.getenv("BLUESKY_TILED_URI",    "http://localhost:8000")
TILED_API_KEY = os.getenv("BLUESKY_TILED_API_KEY", "bluesky")

# ── Local data dir (suitcase.jsonl output) ────────────────────────────────────
import pathlib as _pl
_USER_DIR = _pl.Path.home() / ".easy_bluesky"
DATA_RUNS_DIR = os.getenv(
    "BLUESKY_DATA_DIR",
    str(_USER_DIR / "data" / "runs"),
)

# ── Experiments ───────────────────────────────────────────────────────────────
EXPERIMENTS_DIR = os.getenv(
    "BLUESKY_EXPERIMENTS_DIR",
    str(_USER_DIR / "experiments"),
)
ACTIVE_EXPERIMENT_FILE = str(_USER_DIR / "data" / "active_experiment.json")

# ── UI Colors (semantic — constant across themes) ─────────────────────────────
ACCENT   = "#1f77b4"
SUCCESS  = "#2ca02c"
WARNING  = "#ff7f0e"
DANGER   = "#d62728"
# Dark-theme structural colors (kept for backwards compat; themes.py is canonical)
DARK_BG  = "#1e1e1e"
PANEL_BG = "#252526"
BORDER   = "#3c3c3c"

PLOT_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#17becf",
]

# ── App ────────────────────────────────────────────────────────────────────────
APP_NAME    = "EasyBluesky"
APP_VERSION = "0.1.0"
