from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .http import open_url

MAX_REQUEST_BYTES = 20_000
RETRYABLE_CODES = {11232, 99991400}


def redact_feishu_webhook(message: str) -> str:
    return re.sub(
        r"(?i)(/open-apis/bot/(?:v2/)?hook/)[^/\s?&#\"'<>)]*",
        r"\1***",
        message,
    )


class FeishuDeliveryError(RuntimeError):
    def __init__(self, code: int | str, message: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        guidance = {
            "not_configured": "运行configure_integrations.sh --feishu-only配置飞书Webhook",
            "invalid_webhook": "请复制飞书自定义机器人生成的完整HTTPS地址",
            19001: "机器人地址已失效，请重新复制Webhook",
            19021: "请核对FEISHU_BOT_SECRET签名密钥及系统时间",
            19022: "请核对机器人IP白名单",
            19024: "消息未匹配机器人的关键词规则，请核对安全设置",
            11232: "触发飞书限流，系统已进行有界重试",
        }.get(code, "")
        detail = redact_feishu_webhook(message)[:500]
        super().__init__(
            f"飞书机器人发送失败（code={code}）：{detail}"
            + (f"；处理建议：{guidance}" if guidance else "")
        )


def validate_feishu_webhook(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "open.feishu.cn"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", parsed.path)
        )
    except ValueError:
        valid = False
    if not valid:
        raise FeishuDeliveryError("invalid_webhook", "Webhook格式无效")


class FeishuNotifier:
    """Outbound-only custom bot; one bounded retry budget for all failures."""

    def __init__(self, webhook_url: str, *, secret: str = "", timeout: float = 20.0):
        self.webhook_url = webhook_url.strip()
        self.secret = secret.strip()
        self.timeout = timeout
        if self.webhook_url:
            validate_feishu_webhook(self.webhook_url)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _safe_message(self, message: str) -> str:
        message = message.replace(self.webhook_url, "[飞书Webhook]")
        message = message.replace(self.webhook_url.rsplit("/", 1)[-1], "***")
        return message.replace(self.secret, "***") if self.secret else message

    def _request_data(self, title: str, body: str) -> bytes:
        payload: dict[str, Any] = {"msg_type": "text"}
        if self.secret:
            timestamp = str(int(time.time()))
            key = f"{timestamp}\n{self.secret}".encode()
            payload.update(
                timestamp=timestamp,
                sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode(),
            )

        def encode(text: str) -> bytes:
            return json.dumps(
                {**payload, "content": {"text": text}}, ensure_ascii=False, separators=(",", ":")
            ).encode()

        content = f"{title}\n\n{body}"
        data = encode(content)
        if len(data) > MAX_REQUEST_BYTES:
            low, high = 0, len(content)
            while low < high:
                middle = (low + high + 1) // 2
                if len(encode(content[:middle] + "…")) <= MAX_REQUEST_BYTES:
                    low = middle
                else:
                    high = middle - 1
            data = encode(content[:low] + "…")
        return data

    def send_text(self, title: str, body: str) -> None:
        if not self.enabled:
            raise FeishuDeliveryError("not_configured", "缺少FEISHU_BOT_WEBHOOK")
        for attempt in range(3):
            request = urllib.request.Request(
                self.webhook_url,
                data=self._request_data(title, body),
                method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            try:
                with open_url(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("response is not a JSON object")
                code = result.get("code", result.get("StatusCode"))
                if type(code) is int and code == 0:
                    return
                failure = FeishuDeliveryError(
                    code if type(code) is int else "invalid_response",
                    self._safe_message(
                        str(result.get("msg") or result.get("StatusMessage") or "响应缺少有效code")
                    ),
                    retryable=type(code) is int and code in RETRYABLE_CODES,
                )
            except urllib.error.HTTPError as error:
                failure = FeishuDeliveryError(
                    error.code,
                    f"HTTP {error.code}",
                    retryable=error.code == 429 or error.code >= 500,
                )
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                # Providers may echo the complete webhook or signing secret in an error.
                failure = FeishuDeliveryError(
                    "network",
                    self._safe_message(str(error)),
                    retryable=True,
                )
            if attempt == 2 or not failure.retryable:
                raise failure from None
            time.sleep(0.5 * (2**attempt))
