from datetime import datetime

import pytest

from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.market_clock import SHANGHAI
from stocktopic.market_environment import build_market_environment
from stocktopic.service import StockTopicService

PREVIOUS = "20261120"
DAY = "20261123"
CODES = [f"60000{i}.SH" for i in range(6)]


@pytest.fixture
def service(tmp_path):
    service = StockTopicService(Settings(
        tushare_token="offline-test", db_path=tmp_path / "db", archive_dir=tmp_path / "archive",
    ))
    service.database.initialize()
    service.database.replace_calendar([
        {"cal_date": PREVIOUS, "is_open": 1, "pretrade_date": "20261119"},
        {"cal_date": "20261121", "is_open": 0, "pretrade_date": PREVIOUS},
        {"cal_date": "20261122", "is_open": 0, "pretrade_date": PREVIOUS},
        {"cal_date": DAY, "is_open": 1, "pretrade_date": PREVIOUS},
    ])
    service.database.upsert_stocks([{"ts_code": c, "name": c, "market": "主板"} for c in CODES])
    return service


def quote(code, now, close, high=None, low=None):
    return Quote(
        code=code, name=code, pre_close=10, open=10, high=high or max(10, close),
        low=low or min(10, close), close=close, volume=1000, amount=10000, trades=10,
        trade_time=now.strftime("%H:%M:%S"), captured_at=now,
    )


def test_same_day_prices_and_known_samples_override_stale_boards(service):
    db = service.database
    db.upsert_kpl_events([
        {"trade_date": PREVIOUS, "tag": "涨停", "status": "2连板", "ts_code": c}
        for c in CODES[:3]
    ] + [
        {"trade_date": DAY, "tag": "涨停", "status": "4天3板", "ts_code": c}
        for c in CODES[:2]
    ])
    db.upsert_daily_limits("2026-11-23", [
        {"ts_code": c, "up_limit": 11, "down_limit": 9} for c in CODES
    ])
    now = datetime(2026, 11, 23, 10, 0, tzinfo=SHANGHAI)
    yesterday = datetime(2026, 11, 20, 15, 0, tzinfo=SHANGHAI)
    db.save_quotes([quote(CODES[2], yesterday, 11)], "2026-11-20", "15:00")
    db.save_quotes([
        quote(CODES[0], now, 9.2, high=11),
        quote(CODES[1], now, 11),
        quote(CODES[3], now, 10, high=11, low=9),
    ], "2026-11-23", "10:00")

    result = build_market_environment(db, DAY, as_of=now)

    assert result["limit_up_count"] == 1  # First stock's old seal is broken.
    assert result["max_board_height"] == 0  # 4天3板 is not a consecutive-board fact.
    assert result["yesterday_limit_expected_count"] == 3
    assert result["yesterday_limit_sample_count"] == 2  # Friday's quote is not Monday data.
    assert result["yesterday_limit_return"] == pytest.approx(1.0)
    assert result["promotion_rate"] == 50.0
    assert result["nuclear_button_count"] == 1 and result["nuclear_button_ratio"] == 50.0
    assert result["break_board_return"] == pytest.approx(-8.0)
    assert result["earth_sky_count"] == 1
    assert result["feedback_ready"] is False and result["label"] == "数据待补"


def test_missing_market_samples_are_unknown_and_fresh_loss_is_real(service):
    db = service.database
    db.upsert_kpl_events([{"trade_date": PREVIOUS, "tag": "涨停", "ts_code": CODES[0]}])
    now = datetime(2026, 11, 23, 10, 0, tzinfo=SHANGHAI)
    before = build_market_environment(db, DAY, as_of=now)
    assert before["yesterday_limit_sample_count"] == 0
    assert before["yesterday_limit_return"] is None and before["nuclear_button_ratio"] is None

    db.save_quotes([quote(CODES[0], now, 9.2)], "2026-11-23", "10:00")
    live = build_market_environment(db, DAY, as_of=now)
    assert live["feedback_ready"] is True and live["nuclear_button_ratio"] == 100.0
    stale = build_market_environment(db, DAY, as_of=now.replace(minute=10))
    assert stale["feedback_ready"] is False and stale["yesterday_limit_return"] is None


def test_cohort_settles_next_trade_day_close_only_and_does_not_dilute_losses(service):
    db = service.database
    theme_id = db.upsert_candidate(
        fingerprint="sample", provisional_name="机器人", shared_tag="机器人", direction="positive",
        discovered_at="2026-11-20T10:00:00+08:00", day1_date="2026-11-20",
        discovery_reason="test", members=[],
    )
    db.record_cohort(theme_id, "2026-11-20", [{"code": CODES[0], "outcome": "涨停"}])
    db.record_cohort(theme_id, PREVIOUS, [{"code": CODES[1], "outcome": "涨停"}])
    db.record_cohort(theme_id, DAY, [{"code": CODES[2], "outcome": "涨停"}])
    now = datetime(2026, 11, 23, 10, 0, tzinfo=SHANGHAI)
    db.save_quotes([quote(CODES[0], now, 9.2)], "2026-11-23", "10:00")
    assert db.settle_cohort_next_day_returns(DAY) == 0
    assert db.cohort_stats(theme_id, DAY)["loss_ratio"] is None

    closed = now.replace(hour=15)
    db.save_quotes([quote(CODES[0], closed, 9.2)], "2026-11-23", "15:00")
    assert db.settle_cohort_next_day_returns(DAY) == 1
    stats = db.cohort_stats(theme_id, DAY)
    assert stats["observations"] == 1 and stats["expected_count"] == 2
    assert stats["loss_ratio"] == 1.0 and stats["avg_next_day_return"] == pytest.approx(-8.0)

    db.upsert_daily_bars([{
        "ts_code": CODES[1], "trade_date": DAY, "pre_close": 10, "close": 10.2,
        "open": 10, "high": 10.2, "low": 10, "pct_chg": 2, "vol": 1000,
    }])
    assert db.settle_cohort_next_day_returns(DAY) == 1
    assert db.cohort_stats(theme_id, DAY)["loss_ratio"] == 0.5
    assert db.settle_cohort_next_day_returns(DAY) == 0


def test_recent_feedback_repair_prefers_final_official_data(service):
    db = service.database
    theme_id = db.upsert_candidate(
        fingerprint="repair", provisional_name="机器人", shared_tag="机器人", direction="positive",
        discovered_at="2026-11-20T10:00:00+08:00", day1_date="2026-11-20",
        discovery_reason="test", members=[],
    )
    db.record_cohort(theme_id, "2026-11-20", [{"code": CODES[0], "outcome": "涨停"}])
    now = datetime(2026, 11, 23, 15, 0, tzinfo=SHANGHAI)
    db.save_quotes([quote(CODES[0], now, 10.1)], "2026-11-23", "15:00")
    # Even when the vendor's percentage field is missing, valid final prices show the loss.
    db.upsert_daily_bars([{
        "ts_code": CODES[0], "trade_date": DAY, "pre_close": 10, "close": 9.4,
        "open": 10, "high": 10, "low": 9.4, "vol": 1000,
    }])
    assert db.repair_recent_cohort_feedback("20261124") == 1
    assert db.cohort_stats(theme_id, "20261124")["avg_next_day_return"] == pytest.approx(-6.0)
    assert db.cohort_stats(theme_id, "20261124")["loss_ratio"] == 1.0
