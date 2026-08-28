from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    MERGED = "merged"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Quote:
    code: str
    name: str
    pre_close: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    trades: int
    trade_time: str
    captured_at: datetime

    @property
    def pct_change(self) -> float:
        if self.pre_close <= 0 or self.close <= 0:
            return 0.0
        return (self.close / self.pre_close - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class StockContext:
    code: str
    name: str
    list_date: str = ""
    upper_limit: float | None = None
    lower_limit: float | None = None


@dataclass(frozen=True, slots=True)
class Anomaly:
    code: str
    name: str
    captured_at: datetime
    direction: Direction
    severity: float
    pct_change: float
    change_5m: float
    amount_delta: float
    trade_delta: int
    is_hard_event: bool
    event_types: tuple[str, ...]
    reasons: tuple[str, ...]
    metrics: dict[str, Any] = field(default_factory=dict)
