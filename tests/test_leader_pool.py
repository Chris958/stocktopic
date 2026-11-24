"""Watchlist integration with fixed cohorts, real SQLite, and no network or model calls."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.leader_pool import LeaderPool
from stocktopic.market_clock import SHANGHAI

STOCKS = [f"60000{i}.SH" for i in range(6)]
FIRST_DAY = datetime(2026, 11, 23, 10, 0, tzinfo=SHANGHAI)
AUTH = {"Authorization": "Bearer leader-test"}


@pytest.fixture
def fixture(tmp_path):
    app = create_app(Settings(
        tushare_token="offline-test", app_api_token="leader-test",
        db_path=tmp_path / "db", archive_dir=tmp_path / "archive",
    ))
    svc = app.state.service
    svc.clock.china_now = lambda: FIRST_DAY
    db = svc.database
    db.initialize()
    days = ["20261118", "20261119", "20261120", "20261123", "20261124", "20261125", "20261126"]
    db.replace_calendar([
        {"cal_date": day, "is_open": 1, "pretrade_date": days[i - 1] if i else "20261117"}
        for i, day in enumerate(days)
    ])
    db.upsert_stocks([{"ts_code": c, "name": f"样本{i}", "market": "主板"}
                      for i, c in enumerate(STOCKS)])
    for day in days:
        db.upsert_daily_limits(day, [
            {"ts_code": code, "up_limit": 11, "down_limit": 9} for code in STOCKS
        ])
    db.upsert_daily_bars([
        {"ts_code": c, "trade_date": "20261120", "pre_close": 10, "open": 10, "high": 11,
         "close": 11 if i in {0, 1, 4} else 10, "low": 10, "pct_chg": 10 if i in {0, 1, 4} else 0,
         "vol": 10000}
        for i, c in enumerate(STOCKS)
    ])
    db.upsert_kpl_events([
        {"ts_code": STOCKS[i], "trade_date": "20261120", "tag": "涨停", "status": status}
        for i, status in [(0, "首板"), (1, "4连板"), (4, "首板")]
    ])
    return app, svc


def add_theme(service, name="机器人", members=STOCKS[:4]):
    tid = service.database.upsert_candidate(
        fingerprint=name, provisional_name=name, shared_tag=name, direction="positive",
        discovered_at="2026-11-02T10:00:00+08:00", day1_date="2026-11-02",
        discovery_reason="test", members=[{"code": c, "name": c, "evidence": {}} for c in members],
    )
    service.database.set_theme_status(tid, "confirmed", name)
    return tid


def step(service, when, changes, *, coverage=True, events=(), skip=()):
    series = []
    for i, pct in enumerate(changes):
        if i in skip:
            continue
        price = round(10 * (1 + pct / 100), 3)
        series.append(Quote(
            code=STOCKS[i], name=f"样本{i}", pre_close=10, open=10,
            high=max(10, price), low=min(10, price), close=price,
            volume=10_000, amount=(6 - i) * 100000, trades=30,
            trade_time=when.strftime("%H:%M:%S"), captured_at=when,
        ))
    service.clock.china_now = lambda: when
    service.leaders.observe(when, series, {c: (11, 9) for c in STOCKS}, list(events),
                            service.database.active_stock_map(), coverage_complete=coverage)
    return service.leaders.read(when)["items"]


def by_code(items, i=0):
    return next(item for item in items if item["code"] == STOCKS[i])


def start(service):
    return step(service, FIRST_DAY, [5, 10, 3, 3, 5, 2])


def official_close(service, day, moves):
    rows = []
    for code, move in zip(STOCKS, moves, strict=True):
        price = round(10 * (1 + move / 100), 3)
        rows.append({
            "ts_code": code, "trade_date": day.strftime("%Y%m%d"), "pre_close": 10,
            "open": 10, "high": max(10, price), "low": min(10, price), "close": price,
            "vol": 10000,
        })
    service.database.upsert_daily_bars(rows)


def repair(service, follow=True):
    start(service)
    for minute, moves in [(5, [3.6, 8, 1, 1]), (10, [4.4, 8.1, 1.1, 1.2]),
                          (15, [4.6, 8.2, 2.1 if follow else 1.2, 1.1])]:
        items = step(service, FIRST_DAY + timedelta(minutes=minute), moves + [0, 0])
    return items


def test_unique_across_themes_cross_day_and_restart_with_stable_entry(fixture):
    _, svc = fixture
    add_theme(svc)
    add_theme(svc, "低空经济", [STOCKS[0], *STOCKS[4:]])
    items = start(svc)
    selected = by_code(items)
    assert {d["name"] for d in selected["directions"]} == {"机器人", "低空经济"}
    assert len({item["code"] for item in items}) == len(items)
    assert "实际涨停" in selected["entry_reason"]
    assert by_code(items, 1)["directions"][0]["role"] == "height"

    svc.leaders = LeaderPool(svc.database)  # Process restart.
    items = step(svc, FIRST_DAY + timedelta(days=1), [0.3, 0, -0.2, -0.3, 0, 0])
    again = by_code(items)
    assert again["entered_at"] == selected["entered_at"]
    assert again["entry_reason"] == selected["entry_reason"]
    assert again["action"] == "wait"
    assert sum(e["code"] == STOCKS[0] for e in svc.leaders.history()["items"]) == 1


@pytest.mark.parametrize("follow", [False, True])
def test_fixed_leader_only_issues_window_with_later_peer_follow_through(fixture, follow):
    _, svc = fixture
    add_theme(svc)
    items = repair(svc, follow)
    low, high = by_code(items), by_code(items, 1)
    assert low["action"] == ("opportunity" if follow else "wait")
    assert high["action"] == "wait"
    if follow:
        assert low["status"] == "分歧修复"
        assert low["window"]["follower"] == STOCKS[2]
        assert low["window"]["support_price"] == pytest.approx(10.36)
        later = step(svc, FIRST_DAY + timedelta(minutes=20), [2.8, 7, 0, 1, 0, 0])
        assert by_code(later)["action"] == "wait" and by_code(later)["window"] is None
        assert by_code(later)["entered_at"] == low["entered_at"]


def test_stale_coverage_pause_and_missing_close_do_not_invent_exit(fixture):
    _, svc = fixture
    add_theme(svc)
    assert by_code(repair(svc))["action"] == "opportunity"
    assert by_code(svc.leaders.read(FIRST_DAY + timedelta(minutes=40))["items"])["action"] == "wait"
    failed = [-5, -3, -3, -3, 0, 0]
    official_close(svc, FIRST_DAY, failed)
    missing = FIRST_DAY + timedelta(days=1)
    assert by_code(step(svc, missing, failed, coverage=False))["status"] == "数据待补"
    third = missing + timedelta(days=1)
    assert by_code(step(svc, third, failed))["action"] == "wait"
    official_close(svc, third, failed)
    assert by_code(step(svc, third + timedelta(days=1), failed))["action"] == "wait"
    assert all(e["event"] == "entered" for e in svc.leaders.history()["items"])


@pytest.mark.parametrize("moves,reason", [
    ([-5, -3, -3, -3, 0, 0], "题材与个股同时走弱"),
    ([-2, 8, 4, 3, 0, 0], "原候选持续落后"),
])
def test_two_consecutive_reliable_closes_remove_and_record_reason(fixture, moves, reason):
    _, svc = fixture
    add_theme(svc)
    start(svc)
    official_close(svc, FIRST_DAY, moves)
    second_day = FIRST_DAY + timedelta(days=1)
    after_one = step(svc, second_day, moves)
    assert by_code(after_one)["action"] == "wait"
    # A captured 15:00 sample without the official daily bar doesn't remove the stock.
    assert by_code(step(svc, second_day.replace(hour=15), moves))["action"] == "wait"
    official_close(svc, second_day, moves)
    after_two = step(svc, FIRST_DAY + timedelta(days=2), moves)
    assert STOCKS[0] not in {item["code"] for item in after_two}
    log = [e for e in svc.leaders.history()["items"] if e["code"] == STOCKS[0]]
    assert [e["event"] for e in log] == ["removed", "entered"]
    assert reason in log[0]["reason"] and log[0]["entered_at"] == log[1]["at"]


def test_local_early_direction_cannot_notify_or_turn_into_formal(fixture):
    _, svc = fixture
    events = [{
        "code": STOCKS[i], "name": f"样本{i}", "board_tag": "涨停", "themes": ["人形机器人"],
        "limit_up_time": "094000" if i == 0 else "094500", "pct_change": 10.0,
        "market": "主板", "status": "首板",
    } for i in (0, 1)]
    items = step(svc, FIRST_DAY, [10, 10, 0, 0, 0, 0], events=events)
    assert items and all(s["action"] == "wait" for s in items)
    assert all(s["directions"][0]["scope"] == "local" for s in items)
    assert svc.database.direction_tracking_rows() == []
    assert svc.database.recent_alerts() == []


def test_expired_archive_stops_old_link_and_preserves_history(fixture):
    _, svc = fixture
    theme_id = add_theme(svc)
    start(svc)
    svc.database.archive_theme(theme_id)
    assert svc.leaders.read(FIRST_DAY)["items"] == []
    step(svc, FIRST_DAY + timedelta(minutes=5), [5, 10, 3, 3, 0, 0])
    history = svc.leaders.history()["items"]
    assert history[0]["event"] == "removed" and "超过14天" in history[0]["reason"]


def test_api_reads_snapshot_and_paginated_history_without_background_work(fixture, monkeypatch):
    app, svc = fixture
    add_theme(svc)
    start(svc)
    client = TestClient(app)
    assert client.get("/api/v1/leader-pool").status_code == 401
    assert client.get("/api/v1/leader-pool/history").status_code == 401

    def forbid(*args, **kwargs):
        pytest.fail("a watchlist GET attempted to rescan market data")

    monkeypatch.setattr(svc.leaders, "_prior_boards", forbid)
    response = client.get("/api/v1/leader-pool", headers=AUTH)
    assert response.status_code == 200 and len(response.json()["items"]) == 2
    first = client.get("/api/v1/leader-pool/history?limit=1", headers=AUTH).json()
    second = client.get("/api/v1/leader-pool/history", params={"before": first["next_cursor"]},
                        headers=AUTH).json()
    assert len(first["items"]) == len(second["items"]) == 1 and second["next_cursor"] is None
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_active_link_survives_other_theme_expiry_and_never_reissues_old_window(fixture):
    _, svc = fixture
    robot = add_theme(svc)
    other = add_theme(svc, "低空经济", [STOCKS[0], *STOCKS[4:]])
    assert by_code(repair(svc))["action"] == "opportunity"
    svc.database.archive_theme(robot)
    item = by_code(svc.leaders.read(svc.clock.china_now())["items"])
    assert item["action"] == "wait" and item["window"] is None
    assert [link["theme_id"] for link in item["directions"]] == [other]
    current = step(svc, FIRST_DAY + timedelta(minutes=20), [5, 9, 3, 3, 0, 0])
    assert by_code(current)["entered_at"] == FIRST_DAY.isoformat()
    assert not any(e["event"] == "removed" and e["code"] == STOCKS[0]
                   for e in svc.leaders.history()["items"])


def test_broad_industry_alone_cannot_populate_watchlist(fixture):
    _, svc = fixture
    events = [{"code": c, "board_tag": "涨停", "themes": ["文化传媒"], "pct_change": 10.0,
               "limit_up_time": "094000", "market": "主板"} for c in STOCKS]
    assert not step(svc, FIRST_DAY, [10] * len(STOCKS), events=events)
    assert svc.leaders.history()["items"] == []


def test_live_collector_populates_leaders_without_new_market_or_ai_calls(fixture, monkeypatch):
    _, svc = fixture
    add_theme(svc)
    extras = [f"{prefix}{i:03}.SH" for prefix in (600, 601, 603) for i in range(1000)
              if f"{prefix}{i:03}.SH" not in STOCKS][:2000 - len(STOCKS)]
    svc.database.upsert_stocks([
        {"ts_code": c, "name": c, "market": "主板"} for c in STOCKS + extras
    ])
    # The next valid collection has a configured daily-limit cache; no network setup.
    svc.database.upsert_daily_limits("2026-11-23", [
        {"ts_code": c, "up_limit": 11, "down_limit": 9} for c in extras
    ])

    def realtime(now):
        prices = [10.5, 11, 10.3, 10.4, 10, 10]
        return [Quote(
            code=c, name=c, pre_close=10, open=10, high=p, low=10, close=p,
            volume=1000, amount=10000, trades=10, trade_time="10:00:00", captured_at=now,
        ) for c, p in zip(STOCKS + extras, prices + [10] * len(extras), strict=True)]

    def no_calls(*args, **kwargs):
        pytest.fail("leader watchlist should reuse the collector batch")

    monkeypatch.setattr(svc.provider, "realtime_quotes", realtime)
    monkeypatch.setattr(svc.provider, "call", no_calls)
    monkeypatch.setattr(svc.explainer, "cluster_limit_events", no_calls)
    assert svc.collect_once(FIRST_DAY)["status"] == "success"
    assert by_code(svc.leaders.read(FIRST_DAY)["items"])["action"] == "wait"
