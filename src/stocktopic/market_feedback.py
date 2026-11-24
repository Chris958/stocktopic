"""Shared, dated market evidence for negative feedback and the leader watchlist."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from math import isfinite

from .market_clock import MarketClock
from .market_scope import is_mainboard

PRICE_TOLERANCE = 0.005
MIN_FEEDBACK_COVERAGE = 0.8
MAX_SAMPLE_GAP = timedelta(minutes=7)


def date_forms(day: str) -> tuple[str, str]:
    compact = day.replace("-", "")
    return compact, f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def price_board(price: dict, limits: tuple[float | None, float | None]) -> str | None:
    """Classify from actual prices when valid limits exist, otherwise return unknown."""
    upper, lower = limits
    if lower and price["close"] <= lower + PRICE_TOLERANCE:
        return "跌停"
    if not upper or upper <= 0:
        return None
    if price["close"] >= upper - PRICE_TOLERANCE:
        return "涨停"
    if price["high"] >= upper - PRICE_TOLERANCE:
        return "炸板"
    return "普通"


def fresh_trade_sample(now: datetime, captured: datetime, provider_time: str = "") -> bool:
    """Reject stale explicit provider timestamps as well as stale collection timestamps."""
    if not timedelta(0) <= now - captured <= MAX_SAMPLE_GAP:
        return False
    if captured.date() != now.date() or MarketClock.session_name(captured) != (
        MarketClock.session_name(now)
    ):
        return False
    if not provider_time:
        return True
    try:
        source = MarketClock.normalize(datetime.fromisoformat(provider_time))
    except ValueError:
        try:
            source = MarketClock.normalize(datetime.combine(now.date(), time.fromisoformat(
                provider_time,
            )))
        except ValueError:
            return True  # The provider time is optional; use the known capture time.
    return source.date() == now.date() and -timedelta(seconds=20) <= now - source <= MAX_SAMPLE_GAP


def latest_boards(con, day: str, *, completed: bool = False) -> dict:
    result = {}
    for row in con.execute(
        "SELECT code,board_tag,status,synced_at FROM kpl_events "
        "WHERE trade_date=? AND board_tag IN ('涨停','炸板','跌停') "
        "AND (NOT ? OR COALESCE(json_extract(raw_json,'$.source'),'')!='local_rt_k_limit') "
        "ORDER BY synced_at DESC,CASE board_tag WHEN '跌停' THEN 0 WHEN '炸板' THEN 1 ELSE 2 END",
        (date_forms(day)[0], completed),
    ):
        if is_mainboard(row["code"]):
            result.setdefault(row["code"], dict(row))
    return result


def day_limits(con, day: str) -> dict:
    return {row["code"]: (row["upper_limit"], row["lower_limit"]) for row in con.execute(
        "SELECT code,upper_limit,lower_limit FROM daily_limits WHERE trade_date IN (?,?) "
        "ORDER BY length(trade_date)", date_forms(day),
    )}


def day_prices(con, day: str, *, as_of: datetime | None = None, close_only=False) -> dict:
    """Read dated prices, preferring the official final bar once it is available."""
    compact, iso = date_forms(day)
    local = MarketClock.normalize(as_of) if as_of else None
    if local and compact > local.strftime("%Y%m%d"):
        return {}
    live = bool(
        local and compact == local.strftime("%Y%m%d")
        and MarketClock.session_name(local) in {"morning", "afternoon"}
        and (local.hour, local.minute) < (15, 0)
    )
    # Trading slots are stored as HH:MM. A closing sample must be from the closing slot.
    end = local.strftime("%H:%M") if local and compact == local.strftime("%Y%m%d") else "15:00"
    start = max("09:30", (local - timedelta(minutes=7)).strftime("%H:%M")) if live else "09:30"
    params = (compact, iso, "15:00" if close_only else start, min(end, "15:00"))
    rows = con.execute("""
        WITH ranked AS (
            SELECT code,close,high,low,pre_close,volume,captured_at,provider_trade_time,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY captured_at DESC) AS rn
            FROM quote_snapshots WHERE trade_date IN (?,?) AND slot BETWEEN ? AND ?
        )
        SELECT * FROM ranked WHERE rn=1
    """, params)
    result = {}
    for row in rows:
        if not _valid_price(row):
            continue
        if close_only:
            try:
                when = MarketClock.normalize(datetime.fromisoformat(row["captured_at"]))
            except (TypeError, ValueError):
                continue
            if when.strftime("%Y%m%d") != compact or (when.hour, when.minute) < (15, 0):
                continue
        if live and local:
            try:
                when = MarketClock.normalize(datetime.fromisoformat(row["captured_at"]))
            except (TypeError, ValueError):
                continue
            if not fresh_trade_sample(local, when, row["provider_trade_time"] or ""):
                continue
        result[row["code"]] = _quote_return(row, "snapshot")
    if not live and (local is None or compact < local.strftime("%Y%m%d") or end >= "15:00"):
        for row in con.execute(
            "SELECT code,close,high,low,pre_close,volume FROM stock_daily_bars "
            "WHERE trade_date=?", (compact,),
        ):
            if _valid_price(row):
                result[row["code"]] = _quote_return(row, "official")
    return result


def board_tags(quotes, limits, events):
    tags = {code: row["board_tag"] for code, row in events.items()}
    for code, quote in quotes.items():
        if is_mainboard(code):
            tag = price_board(quote, limits.get(code, (None, None)))
            if tag is not None:
                tags[code] = tag
    return tags


def _valid_price(row) -> bool:
    return (
        row["close"] > 0 and row["pre_close"] > 0 and row["volume"] > 0
        and isfinite(row["close"]) and isfinite(row["pre_close"])
    )


def _quote_return(row, source: str) -> dict:
    return {**dict(row), "source": source,
            "pct_change": round((row["close"] / row["pre_close"] - 1) * 100, 6)}
