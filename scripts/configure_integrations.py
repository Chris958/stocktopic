from __future__ import annotations

import getpass
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

APP_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = APP_DIR / ".env"


def read_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        raise SystemExit("未找到.env，请先运行 ./scripts/install_macos.sh")
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def prompt_value(label: str, current: str, *, secret: bool = False) -> str:
    suffix = " [已配置，回车保留]" if secret and current else f" [{current}]" if current else ""
    prompt = f"{label}{suffix}: "
    value = getpass.getpass(prompt) if secret else input(prompt)
    return value.strip() or current


def upsert(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            result.append(f"{key}={remaining.pop(key)}")
        else:
            result.append(line)
    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.extend(f"{key}={value}" for key, value in remaining.items())
    return result


def main() -> None:
    values = read_values()
    print("===== STOCKTOPIC INTEGRATION CONFIG =====")
    print("敏感值不会显示；秘密字段直接回车会保留原值。")
    updates = {
        "OPENAI_API_KEY": prompt_value(
            "OpenAI API Key", values.get("OPENAI_API_KEY", ""), secret=True
        ),
        "OPENAI_BASE_URL": prompt_value(
            "OpenAI Base URL",
            values.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        ),
        "OPENAI_MODEL": prompt_value("OpenAI模型", values.get("OPENAI_MODEL", "gpt-5.5")),
        "NUMCAT_API_KEY": prompt_value(
            "猫爪数据 API Key",
            values.get("NUMCAT_API_KEY", ""),
            secret=True,
        ),
        "WECOM_BOT_WEBHOOK": prompt_value(
            "企业微信群机器人完整Webhook",
            values.get("WECOM_BOT_WEBHOOK", ""),
            secret=True,
        ),
    }
    base = urlsplit(updates["OPENAI_BASE_URL"])
    if updates["OPENAI_API_KEY"] and (base.scheme not in {"http", "https"} or not base.netloc):
        raise SystemExit("OpenAI Base URL必须是完整的http(s)地址。")
    webhook = urlsplit(updates["WECOM_BOT_WEBHOOK"])
    webhook_query = parse_qs(webhook.query, keep_blank_values=True)
    try:
        webhook_port = webhook.port
    except ValueError:
        webhook_port = -1
    if updates["WECOM_BOT_WEBHOOK"] and not (
        webhook.scheme == "https"
        and webhook.hostname == "qyapi.weixin.qq.com"
        and webhook_port in {None, 443}
        and webhook.username is None
        and webhook.password is None
        and not webhook.fragment
        and webhook.path == "/cgi-bin/webhook/send"
        and set(webhook_query) == {"key"}
        and len(webhook_query["key"]) == 1
        and webhook_query["key"][0].strip()
    ):
        raise SystemExit("企业微信群机器人Webhook格式无效，请复制机器人生成的完整HTTPS地址。")
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise SystemExit("配置值不能包含换行。")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    new_lines = upsert(lines, updates)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=APP_DIR, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines).rstrip() + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, ENV_PATH)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print("配置已安全写入.env。")


if __name__ == "__main__":
    main()
