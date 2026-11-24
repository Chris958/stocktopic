from __future__ import annotations

from datetime import datetime
from statistics import mean, median
from typing import Any

from .board_status import consecutive_boards
from .db import Database
from .market_feedback import (
    MIN_FEEDBACK_COVERAGE,
    board_tags,
    day_limits,
    day_prices,
    latest_boards,
)
from .market_scope import is_mainboard
from .theme_intelligence import market_regime


def build_market_environment(
    database: Database, trade_date: str, *, as_of: datetime | None = None,
) -> dict[str, Any]:
    """Measure same-day market breadth and the observed outcomes of yesterday's sealed cohort."""
    trade_date = trade_date.replace("-", "")
    previous_day = database.previous_trade_date(trade_date)
    if previous_day and previous_day.replace("-", "") >= trade_date:
        previous_day = None
    with database.connect() as con:
        current_events = latest_boards(con, trade_date)
        quotes = {c: q for c, q in day_prices(con, trade_date, as_of=as_of).items()
                  if is_mainboard(c)}
        limits = day_limits(con, trade_date)
        previous_events = latest_boards(con, previous_day, completed=True) if previous_day else {}
        previous_prices = day_prices(con, previous_day, close_only=True) if previous_day else {}
        previous_limits = day_limits(con, previous_day) if previous_day else {}

    tags = board_tags(quotes, limits, current_events)
    prior_tags = board_tags(previous_prices, previous_limits, previous_events)
    sealed = {code for code, tag in tags.items() if tag == "涨停"}
    failed = {code for code, tag in tags.items() if tag == "炸板"}
    down = {code for code, tag in tags.items() if tag == "跌停"}
    # A stock which touched both limits is still a failed board if it ended at limit-down.
    failed |= {c for c in down & quotes.keys()
               if (upper := limits.get(c, (None, None))[0]) and quotes[c]["high"] >= upper - 0.005}
    heights = {c: consecutive_boards(current_events.get(c, {}).get("status")) for c in sealed}
    yesterday = {code for code, tag in prior_tags.items() if tag == "涨停"}
    prior_multi = {c for c in yesterday
                   if consecutive_boards(previous_events.get(c, {}).get("status")) >= 2}
    observed = yesterday & quotes.keys()
    # Remaining sealed after a previously sealed day is itself evidence of promotion.
    promotion_samples = observed & tags.keys()
    promoted = promotion_samples & sealed
    returns = [quotes[c]["pct_change"] for c in observed]
    severe = {c for c in observed if c in down or quotes[c]["pct_change"] <= -7.0}
    broken = prior_multi & (promotion_samples - sealed)
    broken_returns = [quotes[c]["pct_change"] for c in broken]
    attempts = len(sealed | failed)
    span_both_limits = []
    for code, quote in quotes.items():
        upper, lower = limits.get(code, (None, None))
        if upper and lower and quote["high"] >= upper - 0.005 and quote["low"] <= lower + 0.005:
            span_both_limits.append(code)
    coverage = len(observed) / len(yesterday) if yesterday else None
    ready = coverage is not None and coverage >= MIN_FEEDBACK_COVERAGE
    metrics = {
        "limit_up_count": len(sealed),
        "limit_down_count": len(down),
        "seal_rate": _percent(len(sealed), attempts),
        "failed_rate": _percent(len(failed), attempts),
        "max_board_height": max(heights.values(), default=0),
        "promotion_rate": _percent(len(promoted), len(promotion_samples)),
        "promoted_count": len(promoted),
        "promotion_base_count": len(yesterday),
        "promotion_sample_count": len(promotion_samples),
        "yesterday_limit_return": round(mean(returns), 3) if returns else None,
        "yesterday_limit_sample_count": len(observed),
        "yesterday_limit_expected_count": len(yesterday),
        "feedback_coverage": round(coverage, 4) if coverage is not None else None,
        "feedback_ready": ready,
        "board_trade_return": round(median(returns), 3) if returns else None,
        "board_trade_win_rate": _percent(sum(r > 0 for r in returns), len(observed)),
        "earth_sky_count": len(span_both_limits),
        "earth_sky_codes": sorted(span_both_limits),
        "nuclear_button_count": len(severe) if observed else None,
        "nuclear_button_ratio": _percent(len(severe), len(observed)),
        "nuclear_button_codes": sorted(severe),
        "break_board_count": len(broken),
        "break_board_return": round(mean(broken_returns), 3) if broken_returns else None,
        "break_board_sample_count": len(broken),
        "previous_trade_date": previous_day,
    }
    regime = market_regime(metrics) if ready else {"label": "数据待补", "score": None}
    return {**regime, **metrics, "version": "v3-observed-feedback"}


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None
