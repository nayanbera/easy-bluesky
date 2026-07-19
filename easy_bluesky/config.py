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

# ── Kafka ──────────────────────────────────────────────────────────────────────
KAFKA_SERVER = os.getenv("BLUESKY_KAFKA_SERVER", "localhost:9092")
KAFKA_TOPIC  = os.getenv("BLUESKY_KAFKA_TOPIC",  "bluesky.runengine.documents")

# ── Databroker ────────────────────────────────────────────────────────────────
CATALOG_NAME = os.getenv("BLUESKY_CATALOG",      "bluesky_local")

# ── UI Colors ─────────────────────────────────────────────────────────────────
ACCENT   = "#1f77b4"
SUCCESS  = "#2ca02c"
WARNING  = "#ff7f0e"
DANGER   = "#d62728"
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
