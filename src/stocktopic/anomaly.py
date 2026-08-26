from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .domain import Anomaly, Direction, Quote, StockContext


@dataclass(frozen=True, slots=True)
class DetectorThresholds:
    strong_pct: float = 5.0
    rapid_5m_pct: float = 2.0
    hard_rapid_5m_pct: float = 4.0
    negative_pct: float = -5.0
    hard_negative_pct: float = -7.0
    amount_delta: float = 30_000_000.0
    hard_amount_delta: float = 50_000_000.0
    acceleration_ratio: float = 2.0
    minimum_trade_delta: int = 300
    limit_tolerance: float = 0.005


class AnomalyDetector:
    """Balanced detector: hard events enter directly; normal stocks need two conditions."""

    def __init__(self, thresholds: DetectorThresholds | None = None):
        self.thresholds = thresholds or DetectorThresholds()

    def detect(
        self,
        quote: Quote,
        context: StockContext,
        history: list[dict[str, Any]],
    ) -> list[Anomaly]:
        previous, prior = _history_pair(history, quote.captured_at)
        change_5m = _price_change(quote.close, previous.get("close") if previous else None)
        amount_delta = (
            max(0.0, quote.amount - float(previous.get("amount", 0))) if previous else 0.0
        )
        trade_delta = max(0, quote.trades - int(previous.get("trades", 0))) if previous else 0
        previous_amount_delta = _interval_delta(previous, prior, "amount")
        previous_trade_delta = _interval_delta(previous, prior, "trades")
        amount_acceleration = _ratio(amount_delta, previous_amount_delta)
        trade_acceleration = _ratio(float(trade_delta), previous_trade_delta)

        common_metrics = {
            "upper_limit": context.upper_limit,
            "lower_limit": context.lower_limit,
            "amount_acceleration": round(amount_acceleration, 3),
            "trade_acceleration": round(trade_acceleration, 3),
            "provider_trade_time": quote.trade_time,
        }
        events: list[Anomaly] = []
        positive = self._positive(
            quote,
            context,
            change_5m,
            amount_delta,
            trade_delta,
            amount_acceleration,
            trade_acceleration,
            common_metrics,
        )
        if positive:
            events.append(positive)
        negative = self._negative(
            quote,
            context,
            change_5m,
            amount_delta,
            trade_delta,
            common_metrics,
        )
        if negative:
            events.append(negative)
        return events

    def _positive(
        self,
        quote: Quote,
        context: StockContext,
        change_5m: float,
        amount_delta: float,
        trade_delta: int,
        amount_acceleration: float,
        trade_acceleration: float,
        common_metrics: dict[str, Any],
    ) -> Anomaly | None:
        t = self.thresholds
        reasons: list[str] = []
        event_types: list[str] = []
        hard = False
        at_limit = _at_or_above(quote.close, context.upper_limit, t.limit_tolerance)
        touched_limit = _at_or_above(quote.high, context.upper_limit, t.limit_tolerance)
        if at_limit:
            hard = True
            event_types.append("limit_up")
            reasons.append("当前价格封住涨停")
        elif touched_limit:
            event_types.append("touched_limit")

        if change_5m >= t.hard_rapid_5m_pct and amount_delta >= t.hard_amount_delta:
            hard = True
            event_types.append("hard_rapid_rise")
            reasons.append(f"5分钟上涨{change_5m:.2f}%且增量成交额较大")

        conditions = 0
        if quote.pct_change >= t.strong_pct:
            conditions += 1
            event_types.append("strong_gain")
            reasons.append(f"当日涨幅{quote.pct_change:.2f}%")
        if change_5m >= t.rapid_5m_pct:
            conditions += 1
            event_types.append("rapid_rise")
            reasons.append(f"5分钟上涨{change_5m:.2f}%")
        if amount_delta >= t.amount_delta and amount_acceleration >= t.acceleration_ratio:
            conditions += 1
            event_types.append("amount_acceleration")
            reasons.append(f"成交额增速达到前一周期的{amount_acceleration:.1f}倍")
        if trade_delta >= t.minimum_trade_delta and trade_acceleration >= t.acceleration_ratio:
            conditions += 1
            event_types.append("trade_acceleration")
            reasons.append(f"成交笔数增速达到前一周期的{trade_acceleration:.1f}倍")
        if not hard and conditions < 2:
            return None
        severity = min(
            100.0,
            35.0
            + max(0.0, quote.pct_change) * 3.0
            + max(0.0, change_5m) * 4.0
            + (15.0 if at_limit else 0.0),
        )
        return Anomaly(
            code=quote.code,
            name=quote.name,
            captured_at=quote.captured_at,
            direction=Direction.POSITIVE,
            severity=round(severity, 2),
            pct_change=round(quote.pct_change, 3),
            change_5m=round(change_5m, 3),
            amount_delta=amount_delta,
            trade_delta=trade_delta,
            is_hard_event=hard,
            event_types=tuple(dict.fromkeys(event_types)),
            reasons=tuple(dict.fromkeys(reasons)),
            metrics=common_metrics,
        )

    def _negative(
        self,
        quote: Quote,
        context: StockContext,
        change_5m: float,
        amount_delta: float,
        trade_delta: int,
        common_metrics: dict[str, Any],
    ) -> Anomaly | None:
        t = self.thresholds
        reasons: list[str] = []
        event_types: list[str] = []
        hard = False
        touched_limit = _at_or_above(quote.high, context.upper_limit, t.limit_tolerance)
        failed_limit = bool(
            touched_limit
            and context.upper_limit
            and quote.close < context.upper_limit - 0.01
        )
        at_lower_limit = _at_or_below(quote.close, context.lower_limit, t.limit_tolerance)
        if failed_limit:
            hard = True
            event_types.append("failed_limit")
            reasons.append("盘中触及涨停后开板")
        if at_lower_limit:
            hard = True
            event_types.append("limit_down")
            reasons.append("当前价格触及跌停")
        if quote.pct_change <= t.hard_negative_pct or change_5m <= -4.0:
            hard = True
            event_types.append("hard_selloff")
            reasons.append("出现大幅负反馈")

        conditions = 0
        if quote.pct_change <= t.negative_pct:
            conditions += 1
            event_types.append("large_decline")
            reasons.append(f"当日跌幅{quote.pct_change:.2f}%")
        if change_5m <= -2.0:
            conditions += 1
            event_types.append("rapid_decline")
            reasons.append(f"5分钟下跌{abs(change_5m):.2f}%")
        if amount_delta >= t.amount_delta and quote.close < quote.open:
            conditions += 1
            event_types.append("selling_with_volume")
            reasons.append("下跌同时出现较大增量成交额")
        if not hard and conditions < 2:
            return None
        severity = min(
            100.0,
            35.0
            + abs(min(0.0, quote.pct_change)) * 3.0
            + abs(min(0.0, change_5m)) * 4.0
            + (20.0 if failed_limit else 0.0),
        )
        return Anomaly(
            code=quote.code,
            name=quote.name,
            captured_at=quote.captured_at,
            direction=Direction.NEGATIVE,
            severity=round(severity, 2),
            pct_change=round(quote.pct_change, 3),
            change_5m=round(change_5m, 3),
            amount_delta=amount_delta,
            trade_delta=trade_delta,
            is_hard_event=hard,
            event_types=tuple(dict.fromkeys(event_types)),
            reasons=tuple(dict.fromkeys(reasons)),
            metrics=common_metrics,
        )


def _history_pair(
    history: list[dict[str, Any]], captured_at: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not history:
        return None, None
    previous = history[0]
    previous_time = datetime.fromisoformat(str(previous["captured_at"]))
    if previous_time.tzinfo is None:
        previous_time = previous_time.replace(tzinfo=captured_at.tzinfo)
    elapsed = (captured_at - previous_time).total_seconds()
    if elapsed <= 0 or elapsed > 10 * 60:
        return None, None
    prior = history[1] if len(history) > 1 else None
    return previous, prior


def _interval_delta(
    current: dict[str, Any] | None, previous: dict[str, Any] | None, field: str
) -> float:
    if not current or not previous:
        return 0.0
    return max(0.0, float(current.get(field, 0)) - float(previous.get(field, 0)))


def _price_change(current: float, previous: Any) -> float:
    try:
        previous_float = float(previous)
    except (TypeError, ValueError):
        return 0.0
    if previous_float <= 0:
        return 0.0
    return (current / previous_float - 1.0) * 100.0


def _ratio(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return current / previous


def _at_or_above(price: float, target: float | None, tolerance: float) -> bool:
    return bool(target and target > 0 and price >= target - tolerance)


def _at_or_below(price: float, target: float | None, tolerance: float) -> bool:
    return bool(target and target > 0 and price <= target + tolerance)
