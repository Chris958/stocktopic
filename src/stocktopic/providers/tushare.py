from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..domain import Quote
from ..http import open_url


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
            {"ts_code": "6*.SH,0*.SZ"},
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
        return self.call(
            "kpl_concept_cons",
            {"trade_date": trade_date},
            "ts_code,name,con_name,con_code,trade_date,desc,hot_num",
        )


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
