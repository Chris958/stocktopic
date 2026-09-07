#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "===== STOCKTOPIC DOCTOR ====="
echo "APP_DIR=$APP_DIR"
echo "PYTHON=$($APP_DIR/.venv/bin/python --version 2>&1 || true)"
echo "LAUNCHD_STATUS:"
launchctl print "gui/$(id -u)/com.chris958.stocktopic" 2>/dev/null | head -30 || echo "NOT_LOADED"
echo "HEALTH:"
health_url="http://127.0.0.1:8765/health"
health_attempts="${STOCKTOPIC_HEALTH_ATTEMPTS:-30}"
health_interval="${STOCKTOPIC_HEALTH_INTERVAL_SECONDS:-1}"
health_ready=0
for ((attempt = 1; attempt <= health_attempts; attempt++)); do
  if health_output="$(curl --fail --silent --max-time 2 "$health_url" 2>/dev/null)"; then
    echo "$health_output"
    health_ready=1
    break
  fi
  if ((attempt < health_attempts)); then
    sleep "$health_interval"
  fi
done
if ((health_ready == 0)); then
  echo "服务在 ${health_attempts} 次检查后仍未就绪"
  curl --fail --silent --show-error --max-time 10 "$health_url" || true
fi
echo
echo "RECENT_ERROR_LOG:"
tail -30 "$APP_DIR/logs/stocktopic.err.log" 2>/dev/null || echo "NO_ERROR_LOG"
echo "===== FINISHED ====="
