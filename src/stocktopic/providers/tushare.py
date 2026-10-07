from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from itertools import count
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..domain import Quote
from ..http import open_url

logger = logging.getLogger(__name__)


class TushareError(RuntimeError):
    def __init__(self, code: int | str, message: str):
        super().__init__(f"Tushare error {code}: {message}")
        self.code = code
        self.message = message


class TushareClient:
    endpoint = "https://api.tushare.pro"
    mcp_protocol_version = "2024-11-05"

    def __init__(self, token: str = "", timeout: float = 30.0, mcp_url: str = ""):
        self.token = token.strip()
        self.timeout = timeout
        # The official token is the production default.  Keeping an old relay URL
        # in .env must not silently route requests through the relay once a direct
        # token has been configured.
        self.mcp_url = "" if self.token else mcp_url.strip()
        self._mcp_session_id = ""
        self._mcp_ready = False
        self._mcp_lock = threading.Lock()
        self._request_ids = count(1)
        if self.mcp_url:
            parsed = urlsplit(self.mcp_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("Tushare MCP URL must be a complete HTTP(S) URL")
        elif not self.token:
            raise ValueError("Tushare MCP URL or token must be configured")

    @property
    def transport(self) -> str:
        return "mcp" if self.mcp_url else "direct"

    def call(
        self,
        api_name: str,
        params: Mapping[str, Any] | None = None,
        fields: str = "",
    ) -> list[dict[str, Any]]:
        if self.mcp_url:
            return self._call_mcp(api_name, params, fields)
        return self._call_direct(api_name, params, fields)

    def _call_direct(
        self,
        api_name: str,
        params: Mapping[str, Any] | None,
        fields: str,
    ) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "api_name": api_name,
                "token": self.token,
                "params": dict(params or {}),
                "fields": fields,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with open_url(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as error:
            raise TushareError("network", str(error)) from error
        code = result.get("code")
        if code != 0:
            raise TushareError(code, str(result.get("msg") or "unknown error"))
        data = result.get("data") or {}
        field_names = data.get("fields") or []
        items = data.get("items") or []
        return [dict(zip(field_names, values, strict=False)) for values in items]

    def _call_mcp(
        self,
        api_name: str,
        params: Mapping[str, Any] | None,
        fields: str,
    ) -> list[dict[str, Any]]:
        self._ensure_mcp_initialized()
        arguments = dict(params or {})
        if "ts_code" in arguments:
            arguments["symbol"] = arguments.pop("ts_code")

        requested_fields = [value.strip() for value in fields.split(",") if value.strip()]
        mcp_fields = list(dict.fromkeys(requested_fields))
        if api_name == "stock_basic" and "ts_code" in mcp_fields:
            # The relay exposes both native fields under one `symbol` alias. Asking
            # for both loses the exchange-qualified ts_code value.
            mcp_fields = [value for value in mcp_fields if value != "symbol"]
        if mcp_fields:
            arguments["fields"] = mcp_fields

        response = self._mcp_request(
            "tools/call",
            {"name": api_name, "arguments": arguments},
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise TushareError("mcp_protocol", "MCP relay returned no tool result")
        content = self._mcp_content(result)
        if result.get("isError"):
            message = _mcp_error_text(content) or "tool call failed"
            raise TushareError("mcp_tool", _redact_secret_text(message, self.mcp_url))
        envelope = _mcp_envelope(content)
        code = envelope.get("code")
        if code != 0:
            message = _redact_secret_text(
                str(envelope.get("msg") or "unknown error"),
                self.mcp_url,
            )
            raise TushareError(code, message)
        data = envelope.get("data") or {}
        if not isinstance(data, Mapping):
            raise TushareError("mcp_protocol", "MCP Tushare data is not an object")
        field_names = data.get("fields") or []
        items = data.get("items") or []
        rows = [dict(zip(field_names, values, strict=False)) for values in items]
        return _restore_mcp_rows(api_name, rows, requested_fields, params or {})

    def _ensure_mcp_initialized(self) -> None:
        if self._mcp_ready:
            return
        with self._mcp_lock:
            if self._mcp_ready:
                return
            response, session_id = self._mcp_post(
                {
                    "jsonrpc": "2.0",
                    "id": next(self._request_ids),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self.mcp_protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "stocktopic", "version": "1"},
                    },
                }
            )
            if "error" in response:
                raise _mcp_rpc_error(response["error"])
            if not isinstance(response.get("result"), Mapping):
                raise TushareError("mcp_protocol", "MCP relay initialization failed")
            self._mcp_session_id = session_id
            self._mcp_post(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                expect_response=False,
            )
            self._mcp_ready = True

    def _mcp_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._request_ids),
            "method": method,
            "params": dict(params),
        }
        response, _ = self._mcp_post(payload)
        if "error" in response:
            raise _mcp_rpc_error(response["error"])
        return response

    def _mcp_post(
        self,
        payload: Mapping[str, Any],
        *,
        expect_response: bool = True,
    ) -> tuple[dict[str, Any], str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json; charset=utf-8",
        }
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        request = urllib.request.Request(
            self.mcp_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with open_url(request, timeout=self.timeout) as response:
                body = response.read()
                session_id = response.headers.get("Mcp-Session-Id", self._mcp_session_id)
                if not body and not expect_response:
                    return {}, session_id
                content_type = response.headers.get("Content-Type", "")
                return _decode_mcp_response(body, content_type), session_id
        except urllib.error.HTTPError as error:
            message = f"MCP relay rejected the request (HTTP {error.code})"
            raise TushareError(f"mcp_http_{error.code}", message) from error
        except (urllib.error.URLError, TimeoutError) as error:
            message = _redact_secret_text(str(error), self.mcp_url)
            raise TushareError("network", f"MCP relay unavailable: {message}") from error
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as error:
            message = _redact_secret_text(str(error), self.mcp_url)
            raise TushareError("mcp_protocol", f"Invalid MCP relay response: {message}") from error

    @staticmethod
    def _mcp_content(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        content = result.get("content") or []
        if not isinstance(content, list):
            raise TushareError("mcp_protocol", "MCP tool content is not a list")
        return [item for item in content if isinstance(item, Mapping)]

    def realtime_quotes(self, captured_at: datetime) -> list[Quote]:
        rows = self.call(
            "rt_k",
            {"ts_code": "6*.SH,0*.SZ,3*.SZ"},
            "ts_code,name,pre_close,high,open,low,close,vol,amount,num,trade_time",
        )
        quotes: list[Quote] = []
        for row in rows:
            code = str(row.get("ts_code") or "")
            if not code:
                continue
            quotes.append(
                Quote(
                    code=code,
                    name=str(row.get("name") or ""),
                    pre_close=_float(row.get("pre_close")),
                    high=_float(row.get("high")),
                    open=_float(row.get("open")),
                    low=_float(row.get("low")),
                    close=_float(row.get("close")),
                    volume=_int(row.get("vol")),
                    amount=_float(row.get("amount")),
                    trades=_int(row.get("num")),
                    trade_time=str(row.get("trade_time") or ""),
                    captured_at=captured_at,
                )
            )
        return quotes

    def stock_basic(self) -> list[dict[str, Any]]:
        return self.call(
            "stock_basic",
            {"exchange": "", "list_status": "L"},
            "ts_code,symbol,name,area,industry,market,list_date,exchange",
        )

    def trade_calendar(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self.call(
            "trade_cal",
            {"exchange": "SSE", "start_date": start_date, "end_date": end_date},
            "exchange,cal_date,is_open,pretrade_date",
        )

    def stock_limits(self, trade_date: str) -> list[dict[str, Any]]:
        return self.call(
            "stk_limit",
            {"trade_date": trade_date},
            "trade_date,ts_code,pre_close,up_limit,down_limit",
        )

    def daily_basic(self, trade_date: str) -> list[dict[str, Any]]:
        return self.call(
            "daily_basic",
            {"trade_date": trade_date},
            ("ts_code,trade_date,close,turnover_rate,volume_ratio,float_share,total_mv,circ_mv"),
        )

    def daily_prices(self, trade_date: str) -> list[dict[str, Any]]:
        return self.call(
            "daily",
            {"trade_date": trade_date},
            (
                "ts_code,trade_date,open,high,low,close,pre_close,"
                "change,pct_chg,vol,amount"
            ),
        )

    def kpl_list(self, trade_date: str, tag: str) -> list[dict[str, Any]]:
        return self.call(
            "kpl_list",
            {"trade_date": trade_date, "tag": tag},
            (
                "ts_code,name,trade_date,lu_time,ld_time,open_time,last_time,lu_desc,"
                "tag,theme,status,pct_chg,rt_pct_chg,amount,turnover_rate,limit_order,"
                "net_change,bid_amount,bid_change,bid_turnover,lu_bid_vol,free_float,"
                "lu_limit_order"
            ),
        )

    def kpl_concept_members(self, trade_date: str) -> list[dict[str, Any]]:
        """Return a normalized multi-source concept graph for one trading day.

        KPL is the baseline. Eastmoney and TDX permission errors can degrade
        independently, but network failures are re-raised so the service retries the
        whole graph rather than silently persisting a partial snapshot.
        """
        rows = self._paged_call(
            "kpl_concept_cons",
            {"trade_date": trade_date},
            "ts_code,name,con_name,con_code,trade_date,desc,hot_num",
            page_size=3000,
            max_pages=8,
        )
        normalized = [dict(row) for row in rows]

        for loader_name, loader in (
            ("dc_concept", self._normalized_dc_concepts),
            ("tdx_concept", self._normalized_tdx_concepts),
        ):
            try:
                normalized.extend(loader(trade_date))
            except TushareError as error:
                if str(error.code) == "network":
                    raise
                logger.warning("Optional theme graph source %s degraded: %s", loader_name, error)

        return _dedupe_graph_rows(normalized)

    def dc_concept_members(self, trade_date: str, ts_code: str = "") -> list[dict[str, Any]]:
        """Daily Eastmoney concept-theme edges, available from 2026-02-03."""
        params: dict[str, Any] = {"trade_date": trade_date}
        if ts_code:
            params["ts_code"] = ts_code
        return self._paged_call(
            "dc_concept_cons",
            params,
            "ts_code,trade_date,name,theme_code,industry_code,industry,reason,hot_num",
            page_size=3000,
            max_pages=8,
        )

    def tdx_members(self, trade_date: str, board_code: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"trade_date": trade_date}
        if board_code:
            params["ts_code"] = board_code
        return self._paged_call(
            "tdx_member",
            params,
            "ts_code,trade_date,con_code,con_name",
            page_size=3000,
            max_pages=12,
        )

    def sw_industry_members(self, ts_code: str) -> list[dict[str, Any]]:
        return self.call(
            "index_member_all",
            {"ts_code": ts_code, "is_new": "Y"},
            "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,is_new",
        )

    def citic_industry_members(self, ts_code: str) -> list[dict[str, Any]]:
        return self.call(
            "ci_index_member",
            {"ts_code": ts_code, "is_new": "Y"},
            "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,is_new",
        )

    def _normalized_dc_concepts(self, trade_date: str) -> list[dict[str, Any]]:
        concept_rows = self._paged_call(
            "dc_concept",
            {"trade_date": trade_date},
            "theme_code,trade_date,name",
            page_size=2000,
            max_pages=4,
        )
        names = {
            str(row.get("theme_code") or "").strip(): str(row.get("name") or "").strip()
            for row in concept_rows
            if str(row.get("theme_code") or "").strip()
        }
        member_rows = self.dc_concept_members(trade_date)
        result = []
        for row in member_rows:
            stock_code = str(row.get("ts_code") or "").strip()
            theme_code = str(row.get("theme_code") or "").strip()
            if not stock_code or not theme_code:
                continue
            theme_name = names.get(theme_code) or str(row.get("industry") or "").strip()
            if not theme_name:
                theme_name = theme_code
            result.append(
                {
                    "ts_code": f"DC:{theme_code}",
                    "name": theme_name,
                    "con_name": str(row.get("name") or stock_code),
                    "con_code": stock_code,
                    "trade_date": str(row.get("trade_date") or trade_date),
                    "desc": str(row.get("reason") or "")[:800],
                    "hot_num": _int(row.get("hot_num")),
                    "graph_source": "dc_concept",
                }
            )
        return result

    def _normalized_tdx_concepts(self, trade_date: str) -> list[dict[str, Any]]:
        board_rows = self._paged_call(
            "tdx_index",
            {"trade_date": trade_date},
            "ts_code,trade_date,name,idx_type,idx_count",
            page_size=1000,
            max_pages=4,
        )
        concept_names = {
            str(row.get("ts_code") or "").strip(): str(row.get("name") or "").strip()
            for row in board_rows
            if str(row.get("ts_code") or "").strip()
            and "概念" in str(row.get("idx_type") or "")
        }
        if not concept_names:
            return []
        member_rows = self.tdx_members(trade_date)
        result = []
        for row in member_rows:
            board_code = str(row.get("ts_code") or "").strip()
            stock_code = str(row.get("con_code") or "").strip()
            if board_code not in concept_names or not stock_code:
                continue
            result.append(
                {
                    "ts_code": f"TDX:{board_code}",
                    "name": concept_names[board_code],
                    "con_name": str(row.get("con_name") or stock_code),
                    "con_code": stock_code,
                    "trade_date": str(row.get("trade_date") or trade_date),
                    "desc": "通达信概念板块结构化成分",
                    "hot_num": 0,
                    "graph_source": "tdx_concept",
                }
            )
        return result

    def _paged_call(
        self,
        api_name: str,
        params: Mapping[str, Any],
        fields: str,
        *,
        page_size: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Page Tushare list APIs while safely handling endpoints that ignore offset."""
        rows: list[dict[str, Any]] = []
        previous_signature: tuple[str, str, int] | None = None
        offset = 0
        for _ in range(max_pages):
            page_params = dict(params)
            page_params["limit"] = page_size
            page_params["offset"] = offset
            page = self.call(api_name, page_params, fields)
            if not page:
                break
            signature = (
                json.dumps(page[0], sort_keys=True, ensure_ascii=False, default=str),
                json.dumps(page[-1], sort_keys=True, ensure_ascii=False, default=str),
                len(page),
            )
            if signature == previous_signature:
                break
            previous_signature = signature
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return rows


_STOCK_CODE_APIS = {
    "ci_index_member",
    "daily",
    "daily_basic",
    "dc_concept_cons",
    "index_member_all",
    "kpl_list",
    "moneyflow",
    "moneyflow_dc",
    "moneyflow_ths",
    "rt_k",
    "stk_limit",
    "stock_basic",
}


def _decode_mcp_response(body: bytes, content_type: str) -> dict[str, Any]:
    text = body.decode("utf-8")
    if "text/event-stream" not in content_type.lower():
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("MCP JSON-RPC response is not an object")
        return value

    events: list[str] = []
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line:
            if data_lines:
                events.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        events.append("\n".join(data_lines))
    for event in events:
        value = json.loads(event)
        if isinstance(value, dict) and ("result" in value or "error" in value):
            return value
    raise ValueError("MCP event stream contains no JSON-RPC response")


def _mcp_rpc_error(value: Any) -> TushareError:
    if isinstance(value, Mapping):
        code = value.get("code", "unknown")
        message = value.get("message") or "unknown JSON-RPC error"
    else:
        code = "unknown"
        message = value
    return TushareError(f"mcp_rpc_{code}", _redact_secret_text(str(message), ""))


def _mcp_error_text(content: list[Mapping[str, Any]]) -> str:
    return "; ".join(
        str(item.get("text") or "").strip()
        for item in content
        if item.get("type") == "text" and str(item.get("text") or "").strip()
    )


def _mcp_envelope(content: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    for item in content:
        if item.get("type") != "text":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    raise TushareError("mcp_protocol", "MCP tool returned no Tushare data envelope")


def _restore_mcp_rows(
    api_name: str,
    rows: list[dict[str, Any]],
    requested_fields: list[str],
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if "ts_code" not in requested_fields:
        return rows
    requested_code = str(params.get("ts_code") or "").strip()
    can_restore_requested = requested_code and not any(
        marker in requested_code for marker in ("*", ",")
    )
    for row in rows:
        value = row.pop("symbol", row.get("ts_code"))
        if not value and can_restore_requested:
            value = requested_code
        code = str(value or "").strip()
        if api_name in _STOCK_CODE_APIS:
            code = _qualified_stock_code(code, str(row.get("exchange") or ""))
        row["ts_code"] = code
        if api_name == "stock_basic":
            row["symbol"] = code.split(".", 1)[0]
    return rows


def _qualified_stock_code(value: str, exchange: str = "") -> str:
    if not value or "." in value:
        return value
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange.upper(), "")
    if not suffix and len(value) == 6 and value.isdigit():
        if value.startswith("6"):
            suffix = "SH"
        elif value.startswith(("0", "3")):
            suffix = "SZ"
        elif value.startswith(("4", "8", "9")):
            suffix = "BJ"
    return f"{value}.{suffix}" if suffix else value


def _redact_secret_text(message: str, secret_url: str) -> str:
    result = message
    if secret_url:
        parsed = urlsplit(secret_url)
        redacted = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))
        result = result.replace(secret_url, redacted)
    return re.sub(
        r"(?i)([?&](?:token|key|api[_-]?key)=)[^&\s'\"]+",
        r"\1<redacted>",
        result,
    )


def _dedupe_graph_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("trade_date") or ""),
            str(row.get("ts_code") or ""),
            str(row.get("con_code") or ""),
        )
        if not key[1] or not key[2] or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
