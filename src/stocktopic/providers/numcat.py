from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..http import open_url


class NumcatError(RuntimeError):
    def __init__(self, code: int | str, message: str, *, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"猫爪数据错误 {code}：{message}")


class NumcatClient:
    """Small client for the public HTTPS Cat Data API gateway."""

    endpoint = "https://numcat.net/api"
    trade_fields = (
        "symbol,tradedate,time,trade_id,price,volume,amount,bs_flag,trade_code,"
        "buy_order_id,sell_order_id"
    )
    order_fields = (
        "symbol,tradedate,time,order_id,price,volume,amount,side,order_type,order_no"
    )

    def __init__(self, api_key: str, timeout: float = 45.0):
        self.api_key = api_key.strip()
        self.timeout = timeout
        _validate_endpoint(self.endpoint)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def trade_history(
        self,
        symbol: str,
        trade_date: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._history(
            "level2_trade_history",
            symbol,
            trade_date,
            self.trade_fields,
            start_time=start_time,
            end_time=end_time,
        )

    def order_history(
        self,
        symbol: str,
        trade_date: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._history(
            "level2_order_history",
            symbol,
            trade_date,
            self.order_fields,
            start_time=start_time,
            end_time=end_time,
        )

    def _history(
        self,
        api_name: str,
        symbol: str,
        trade_date: str,
        fields: str,
        *,
        start_time: str | None,
        end_time: str | None,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            raise NumcatError("not_configured", "缺少NUMCAT_API_KEY")
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(200):
            params: dict[str, Any] = {
                "symbol": symbol,
                "tradedate": trade_date,
                "page_size": 50000,
            }
            if start_time:
                params["start_time"] = start_time
            if end_time:
                params["end_time"] = end_time
            if cursor:
                params["cursor"] = cursor
            page = self._call_page(api_name, params, fields)
            rows.extend(page["items"])
            next_cursor = page.get("next_cursor")
            if not page.get("has_more") or not next_cursor:
                return rows
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise NumcatError("pagination", "接口返回了重复游标，已停止以避免死循环")
            seen_cursors.add(cursor)
        raise NumcatError("pagination", "逐笔数据超过200页，已停止以避免异常请求")

    def _call_page(
        self,
        api_name: str,
        params: Mapping[str, Any],
        fields: str,
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "apiname": api_name,
                "apikey": self.api_key,
                "fields": fields,
                "params": dict(params),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with open_url(request, timeout=self.timeout) as response:
                    value = json.loads(response.read().decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("response is not a JSON object")
                result = value
                break
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code < 500 and error.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
                last_error = error
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
        if result is None:
            raise NumcatError(
                "network",
                str(last_error or "unknown network error"),
                retryable=True,
            ) from last_error
        code = result.get("code")
        if str(code) not in {"0", "200"}:
            raise NumcatError(code or "unknown", str(result.get("message") or "unknown error"))
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise NumcatError("schema", "data不是对象")
        field_names = data.get("fields") or []
        items = data.get("items") or []
        if not isinstance(field_names, list) or not isinstance(items, list):
            raise NumcatError("schema", "fields或items格式错误")
        return {
            "items": [
                dict(zip(field_names, values, strict=False))
                for values in items
                if isinstance(values, list)
            ],
            "has_more": bool(data.get("has_more")),
            "next_cursor": data.get("next_cursor"),
        }


def _validate_endpoint(value: str) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("猫爪数据接口地址无效") from error
    if not (
        parsed.scheme == "https"
        and parsed.hostname in {"numcat.net", "www.numcat.net"}
        and port in {None, 443}
        and parsed.path == "/api"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        raise ValueError("猫爪数据必须使用官方HTTPS接口 https://numcat.net/api")
