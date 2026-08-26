#!/usr/bin/env bash
set -euo pipefail

USER_HOME="$(dscl . -read "/Users/$USER" NFSHomeDirectory | awk '{print $2}')"
PLIST_PATH="$USER_HOME/Library/LaunchAgents/com.chris958.stocktopic.plist"
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
if [[ -f "$PLIST_PATH" ]]; then
  mv "$PLIST_PATH" "$PLIST_PATH.disabled"
fi
echo "服务已停用。项目、数据库和备份均未删除。"

