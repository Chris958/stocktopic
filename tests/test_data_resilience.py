from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from stocktopic.data_resilience import _quote_failure_streak

CN = ZoneInfo("Asia/Shanghai")


class FakeDatabase:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_metadata(self, key: str) -> str | None:
        return self.values.get(key)

    def set_metadata(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeService:
    def __init__(self):
        self.database = FakeDatabase()


def test_quote_failure_streak_counts_unique_consecutive_slots_only():
    service = FakeService()

    first = datetime(2026, 9, 3, 14, 0, tzinfo=CN)
    assert _quote_failure_streak(service, first) == 1
    assert _quote_failure_streak(service, first.replace(second=10)) == 1

    second = datetime(2026, 9, 3, 14, 5, tzinfo=CN)
    assert _quote_failure_streak(service, second) == 2

    state = json.loads(service.database.get_metadata("quote_failure_streak") or "{}")
    assert state["count"] == 2
    assert state["slot"] == "202609031405"


def test_quote_failure_streak_resets_after_long_gap():
    service = FakeService()

    assert _quote_failure_streak(
        service, datetime(2026, 9, 3, 11, 30, tzinfo=CN)
    ) == 1
    assert _quote_failure_streak(
        service, datetime(2026, 9, 3, 13, 0, tzinfo=CN)
    ) == 1
