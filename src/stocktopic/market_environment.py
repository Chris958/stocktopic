from __future__ import annotations

from statistics import mean, median
from typing import Any

from .db import Database
from .theme_intelligence import market_regime

POSITIVE_BOARD_TAGS = {"涨停", "创业板涨幅超10%"}


def build_market_environment(database: Database, trade_date: str) -> dict[str, Any]:
    """Build market-environment V2 from deterministic market data.

    Definitions:
    - promotion_rate: yesterday's sealed boards that remain sealed today with board height >= 2.
    - yesterday_limit_return: mean current return of yesterday's sealed boards.
    - board_trade_return: median current return of yesterday's sealed boards (board buyer P/L proxy).
    - earth_sky_count: intraday high touched upper limit and low touched lower limit.
    - nuclear_button: yesterday's sealed boards currently <= -7% or at limit down.
    - break_board_return: mean current return of yesterday's 2+ board stocks that failed to seal today.
    """
    trade_date = str(trade_date).replace("-", "")
    previous_trade_date = database.previous_trade_date(trade_date)

    with database.connect() as connection:
        current_rows = connection.execute(
            """
            SELECT code, board_tag, status
            FROM kpl_events
            WHERE trade_date=?
            """,
            (trade_date,),
        ).fetchall()
        previous_rows = (
            connection.execute(
                """
                SELECT code, board_tag, status
                FROM kpl_events
                WHERE trade_date=?
                """,
                (previous_trade_date,),
            ).fetchall()
            if previous_trade_date
            else []
        )

        quote_rows = connection.execute(
            """
            WITH ranked AS (
                SELECT code, pct_change, high, low, close, pre_close,
                       ROW_NUMBER() OVER (
                           PARTITION BY code ORDER BY captured_at DESC
                       ) AS rn
                FROM quote_snapshots
                WHERE trade_date=?
            )
            SELECT code, pct_change, high, low, close, pre_close
            FROM ranked
            WHERE rn=1
            """,
            (trade_date,),
        ).fetchall()

        daily_rows = connection.execute(
            """
            SELECT code, pct_change, high, low, close, pre_close
            FROM stock_daily_bars
            WHERE trade_date=?
            """,
            (trade_date,),
        ).fetchall()

        limit_rows = connection.execute(
            """
            SELECT code, upper_limit, lower_limit
            FROM daily_limits
            WHERE trade_date=?
            """,
            (trade_date,),
        ).fetchall()

    current_events = [dict(row) for row in current_rows]
    previous_events = [dict(row) for row in previous_rows]

    current_limit_codes = {
        str(row["code"])
        for row in current_events
        if str(row.get("board_tag") or "") in POSITIVE_BOARD_TAGS
    }
    failed_codes = {
        str(row["code"])
        for row in current_events
        if str(row.get("board_tag") or "") == "炸板"
    }
    failed_only_codes = failed_codes - current_limit_codes
    limit_down_codes = {
        str(row["code"])
        for row in current_events
        if str(row.get("board_tag") or "") == "跌停"
    }

    current_height: dict[str, int] = {}
    for row in current_events:
        if str(row.get("board_tag") or "") not in POSITIVE_BOARD_TAGS:
            continue
        code = str(row["code"])
        current_height[code] = max(current_height.get(code, 0), _board_height(row.get("status")))

    previous_limit_codes = {
        str(row["code"])
        for row in previous_events
        if str(row.get("board_tag") or "") in POSITIVE_BOARD_TAGS
    }
    previous_height: dict[str, int] = {}
    for row in previous_events:
        if str(row.get("board_tag") or "") not in POSITIVE_BOARD_TAGS:
            continue
        code = str(row["code"])
        previous_height[code] = max(previous_height.get(code, 0), _board_height(row.get("status")))
    previous_multi_board_codes = {
        code for code, height in previous_height.items() if height >= 2
    }

    quote_map = {str(row["code"]): dict(row) for row in daily_rows}
    quote_map.update({str(row["code"]): dict(row) for row in quote_rows})
    limit_map = {str(row["code"]): dict(row) for row in limit_rows}

    attempts = len(current_limit_codes | failed_codes)
    seal_rate = len(current_limit_codes) / attempts * 100 if attempts else 0.0
    failed_rate = len(failed_only_codes) / attempts * 100 if attempts else 0.0

    promoted_codes = {
        code
        for code in previous_limit_codes
        if code in current_limit_codes and current_height.get(code, 0) >= 2
    }
    promotion_rate = (
        len(promoted_codes) / len(previous_limit_codes) * 100
        if previous_limit_codes
        else 0.0
    )

    yesterday_returns = [
        _float(quote_map[code].get("pct_change"))
        for code in previous_limit_codes
        if code in quote_map
    ]
    yesterday_limit_return = mean(yesterday_returns) if yesterday_returns else 0.0
    board_trade_return = median(yesterday_returns) if yesterday_returns else 0.0
    board_trade_win_rate = (
        sum(value > 0 for value in yesterday_returns) / len(yesterday_returns) * 100
        if yesterday_returns
        else 0.0
    )

    nuclear_button_codes = {
        code
        for code in previous_limit_codes
        if code in limit_down_codes
        or (code in quote_map and _float(quote_map[code].get("pct_change")) <= -7.0)
    }
    nuclear_button_ratio = (
        len(nuclear_button_codes) / len(previous_limit_codes) * 100
        if previous_limit_codes
        else 0.0
    )

    broken_codes = previous_multi_board_codes - current_limit_codes
    broken_returns = [
        _float(quote_map[code].get("pct_change"))
        for code in broken_codes
        if code in quote_map
    ]
    break_board_return = mean(broken_returns) if broken_returns else 0.0

    earth_sky_codes: list[str] = []
    for code, quote in quote_map.items():
        limits = limit_map.get(code)
        if not limits:
            continue
        upper = _float(limits.get("upper_limit"))
        lower = _float(limits.get("lower_limit"))
        high = _float(quote.get("high"))
        low = _float(quote.get("low"))
        if upper > 0 and lower > 0 and high >= upper * 0.998 and low <= lower * 1.002:
            earth_sky_codes.append(code)

    metrics = {
        "limit_up_count": len(current_limit_codes),
        "limit_down_count": len(limit_down_codes),
        "seal_rate": round(seal_rate, 2),
        "failed_rate": round(failed_rate, 2),
        "max_board_height": max(current_height.values(), default=0),
        "promotion_rate": round(promotion_rate, 2),
        "promoted_count": len(promoted_codes),
        "promotion_base_count": len(previous_limit_codes),
        "yesterday_limit_return": round(yesterday_limit_return, 3),
        "yesterday_limit_sample_count": len(yesterday_returns),
        "board_trade_return": round(board_trade_return, 3),
        "board_trade_win_rate": round(board_trade_win_rate, 2),
        "earth_sky_count": len(earth_sky_codes),
        "earth_sky_codes": sorted(earth_sky_codes),
        "nuclear_button_count": len(nuclear_button_codes),
        "nuclear_button_ratio": round(nuclear_button_ratio, 2),
        "nuclear_button_codes": sorted(nuclear_button_codes),
        "break_board_count": len(broken_codes),
        "break_board_return": round(break_board_return, 3),
        "break_board_sample_count": len(broken_returns),
        "previous_trade_date": previous_trade_date,
    }
    return {
        **market_regime(metrics),
        **metrics,
        "version": "v2-real-market-breadth",
    }


def _board_height(status: Any) -> int:
    value = str(status or "")
    if "首板" in value:
        return 1
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
