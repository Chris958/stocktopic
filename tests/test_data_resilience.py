from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import stocktopic.data_resilience as resilience
from stocktopic.data_resilience import (
    _is_dependent_network_failure,
    _quote_failure_streak,
    _record_tushare_network_failure,
)

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


def test_tushare_network_incident_aggregates_jobs_and_alerts_only_after_ten_minutes():
    service = FakeService()
    resilience._last_tushare_success_at = None
    first = datetime(2026, 9, 3, 16, 49, tzinfo=CN)

    state = _record_tushare_network_failure(
        service,
        "sync_kpl_concepts",
        first,
        "Tushare error network: DNS",
    )
    assert state["should_alert"] is False

    state = _record_tushare_network_failure(
        service,
        "sync_daily_limits",
        first + timedelta(minutes=1),
        "Tushare error network: DNS",
    )
    assert state["should_alert"] is False
    assert state["jobs"] == ["sync_daily_limits", "sync_kpl_concepts"]

    state = _record_tushare_network_failure(
        service,
        "tushare_network_probe",
        first + timedelta(minutes=11),
        "Tushare error network: DNS",
    )
    assert state["should_alert"] is True
    assert state["alerted"] is True

    repeated = _record_tushare_network_failure(
        service,
        "sync_kpl_events",
        first + timedelta(minutes=12),
        "Tushare error network: DNS",
    )
    assert repeated["should_alert"] is False


def test_discovery_backfill_wrapper_is_suppressed_during_recent_network_incident():
    service = FakeService()
    resilience._last_tushare_success_at = None
    current = datetime(2026, 9, 3, 16, 50, tzinfo=CN)
    _record_tushare_network_failure(
        service,
        "sync_kpl_events",
        current,
        "Tushare error network: DNS",
    )

    assert _is_dependent_network_failure(
        service,
        "discovery_backfill",
        "KPL events unavailable for 20260902",
        current + timedelta(seconds=30),
    )
