from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from typing import Any

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

    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token.strip()
        self.timeout = timeout
        if not self.token:
            raise ValueError("Tushare token cannot be empty")

    def call(
        self,
        api_name: str,
        params: Mapping[str, Any] | None = None,
        fields: str = "",
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

        The service already syncs this method once per reference day. We therefore
        reuse that existing job to persist KPL + Eastmoney + TDX concept relations
        into stock_tags/kpl_concept_memberships without adding a new scheduler.
        Optional graph sources degrade independently so KPL remains the baseline.
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
