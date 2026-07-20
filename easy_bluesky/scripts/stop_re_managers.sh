#!/usr/bin/env bash
# stop_re_managers.sh — Stop all running RE Manager instances on this machine.

echo "========================================"
echo " Stopping Bluesky RE Manager instances"
echo "========================================"

if pgrep -f "start-re-manager" &>/dev/null; then
    pkill -f "start-re-manager"
    echo "[OK]    All RE Manager instances stopped"
else
    echo "[INFO]  No RE Manager instances were running"
fi
