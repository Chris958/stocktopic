import json
from datetime import datetime

import pytest

from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.market_clock import SHANGHAI
from stocktopic.service import StockTopicService


def now(day=16, hour=10, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI)


@pytest.fixture
def service(tmp_path):
    service = StockTopicService(
        Settings(
            tushare_token="test",
            db_path=tmp_path / "test.sqlite3",
            archive_dir=tmp_path / "archive",
        )
    )
    service.initialize_storage()
    service.database.replace_calendar(
        [
            {"cal_date": f"202610{day:02}", "is_open": int(now(day).weekday() < 5)}
            for day in range(8, 31)
        ]
    )
    service.database.upsert_stocks(
        [
            {"ts_code": f"{600000 + i:06}.SH", "name": f"合成股票{i}", "market": "主板"}
            for i in range(4)
        ]
    )
    return service


class Provider:
    transport = "test"

    def __init__(self, count=4):
        self.count, self.limit_dates, self.kpl_calls = count, [], []

    def stock_limits(self, day):
        self.limit_dates.append(day)
        return [
            {
                "ts_code": f"{600000 + i:06}.SH",
                "trade_date": day,
                "pre_close": 10,
                "up_limit": 11,
                "down_limit": 9,
            }
            for i in range(self.count)
        ]

    def realtime_quotes(self, current):
        return [
            Quote(
                code=f"{600000 + i:06}.SH",
                name=f"合成股票{i}",
                pre_close=10,
                open=10,
                close=11 if i < 4 else 10.1,
                high=11 if i < 4 else 10.2,
                low=10,
                volume=100000 + current.day,
                amount=1000000 + current.day,
                trades=100,
                trade_time=current.strftime("%H:%M:%S"),
                captured_at=current,
            )
            for i in range(self.count)
        ]

    def kpl_list(self, day, tag):
        self.kpl_calls.append((day, tag))
        return (
            [
                {
                    "trade_date": day,
                    "ts_code": "600000.SH",
                    "name": "合成股票0",
                    "tag": "涨停",
                    "theme": "机器人概念",
                    "status": "2连板",
                    "lu_time": "093210",
                }
            ]
            if tag == "涨停"
            else []
        )

    def kpl_concept_members(self, day):
        return []

    def daily_prices(self, day):
        return [
            {
                "trade_date": day,
                "ts_code": f"{600000 + i:06}.SH",
                "pre_close": 10,
                "open": 10,
                "high": 11 if i < 4 else 10.2,
                "low": 10,
                "close": 11 if i < 4 else 10.1,
            }
            for i in range(self.count)
        ]


def test_trading_date_rollover_recovers_mainboard_without_restart(service):
    service.provider = Provider(2000)
    service.database.upsert_stocks(
        [
            {"ts_code": f"{600000 + i:06}.SH", "name": f"合成股票{i}", "market": "主板"}
            for i in range(2000)
        ]
    )
    for day in (16, 19):
        result = service.collect_once(now(day))
        assert result["status"] == "success"
        assert result["mainboard_touch_signals"] == 4
        assert result["discovery_trade_date"] == f"202610{day}"
        assert result["discovery_coverage"]["mainboard_available"] is True
        assert service.database.strong_input_counts(f"202610{day}")["主板"] == 4
    assert service.provider.limit_dates == ["20261016", "20261019"]
    assert service.provider.kpl_calls == []  # Official KPL is a delayed daily source.


def test_empty_daily_limits_retry_after_cooldown_and_wrong_date_is_rejected(service):
    provider = Provider(0)
    service.provider = provider
    readiness = service.data_readiness
    assert readiness.ensure_limits("20261016", now())["ready"] is False
    assert service.database.latest_run("sync_daily_limits")["status"] == "degraded"
    readiness.ensure_limits("20261016", now(minute=4))
    assert len(provider.limit_dates) == 1
    provider.count = 4
    assert readiness.ensure_limits("20261016", now(minute=5))["ready"] is True
    assert len(provider.limit_dates) == 2
    provider.stock_limits = lambda day: [
        {"ts_code": "600000.SH", "trade_date": "20261016", "up_limit": 11, "down_limit": 9}
    ]
    service.clock.china_now = lambda: now(19)
    assert readiness.ensure_limits("20261019", now(19))["ready"] is False
    assert service.database.daily_limit_map("2026-10-19") == {}
    health = service.health()
    assert health["status"] == "degraded"
    assert any("主板" in message for message in health["warnings"])


def test_prepublication_empty_is_pending_not_a_provider_failure(service):
    provider = Provider()
    service.provider = provider
    assert service.data_readiness.kpl_for_date("20261016", now()) == 0
    assert provider.kpl_calls == []
    coverage = json.loads(service.database.get_metadata("kpl_event_coverage:20261016"))
    assert coverage["state"] == "pending_publication" and coverage["row_count"] is None
    assert service.data_readiness.kpl_for_date("20261016", now(17, 5)) == 0
    assert provider.kpl_calls == []
    assert service.data_readiness.kpl_for_date("20261016", now(17, 7)) == 1
    assert len(provider.kpl_calls) == 3


def test_readiness_is_not_marked_ready_by_partial_limit_coverage(service):
    service.provider = Provider(1)
    coverage = service.data_readiness.ensure_limits("20261016", now())
    assert coverage["valid_mainboard_count"] == 1
    assert coverage["expected_mainboard_count"] == 4
    assert coverage["state"] == "incomplete"
    assert len(coverage["missing_codes_sample"]) == 3


def test_limits_alone_do_not_hide_missing_mainboard_quotes(service):
    service.provider = Provider()
    service.data_readiness.ensure_limits("20261016", now())
    quotes = service.provider.realtime_quotes(now())[:1]
    coverage = service.data_readiness.signal_coverage(
        "20261016", now(), quotes, service.database.daily_limit_map("2026-10-16")
    )
    assert coverage["expected_mainboard_count"] == 4
    assert coverage["mainboard_with_limit_count"] == 1
    assert coverage["mainboard_available"] is False
    service.clock.china_now = now
    assert service.health()["status"] == "degraded"


def test_previous_day_publication_is_scheduled_on_saturday_with_bounded_retry(service):
    readiness = service.data_readiness
    assert "20261016" in readiness.due_completed_dates(now(17, 7))
    service.database.set_metadata(
        "completed_sources:20261016",
        json.dumps(
            {
                "state": "incomplete",
                "attempted_at": now(17, 7).isoformat(),
            }
        ),
    )
    assert "20261016" not in readiness.due_completed_dates(now(17, 7, 29))
    assert "20261016" in readiness.due_completed_dates(now(17, 7, 30))
    service.database.set_metadata("completed_sources:20261016", '{"state":"ready"}')
    assert "20261016" not in readiness.due_completed_dates(now(19))


def test_delayed_sources_reconcile_without_ai_or_backdated_opportunity_alert(service):
    service.provider = Provider(2000)
    service.database.upsert_stocks(
        [
            {"ts_code": f"{600000 + i:06}.SH", "name": f"合成股票{i}", "market": "主板"}
            for i in range(2000)
        ]
    )
    messages = []
    service._send_alert = lambda **kw: messages.append(kw)
    service.explainer._call_prompt = lambda *a, **kw: pytest.fail(
        "Historical source repair calls AI"
    )
    result = service.data_readiness.reconcile("20261016", now(17, 7))
    assert result["state"] == "ready"
    assert result["mainboard_replayed"] == 3
    events = {e["code"]: e for e in service.database.limit_touch_events("20261016")}
    assert events["600000.SH"]["status"] == "2连板"
    assert events["600000.SH"]["limit_up_time"] == "093210"
    replay = events["600001.SH"]
    assert replay["limit_up_time"] is None
    assert replay["limit_reason"] == ""
    assert replay["limit_time_basis"] == "daily_high_time_unknown"
    assert replay["synced_at"] == now(17, 7).isoformat()
    assert messages == []
    assert service.database.list_themes() == []
    previous_calls = list(service.provider.kpl_calls)
    service.data_readiness.reconcile("20261016", now(17, 8))
    assert service.provider.kpl_calls == previous_calls


def test_intraday_fallback_is_preserved_when_daily_evidence_is_replayed(service):
    service.provider = Provider()
    current = now()
    service.data_readiness.ensure_limits("20261016", current)
    quotes = service.provider.realtime_quotes(current)
    service.database.upsert_mainboard_limit_events(
        "20261016",
        quotes,
        service.database.daily_limit_map("2026-10-16"),
    )
    service.database.upsert_daily_bars(service.provider.daily_prices("20261016"))
    assert service.database.backfill_mainboard_limit_events("20261016", now(17)) == 0
    assert {e["limit_up_time"] for e in service.database.limit_touch_events("20261016")} == {
        "100000"
    }
