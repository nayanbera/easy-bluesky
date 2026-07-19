#!/usr/bin/env bash
# scripts/start_services.sh
# Start all services required by EasyBluesky
# Usage: bash scripts/start_services.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Starting Bluesky services..."

# 1. Tiled server (MongoDB backend)
echo "[1/4] Starting Tiled server (port 8000)..."
cd "$PROJECT_DIR"
tiled serve pyobject scripts.tiled_adapter:adapter \
    --public \
    --api-key "${BLUESKY_TILED_API_KEY:-bluesky}" \
    --port 8000 &
TILED_PID=$!
echo "  Tiled PID: $TILED_PID"
sleep 3

# 2. RE Manager (with MongoDB/TiledWriter startup script)
echo "[2/4] Starting RE Manager..."
start-re-manager \
    --zmq-publish-console ON \
    --startup-script "$PROJECT_DIR/scripts/re_startup_mongo.py" &
RE_PID=$!
echo "  RE Manager PID: $RE_PID"
sleep 2

# 3. ZMQ → Kafka bridge (live viewer)
echo "[3/4] Starting ZMQ → Kafka bridge..."
python3 scripts/zmq_to_kafka.py &
BRIDGE_PID=$!
echo "  Bridge PID: $BRIDGE_PID"
sleep 1

# 4. Launch the app
echo "[4/4] Launching EasyBluesky..."
python3 -m easy_bluesky.main

# Cleanup on exit
trap "kill $TILED_PID $RE_PID $BRIDGE_PID 2>/dev/null; echo 'Services stopped.'" EXIT
