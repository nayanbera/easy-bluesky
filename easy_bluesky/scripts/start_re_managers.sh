#!/usr/bin/env bash
# start_re_managers.sh — Start real and sim RE Manager instances side by side.
#
# Ports used (configurable via env vars):
#   Real:  CTRL=60615  INFO=60625
#   Sim:   CTRL=60616  INFO=60626
#
# Conda environment (optional):
#   Set CONDA_ENV and CONDA_PATH to run inside a specific conda environment.
#   Example:
#     CONDA_ENV=bluesky CONDA_PATH=~/miniconda3 ./start_re_managers.sh
#
# Logs written to $LOG_DIR (default /tmp).
# Options: --real-only | --sim-only

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"

REAL_CTRL="${REAL_CTRL_PORT:-60615}"
REAL_INFO="${REAL_INFO_PORT:-60625}"
SIM_CTRL="${SIM_CTRL_PORT:-60616}"
SIM_INFO="${SIM_INFO_PORT:-60626}"

CONDA_ENV="${CONDA_ENV:-}"
CONDA_PATH="${CONDA_PATH:-$HOME/miniconda3}"

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
            echo ""
            echo "Environment variables:"
            echo "  CONDA_ENV    Conda environment name (e.g. bluesky)"
            echo "  CONDA_PATH   Conda base directory (default: ~/miniconda3)"
            echo "  REAL_CTRL_PORT / REAL_INFO_PORT  (default: 60615 / 60625)"
            echo "  SIM_CTRL_PORT  / SIM_INFO_PORT   (default: 60616 / 60626)"
            echo "  LOG_DIR      Log directory (default: /tmp)"
            exit 0 ;;
    esac
done

# ── Resolve the start-re-manager executable ────────────────────────────────────
if [ -n "$CONDA_ENV" ]; then
    CONDA_BIN="${CONDA_PATH}/bin/conda"
    if [ ! -x "$CONDA_BIN" ]; then
        echo "[ERROR] conda not found at: ${CONDA_BIN}"
        echo "        Set CONDA_PATH to your conda base directory."
        exit 1
    fi
    # Verify the env exists
    if ! "$CONDA_BIN" env list | grep -q "^${CONDA_ENV} \|^${CONDA_ENV}$"; then
        echo "[ERROR] conda env '${CONDA_ENV}' not found."
        echo "        Available envs:"; "$CONDA_BIN" env list
        exit 1
    fi
    RUN_PREFIX="${CONDA_BIN} run -n ${CONDA_ENV} --no-capture-output "
    echo "[INFO]  Using conda env: ${CONDA_ENV} (${CONDA_PATH})"
else
    RUN_PREFIX=""
    if ! command -v start-re-manager &>/dev/null; then
        echo "[ERROR] start-re-manager not found on PATH."
        echo "        Activate your environment first, or set CONDA_ENV."
        exit 1
    fi
fi

# ── Start a single instance ────────────────────────────────────────────────────
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

    # shellcheck disable=SC2086
    nohup ${RUN_PREFIX}start-re-manager \
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
