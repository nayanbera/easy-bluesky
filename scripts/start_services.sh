#!/usr/bin/env bash
# scripts/start_services.sh
# Start all services required by EasyBluesky
# Usage: bash scripts/start_services.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Starting Bluesky services..."

# 1. Tiled server (read-only directory serve — data written by suitcase.jsonl)
echo "[1/3] Starting Tiled server (port 8000)..."
RUNS_DIR="$PROJECT_DIR/data/runs"
mkdir -p "$RUNS_DIR"
tiled serve directory "$RUNS_DIR" \
    --api-key "${BLUESKY_TILED_API_KEY:-bluesky}" \
    --watch \
    --port 8000 &
TILED_PID=$!
echo "  Tiled PID: $TILED_PID"
sleep 3

# 2. RE Manager
echo "[2/3] Starting RE Manager..."
start-re-manager \
    --zmq-publish-console ON \
    --existing-plans-devices "$PROJECT_DIR/scripts/existing_plans_and_devices.yaml" \
    --user-group-permissions "$PROJECT_DIR/scripts/user_group_permissions.yaml" \
    --startup-script "$PROJECT_DIR/scripts/re_startup_mongo.py" &
RE_PID=$!
echo "  RE Manager PID: $RE_PID"
sleep 2

# 3. Launch the app
echo "[3/3] Launching EasyBluesky..."
python3 -m easy_bluesky.main

# Cleanup on exit
trap "kill $TILED_PID $RE_PID 2>/dev/null; echo 'Services stopped.'" EXIT
