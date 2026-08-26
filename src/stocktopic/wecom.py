from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from .http import open_url


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
            raise RuntimeError("WeCom is not configured")
        content = f"{title}\n\n{body}"[:2000]
        payload = {
            "touser": self.to_user,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": content},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        token = self._access_token()
        result = self._post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}", payload
        )
        if int(result.get("errcode", -1)) != 0:
            # Token can be invalidated by another caller; refresh once.
            if int(result.get("errcode", -1)) in {40014, 42001}:
                self._expires_at = 0
                token = self._access_token()
                result = self._post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                    payload,
                )
            if int(result.get("errcode", -1)) != 0:
                raise RuntimeError(f"WeCom send failed: {result}")

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            query = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.secret})
            with open_url(
                f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?{query}",
                timeout=self.timeout,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
            if int(result.get("errcode", -1)) != 0:
                raise RuntimeError(f"WeCom token failed: {result}")
            self._token = str(result["access_token"])
            self._expires_at = time.time() + int(result.get("expires_in", 7200)) - 300
            return self._token

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with open_url(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
