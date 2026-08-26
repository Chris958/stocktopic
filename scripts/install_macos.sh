#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "此安装脚本仅支持macOS。"
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${STOCKTOPIC_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到python3，请先安装Python 3.11或更高版本。"
  exit 1
fi

PYTHON_OK="$($PYTHON_BIN -c 'import sys; print(int(sys.version_info >= (3, 11)))')"
if [[ "$PYTHON_OK" != "1" ]]; then
  echo "Python版本过低，需要3.11或更高版本。"
  exit 1
fi

USER_HOME="$(dscl . -read "/Users/$USER" NFSHomeDirectory | awk '{print $2}')"
LAUNCH_DIR="$USER_HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_DIR/com.chris958.stocktopic.plist"
LOG_DIR="$APP_DIR/logs"
DATA_DIR="$APP_DIR/data"
VENV_DIR="$APP_DIR/.venv"

mkdir -p "$LAUNCH_DIR" "$LOG_DIR" "$DATA_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -e "$APP_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "首次配置：密钥只会保存在Mac mini本地的.env文件中。"
  read -r -s -p "Tushare Token: " TUSHARE_TOKEN_INPUT; echo
  read -r -s -p "OpenAI API Key: " OPENAI_KEY_INPUT; echo
  read -r -p "OpenAI Base URL [https://api.openai.com/v1]: " OPENAI_BASE_URL_INPUT
  OPENAI_BASE_URL_INPUT="${OPENAI_BASE_URL_INPUT:-https://api.openai.com/v1}"
  read -r -p "OpenAI模型 [gpt-5.5]: " OPENAI_MODEL_INPUT
  OPENAI_MODEL_INPUT="${OPENAI_MODEL_INPUT:-gpt-5.5}"
  read -r -p "企业微信 CorpID: " WECOM_CORP_INPUT
  read -r -p "企业微信 AgentID: " WECOM_AGENT_INPUT
  read -r -s -p "企业微信 Secret: " WECOM_SECRET_INPUT; echo
  read -r -p "企业微信接收UserID [@all]: " WECOM_USER_INPUT
  WECOM_USER_INPUT="${WECOM_USER_INPUT:-@all}"
  read -r -p "管理用户名 [admin]: " ADMIN_USER_INPUT
  ADMIN_USER_INPUT="${ADMIN_USER_INPUT:-admin}"
  read -r -s -p "设置管理密码: " ADMIN_PASSWORD_INPUT; echo
  if [[ -z "$TUSHARE_TOKEN_INPUT" || -z "$ADMIN_PASSWORD_INPUT" ]]; then
    echo "Tushare Token和管理密码不能为空。"
    exit 1
  fi
  APP_TOKEN="$($VENV_DIR/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  {
    printf 'TUSHARE_TOKEN=%s\n' "$TUSHARE_TOKEN_INPUT"
    printf 'OPENAI_API_KEY=%s\n' "$OPENAI_KEY_INPUT"
    printf 'OPENAI_BASE_URL=%s\n' "$OPENAI_BASE_URL_INPUT"
    printf 'OPENAI_MODEL=%s\n' "$OPENAI_MODEL_INPUT"
    printf 'WECOM_CORP_ID=%s\n' "$WECOM_CORP_INPUT"
    printf 'WECOM_AGENT_ID=%s\n' "$WECOM_AGENT_INPUT"
    printf 'WECOM_SECRET=%s\n' "$WECOM_SECRET_INPUT"
    printf 'WECOM_TO_USER=%s\n' "$WECOM_USER_INPUT"
    printf 'ADMIN_USERNAME=%s\n' "$ADMIN_USER_INPUT"
    printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD_INPUT"
    printf 'APP_API_TOKEN=%s\n' "$APP_TOKEN"
    printf 'STOCKTOPIC_DB_PATH=%s/data/stocktopic.sqlite3\n' "$APP_DIR"
    printf 'STOCKTOPIC_ARCHIVE_DIR=%s/data/archive\n' "$APP_DIR"
    printf 'STOCKTOPIC_HOST=127.0.0.1\n'
    printf 'STOCKTOPIC_PORT=8765\n'
    printf 'LOG_LEVEL=INFO\n'
  } > "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

if ! grep -q '^OPENAI_BASE_URL=' "$APP_DIR/.env"; then
  OPENAI_BASE_URL_INPUT="https://api.openai.com/v1"
  if [[ -t 0 ]]; then
    echo "检测到旧版.env缺少OPENAI_BASE_URL。"
    read -r -p "OpenAI Base URL [https://api.openai.com/v1]: " OPENAI_BASE_URL_INPUT
    OPENAI_BASE_URL_INPUT="${OPENAI_BASE_URL_INPUT:-https://api.openai.com/v1}"
  fi
  printf '\nOPENAI_BASE_URL=%s\n' "$OPENAI_BASE_URL_INPUT" >> "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

sed \
  -e "s|__PYTHON__|$VENV_DIR/bin/python|g" \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  "$APP_DIR/launchd/com.chris958.stocktopic.plist.template" > "$PLIST_PATH"
plutil -lint "$PLIST_PATH"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/com.chris958.stocktopic"
launchctl kickstart -k "gui/$(id -u)/com.chris958.stocktopic"

echo "安装完成。"
echo "管理页面：http://127.0.0.1:8765"
echo "健康检查：http://127.0.0.1:8765/health"
echo "日志目录：$LOG_DIR"
echo "请运行：$APP_DIR/scripts/doctor.sh"
