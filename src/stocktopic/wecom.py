from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .http import open_url


class WeComDeliveryError(RuntimeError):
    def __init__(self, stage: str, errcode: int | str, errmsg: str, retryable: bool = False):
        self.stage = stage
        self.errcode = errcode
        self.errmsg = errmsg
        self.retryable = retryable
        guidance = _guidance(errcode)
        suffix = f"；处理建议：{guidance}" if guidance else ""
        super().__init__(f"企业微信{stage}失败（errcode={errcode}）：{errmsg}{suffix}")


class WeComNotifier:
    def __init__(
        self,
        corp_id: str,
        agent_id: str,
        secret: str,
        to_user: str = "@all",
        timeout: float = 20.0,
    ):
        self.corp_id = corp_id.strip()
        self.agent_id = agent_id.strip()
        self.secret = secret.strip()
        self.to_user = to_user.strip() or "@all"
        self.timeout = timeout
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.corp_id and self.agent_id and self.secret)

    def send_text(self, title: str, body: str) -> None:
        if not self.enabled:
            raise WeComDeliveryError("配置", "not_configured", "CorpID/AgentID/Secret不完整")
        try:
            agent_id = int(self.agent_id)
        except ValueError as error:
            raise WeComDeliveryError("配置", "invalid_agentid", "AgentID必须是数字") from error
        content = f"{title}\n\n{body}"[:2000]
        payload = {
            "touser": self.to_user,
            "msgtype": "text",
            "agentid": agent_id,
            "text": {"content": content},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        token = self._access_token()
        token_refreshed = False
        for attempt in range(3):
            result = self._post(
                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                payload,
            )
            code = int(result.get("errcode", -1))
            if code == 0:
                return
            if code in {40014, 42001} and not token_refreshed:
                self._expires_at = 0
                token = self._access_token()
                token_refreshed = True
                continue
            if code in {-1, 45009} and attempt < 2:
                time.sleep(0.5 * (2**attempt))
                continue
            raise WeComDeliveryError(
                "发送消息",
                code,
                str(result.get("errmsg") or "unknown error"),
                retryable=code in {-1, 45009},
            )

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            query = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.secret})
            result = self._get_json(
                f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?{query}", "获取Token"
            )
            code = int(result.get("errcode", -1))
            if code != 0:
                raise WeComDeliveryError(
                    "获取Token", code, str(result.get("errmsg") or "unknown error")
                )
            self._token = str(result["access_token"])
            self._expires_at = time.time() + int(result.get("expires_in", 7200)) - 300
            return self._token

    def _get_json(self, url: str, stage: str) -> dict[str, Any]:
        return self._open_json(url, stage)

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        return self._open_json(request, "发送消息")

    def _open_json(self, request: str | urllib.request.Request, stage: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with open_url(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code < 500 and error.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
        message = str(last_error or "unknown network error")
        raise WeComDeliveryError(stage, "network", message, retryable=True) from last_error


def _guidance(errcode: int | str) -> str:
    return {
        60020: "将Mac mini当前公网IPv4加入该自建应用的企业可信IP",
        40013: "检查CorpID是否属于当前企业",
        40001: "检查自建应用Secret，必要时在企业微信后台重置",
        40003: "检查接收UserID是否为通讯录中的账号ID",
        81013: "接收账号不在应用可见范围，请调整应用可见范围",
        40014: "access_token无效，系统已自动刷新一次",
        42001: "access_token已过期，系统已自动刷新一次",
        45009: "企业微信接口调用频率超限，稍后重试",
    }.get(errcode, "")
