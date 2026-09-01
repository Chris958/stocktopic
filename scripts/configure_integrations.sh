#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$APP_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "未找到项目虚拟环境，请先运行 ./scripts/install_macos.sh"
  exit 1
fi

"$PYTHON_BIN" "$APP_DIR/scripts/configure_integrations.py"
launchctl kickstart -k "gui/$(id -u)/com.chris958.stocktopic"

echo "服务已重启。请运行：$APP_DIR/scripts/doctor.sh"
echo "然后在网页预警页面点击“测试群机器人”。"
