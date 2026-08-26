#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "===== STOCKTOPIC DOCTOR ====="
echo "APP_DIR=$APP_DIR"
echo "PYTHON=$($APP_DIR/.venv/bin/python --version 2>&1 || true)"
echo "LAUNCHD_STATUS:"
launchctl print "gui/$(id -u)/com.chris958.stocktopic" 2>/dev/null | head -30 || echo "NOT_LOADED"
echo "HEALTH:"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8765/health || true
echo
echo "RECENT_ERROR_LOG:"
tail -30 "$APP_DIR/logs/stocktopic.err.log" 2>/dev/null || echo "NO_ERROR_LOG"
echo "===== FINISHED ====="

