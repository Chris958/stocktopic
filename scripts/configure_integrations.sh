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
echo "然后可运行Level-2测试：$PYTHON_BIN $APP_DIR/scripts/analyze_level2.py --code 603269.SH"
echo "群机器人可在网页预警页面点击“测试群机器人”。"
