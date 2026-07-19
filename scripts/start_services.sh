#!/usr/bin/env bash
# scripts/start_services.sh
# Start all services required by the Bluesky Desktop App
# Usage: bash scripts/start_services.sh

set -e

echo "Starting Bluesky services..."

# 1. RE Manager
echo "[1/3] Starting RE Manager..."
start-re-manager --zmq-publish-console ON &
RE_PID=$!
echo "  RE Manager PID: $RE_PID"
sleep 2

# 2. HTTP Server (optional — only needed for web access)
# echo "[2/3] Starting HTTP Server..."
# export QSERVER_SINGLE_USER_API_KEY=mystaticapikey
# export QSERVER_ZMQ_CONTROL_ADDRESS=tcp://localhost:60615
# export QSERVER_ZMQ_INFO_ADDRESS=tcp://localhost:60625
# start-bluesky-httpserver --host 0.0.0.0 --port 60610 &
# HTTP_PID=$!

# 3. ZMQ to Kafka bridge
echo "[2/3] Starting ZMQ → Kafka bridge..."
python3 scripts/zmq_to_kafka.py &
BRIDGE_PID=$!
echo "  Bridge PID: $BRIDGE_PID"
sleep 1

# 4. Launch the app
echo "[3/3] Launching Bluesky App..."
python3 -m bluesky_app.main

# Cleanup on exit
trap "kill $RE_PID $BRIDGE_PID 2>/dev/null; echo 'Services stopped.'" EXIT
