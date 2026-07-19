#!/usr/bin/env bash
# launch.sh — Start EasyBluesky, optionally connecting to a remote RE Manager.
#
# Usage:
#   ./launch.sh                     # connect to localhost (default)
#   ./launch.sh 192.168.1.50        # connect to a remote machine by IP
#   ./launch.sh beamline-control    # connect to a remote machine by hostname

# ── Configuration ──────────────────────────────────────────────────────────────
CONDA_ENV="easy-bluesky"
CONDA_BASE="/opt/anaconda3"

RE_HOST="${1:-localhost}"       # first argument, or localhost if not given

ZMQ_CONTROL_PORT=60615
ZMQ_INFO_PORT=60625
ZMQ_PUB_PORT=60630
# ───────────────────────────────────────────────────────────────────────────────

# Resolve full path to this script's directory so the app runs from the
# correct working directory regardless of where launch.sh is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate conda environment
source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate "${CONDA_ENV}"

if [[ $? -ne 0 ]]; then
    echo "ERROR: could not activate conda environment '${CONDA_ENV}'"
    echo "       Check CONDA_BASE and CONDA_ENV at the top of this script."
    exit 1
fi

# Export ZMQ addresses
export BLUESKY_ZMQ_CONTROL="tcp://${RE_HOST}:${ZMQ_CONTROL_PORT}"
export BLUESKY_ZMQ_INFO="tcp://${RE_HOST}:${ZMQ_INFO_PORT}"
export BLUESKY_ZMQ_PUB_HOST="${RE_HOST}"
export BLUESKY_ZMQ_PUB_PORT="${ZMQ_PUB_PORT}"

echo "──────────────────────────────────────────────"
echo "  EasyBluesky"
echo "  RE Manager : ${RE_HOST}"
echo "  Control    : ${BLUESKY_ZMQ_CONTROL}"
echo "  Info       : ${BLUESKY_ZMQ_INFO}"
echo "  Doc stream : tcp://${RE_HOST}:${ZMQ_PUB_PORT}"
echo "──────────────────────────────────────────────"

cd "${SCRIPT_DIR}"
python -c "from easy_bluesky.main import main; main()"
