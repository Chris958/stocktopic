from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .http import open_url


class WeComDeliveryError(RuntimeError):
    def __init__(self, stage: str, errcode: int | str, errmsg: str, retryable: bool = False):
        self.stage = stage
        self.errcode = errcode
        self.errmsg = errmsg
        self.retryable = retryable
        guidance = _guidance(errcode)
        suffix = f"；处理建议：{guidance}" if guidance else ""
        super().__init__(f"企业微信群机器人{stage}失败（errcode={errcode}）：{errmsg}{suffix}")


class WeComNotifier:
    """Send outbound-only notifications through a WeCom group robot webhook."""

    def __init__(self, webhook_url: str, timeout: float = 20.0):
        self.webhook_url = webhook_url.strip()
        self.timeout = timeout
        if self.webhook_url:
            _validate_webhook_url(self.webhook_url)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_text(self, title: str, body: str) -> None:
        if not self.enabled:
            raise WeComDeliveryError("配置", "not_configured", "缺少WECOM_BOT_WEBHOOK")
        content = _truncate_utf8(f"{title}\n\n{body}", 2000)
        payload = {"msgtype": "text", "text": {"content": content}}
        for attempt in range(3):
            result = self._post(payload)
            code = _integer(result.get("errcode"), -1)
            if code == 0:
                return
            if code in {-1, 45009} and attempt < 2:
                time.sleep(0.5 * (2**attempt))
                continue
            raise WeComDeliveryError(
                "发送消息",
                code,
                str(result.get("errmsg") or "unknown error"),
                retryable=code in {-1, 45009},
            )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with open_url(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if isinstance(result, dict):
                    return result
                last_error = ValueError("response is not a JSON object")
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code < 500 and error.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
        message = str(last_error or "unknown network error")
        raise WeComDeliveryError("发送消息", "network", message, retryable=True) from last_error


def _validate_webhook_url(value: str) -> None:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    valid = bool(
        parsed.scheme == "https"
        and parsed.hostname == "qyapi.weixin.qq.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
        and parsed.path == "/cgi-bin/webhook/send"
        and set(query) == {"key"}
        and len(query.get("key", [])) == 1
        and query["key"][0].strip()
    )
    if not valid:
        raise WeComDeliveryError(
            "配置",
            "invalid_webhook",
            "Webhook必须是企业微信群机器人生成的完整HTTPS地址",
        )


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    suffix = "…".encode()
    return encoded[: maximum_bytes - len(suffix)].decode("utf-8", "ignore") + "…"


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _guidance(errcode: int | str) -> str:
    return {
        "not_configured": "运行configure_integrations.sh并填写群机器人Webhook",
        "invalid_webhook": "在企业微信群中重新添加机器人并复制完整Webhook地址",
        93000: "Webhook地址或机器人Key无效，请在群机器人设置中重新复制",
        45009: "企业微信群机器人调用频率超限，系统稍后重试",
    }.get(errcode, "")
