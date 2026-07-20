#!/usr/bin/env bash
# start_re_managers.sh — Start real and sim RE Manager instances side by side.
#
# Ports used (configurable via env vars):
#   Real:  CTRL=60615  INFO=60625  DOC=60630
#   Sim:   CTRL=60616  INFO=60626  DOC=60631
#
# Logs written to $LOG_DIR (default /tmp).
# Run with --real-only or --sim-only to start a single instance.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"

REAL_CTRL="${REAL_CTRL_PORT:-60615}"
REAL_INFO="${REAL_INFO_PORT:-60625}"
SIM_CTRL="${SIM_CTRL_PORT:-60616}"
SIM_INFO="${SIM_INFO_PORT:-60626}"

START_REAL=true
START_SIM=true

for arg in "$@"; do
    case "$arg" in
        --real-only) START_SIM=false ;;
        --sim-only)  START_REAL=false ;;
        --help|-h)
            echo "Usage: $(basename "$0") [--real-only | --sim-only]"
            echo "  --real-only  Start only the real hardware instance"
            echo "  --sim-only   Start only the simulation instance"
            exit 0 ;;
    esac
done

if ! command -v start-re-manager &>/dev/null; then
    echo "[ERROR] start-re-manager not found."
    echo "        Install it with:  pip install bluesky-queueserver"
    exit 1
fi

start_instance() {
    local name="$1"
    local script="$2"
    local ctrl_port="$3"
    local info_port="$4"
    local log="$LOG_DIR/re-manager-${name}.log"

    # Check if already running on this control port
    if pgrep -f "zmq-control-addr tcp://\*:${ctrl_port}" &>/dev/null 2>&1 || \
       pgrep -f "zmq-control-addr tcp://.*:${ctrl_port}" &>/dev/null 2>&1; then
        echo "[SKIP]  RE Manager (${name}) already running on port ${ctrl_port}"
        return
    fi

    local startup="${SCRIPTS_DIR}/${script}"
    if [ ! -f "$startup" ]; then
        echo "[WARN]  Startup script not found: ${startup}"
        echo "        Run the EasyBluesky app once so it creates default scripts,"
        echo "        or copy your own to: ${SCRIPTS_DIR}/"
        return
    fi

    local existing_pd="${SCRIPTS_DIR}/existing_plans_and_devices.yaml"
    local permissions="${SCRIPTS_DIR}/user_group_permissions.yaml"

    nohup start-re-manager \
        --zmq-control-addr "tcp://*:${ctrl_port}" \
        --zmq-info-addr    "tcp://*:${info_port}" \
        --zmq-publish-console ON \
        --startup-script "$startup" \
        --existing-plans-devices "$existing_pd" \
        --user-group-permissions "$permissions" \
        > "$log" 2>&1 &

    local pid=$!
    echo "[OK]    RE Manager (${name}) started — PID ${pid}"
    echo "        Control: tcp://*:${ctrl_port}  Info: tcp://*:${info_port}"
    echo "        Log:     ${log}"
}

echo "========================================"
echo " Starting Bluesky RE Manager instances"
echo "========================================"

$START_REAL && start_instance "real" "re_startup_mongo.py" "$REAL_CTRL" "$REAL_INFO"
$START_SIM  && start_instance "sim"  "re_startup_sim.py"   "$SIM_CTRL"  "$SIM_INFO"

echo "========================================"
echo " Done — connect with the EasyBluesky app"
echo "========================================"
