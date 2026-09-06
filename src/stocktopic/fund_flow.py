from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from typing import Any

from .http import open_url
from .market_clock import MarketClock

SOURCES = ("tushare", "eastmoney", "ths")
VERSION = "moneyflow_v1"


def number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("missing amount")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite amount")
    return result


def direction(value: float | None) -> str:
    return (
        "unknown"
        if value is None
        else "inflow"
        if value > 0
        else "outflow"
        if value < 0
        else "flat"
    )


def normalize(source: str, row: dict, code: str, trade_date: str) -> dict:
    if row.get("ts_code") != code or str(row.get("trade_date")) != trade_date:
        raise ValueError("资金数据代码或交易日不匹配")

    def amount(key):
        return number(row.get(key)) * 10000

    if source == "tushare":
        large = amount("buy_lg_amount") - amount("sell_lg_amount")
        extra = amount("buy_elg_amount") - amount("sell_elg_amount")
        main = large + extra
        basis = "20万≤成交额<100万；超大单≥100万；主力=两档净额之和"
    elif source == "eastmoney":
        large, extra, main = amount("buy_lg_amount"), amount("buy_elg_amount"), amount("net_amount")
        basis = "东方财富原生主力及大小单口径"
    elif source == "ths":
        large, extra, main = None, None, amount("buy_lg_amount")
        basis = "同花顺今日大单净额作为主力方向代理；不提供可比超大单拆分"
    else:
        raise ValueError("unsupported source")
    return dict(
        source=source,
        code=code,
        trade_date=trade_date,
        unit="CNY",
        large_net=large,
        extra_large_net=extra,
        main_net=main,
        direction=direction(main),
        basis=basis,
        raw=row,
        status="available",
    )


def consensus(sources: dict) -> dict:
    directions = {s: direction(sources.get(s, {}).get("main_net")) for s in SOURCES}
    valid = [d for d in directions.values() if d != "unknown"]
    counts = Counter(valid)
    majority, count = counts.most_common(1)[0] if counts else ("unknown", 0)
    tied = len([n for n in counts.values() if n == count]) > 1
    complete = len(valid) == 3
    disagreement = len(counts) > 1
    status = (
        "insufficient"
        if not complete
        else "disputed"
        if disagreement
        else "neutral"
        if majority == "flat"
        else "confirmed"
    )
    return dict(
        score=round(count / 3 * 100, 2),
        available_sources=len(valid),
        directions=directions,
        direction="mixed" if tied else majority,
        unanimous=complete and not disagreement,
        disagreement=disagreement,
        status=status,
        direction_confirmed=status == "confirmed",
        meaning="多源方向交叉验证，不证明交易主体身份或真实主力行为",
    )


def report(sources: dict, trade_date: str, captured_at: str) -> dict:
    base = sources.get("tushare", {})
    return dict(
        version=VERSION,
        trade_date=trade_date,
        captured_at=captured_at,
        unit="CNY",
        sources=sources,
        consensus=consensus(sources),
        large_net=base.get("large_net"),
        extra_large_net=base.get("extra_large_net"),
        main_net=base.get("main_net"),
    )


def aggregate(members: list[dict], reports: dict, trade_date: str, captured_at: str) -> dict:
    codes = list(dict.fromkeys(m["code"] for m in members))
    sources = {}
    for source in SOURCES:
        rows = [reports.get(c, {}).get("sources", {}).get(source, {}) for c in codes]
        available = [r for r in rows if r.get("main_net") is not None]
        complete = bool(codes) and len(available) == len(codes)
        values = {}
        for key in ("large_net", "extra_large_net", "main_net"):
            vals = [r.get(key) for r in rows]
            values[key] = sum(vals) if vals and all(v is not None for v in vals) else None
        sources[source] = dict(
            source=source,
            **values,
            coverage=len(available),
            target_count=len(codes),
            status="available" if complete else "incomplete",
        )
    result = report(sources, trade_date, captured_at)
    valid = [reports.get(c, {}).get("main_net") for c in codes]
    covered = [v for v in valid if v is not None]
    core = [m["code"] for m in members if m.get("leader_rank") == 1]
    core_values = [reports.get(c, {}).get("main_net") for c in core]
    main_direction = direction(result["main_net"])
    result.update(
        member_codes=codes,
        core_codes=core,
        target_count=len(codes),
        covered_count=len(covered),
        inflow_count=sum(v > 0 for v in covered),
        outflow_count=sum(v < 0 for v in covered),
        breadth=round(sum(v > 0 for v in covered) / len(codes) * 100, 2) if codes else None,
        breadth_complete=len(covered) == len(codes),
        core_consistency=(
            sum(direction(v) == main_direction for v in core_values) / len(core_values) * 100
            if core_values
            and all(v is not None for v in core_values)
            and main_direction != "unknown"
            else None
        ),
    )
    return result


class EastmoneyFlowClient:
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"

    def snapshot(self, code: str, now: datetime) -> dict:
        symbol, exchange = code.split(".")
        params = dict(
            secid=f"{1 if exchange == 'SH' else 0}.{symbol}",
            klt=1,
            lmt=0,
            fields1="f1,f2,f3,f7",
            fields2="f51,f52,f53,f54,f55,f56",
            ut="b2884a393a59ad64002292a3e90d46a5",
        )
        request = urllib.request.Request(
            self.endpoint + "?" + urllib.parse.urlencode(params),
            headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
        )
        with open_url(request, timeout=12) as response:
            payload = json.loads(response.read())
        data = payload.get("data") or {}
        if payload.get("rc") != 0 or data.get("code") != symbol or not data.get("klines"):
            raise ValueError("东方财富实时资金字段为空，未通过可用性验证")
        # Provider order: timestamp, main, small, medium, large, extra-large; CNY.
        fields = data["klines"][-1].split(",")
        if len(fields) != 6:
            raise ValueError("东方财富实时字段结构发生变化")
        stamp = MarketClock.normalize(datetime.fromisoformat(fields[0]))
        local = MarketClock.normalize(now)
        if stamp.date() != local.date() or not 0 <= (local - stamp).total_seconds() <= 1200:
            raise ValueError("东方财富实时资金时间戳过期或无效")
        main, small, medium, large, extra = map(number, fields[1:])
        if abs(main - large - extra) > max(1, abs(main) * 0.0001):
            raise ValueError("东方财富主力与大小单字段校验失败")
        return dict(
            source="eastmoney",
            code=code,
            trade_date=local.strftime("%Y%m%d"),
            source_time=stamp.isoformat(),
            main_net=main,
            large_net=large,
            extra_large_net=extra,
            small_net=small,
            medium_net=medium,
            unit="CNY",
            raw=payload,
            status="available",
        )
