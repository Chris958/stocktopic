import json
from datetime import datetime

import pytest

from stocktopic.config import Settings
from stocktopic.market_clock import SHANGHAI
from stocktopic.market_cycles import MarketCycleTracker
from stocktopic.service import StockTopicService
from stocktopic.theme_policy import policy_structured_event_clusters


def current(day=16):
    return datetime(2026, 10, day, 10, 0, tzinfo=SHANGHAI)


def event(i, tag="机器人概念", status="首板"):
    return {
        "code": f"{600000 + i:06}.SH",
        "name": f"合成股票{i}",
        "themes": [tag],
        "board_tag": "涨停",
        "status": status,
        "market": "主板",
        "pct_change": 10,
        "limit_up_time": "093500",
        "limit_reason": "合成测试业务关系",
    }


@pytest.fixture
def service(tmp_path):
    s = StockTopicService(
        Settings(
            tushare_token="test",
            openai_api_key="test",
            db_path=tmp_path / "test.sqlite3",
            archive_dir=tmp_path / "archive",
        )
    )
    s.initialize_storage()
    s.database.replace_calendar(
        [
            {"cal_date": f"202610{d:02}", "is_open": int(current(d).weekday() < 5)}
            for d in range(8, 31)
        ]
    )
    return s


def observe(service, day, events, complete=True):
    service.market_cycles.observe(
        f"202610{day}",
        current(day),
        events,
        policy_structured_event_clusters(events),
        coverage_complete=complete,
    )


def test_robot_and_low_air_track_independent_cross_day_cycles_with_ladders(service):
    first = [event(i) for i in range(4)] + [event(i, "低空经济") for i in range(10, 14)]
    observe(service, 16, first)
    old = {c["family"]: c for c in service.market_cycles.dashboard(current())}
    second = [event(0, "人形机器人", "2连板"), event(1, "人形机器人", "2连板")]
    second += [event(i, "人形机器人") for i in (4, 5, 6)]
    second += [event(i, "低空经济", "2连板") for i in (10, 11)]
    observe(service, 19, second)
    cycles = {c["family"]: c for c in service.market_cycles.dashboard(current(19))}
    robot = cycles["机器人"]
    assert robot["cycle_id"] == old["机器人"]["cycle_id"]
    assert robot["latest"]["board_ladder"] == {"2": 2, "1": 3}
    assert robot["latest"]["new_strong_count"] == 3
    assert robot["latest"]["retained_strong_count"] == 2
    assert robot["latest"]["promotion_count"] == 2
    assert robot["latest"]["stage"] == "扩散"
    assert cycles["低空经济"]["latest"]["stage"] == "分化"
    assert service.database.list_themes() == []  # Market observations do not admit an event.


def test_dashboard_keeps_directions_beyond_the_initial_eight_cards(service):
    rows = [event(i) for i in range(4)]
    for row in rows:
        row["themes"] = [f"细分事件{j}" for j in range(10)]
    observe(service, 16, rows)
    assert len(service.market_cycles.dashboard(current())) == 10


def test_four_stock_ai_rejection_stays_visible_as_a_market_observation(service):
    events = [event(i) for i in range(4)]
    service.database.limit_touch_events = lambda _: events
    calls = []

    def reject(prompt, **kwargs):
        calls.append(prompt)
        return (
            {},
            {
                "clusters": [],
                "rejected_nodes": [{"anchor_tag": "机器人概念", "reason": "无共同催化证据"}],
            },
            [],
        )

    service.explainer._call_prompt = reject
    assert service.discover_trade_date("20261016", current()) == []
    assert service.database.list_themes() == []
    cycles = service.market_cycles.dashboard(current())
    assert cycles[0]["latest"]["touch_count"] == 4
    assert cycles[0]["event_admission"] == "not_verified"
    assert len(calls) == 1
    assert service.database.recent_alerts() == []


def test_three_to_four_rejected_upgrade_updates_market_snapshot_and_preserves_no_ai_floor(service):
    rows = [event(i) for i in range(3)]
    service.database.limit_touch_events = lambda _: rows
    calls = []

    def reject(*args, **kwargs):
        calls.append(True)
        return {}, {"clusters": []}, []

    service.explainer._call_prompt = reject
    service.discover_trade_date("20261016", current())
    assert calls == [] and service.database.recent_alerts() == []
    assert service.market_cycles.dashboard(current()) == []
    rows.append(event(3))
    service.discover_trade_date("20261016", current())
    assert len(calls) == 1
    assert service.market_cycles.dashboard(current())[0]["latest"]["touch_count"] == 4
    assert service.database.recent_alerts() == []


def test_missing_market_coverage_is_not_treated_as_cycle_cooling(service):
    observe(service, 16, [event(i) for i in range(4)])
    observe(service, 19, [], complete=False)
    cycle = service.market_cycles.dashboard(current(19))[0]
    assert cycle["latest"]["stage"] == "数据待补"
    assert cycle["inactive_complete_days"] == 0
    observe(service, 20, [event(i) for i in range(5)])
    cycle = service.market_cycles.dashboard(current(20))[0]
    assert cycle["latest"]["stage"] == "跟踪恢复"
    assert cycle["latest"]["promotion_count"] is None


def test_market_observation_alone_does_not_restore_archived_event(service):
    theme_id = service.database.upsert_candidate(
        fingerprint="old",
        provisional_name="具体机器人事件",
        shared_tag="人形机器人",
        direction="positive",
        discovered_at=current().isoformat(),
        day1_date="2026-10-16",
        discovery_reason="4股共同事件",
        members=[],
    )
    service.database.set_theme_status(theme_id, "confirmed", "具体机器人事件")
    service.database.archive_theme(theme_id)
    observe(service, 19, [event(i) for i in range(4)])
    cycle = service.market_cycles.dashboard(current(19))[0]
    assert cycle["event_admission"] == "historical_event_only"
    assert cycle["related_themes"][0]["id"] == theme_id
    assert service.database.get_theme(theme_id)["status"] == "archived"
    restarted = MarketCycleTracker(service.database)
    assert restarted.dashboard(current(19))[0]["cycle_id"] == cycle["cycle_id"]


def test_late_replay_cannot_overwrite_newer_live_snapshot(service):
    observe(service, 19, [event(i) for i in range(4)])
    service.market_cycles.observe(
        "20261016",
        current(20),
        [event(i) for i in range(10)],
        policy_structured_event_clusters([event(i) for i in range(10)]),
        coverage_complete=True,
    )
    assert service.market_cycles.dashboard(current(20))[0]["latest"]["trade_date"] == "20261019"


def test_previous_closing_streak_uses_official_limits_and_excludes_future_data(service):
    db = service.database
    for day, close in ((21, 10), (22, 11), (23, 11), (26, 11)):
        db.upsert_daily_limits(
            f"2026-10-{day}", [{"ts_code": "600000.SH", "up_limit": 11, "down_limit": 9}]
        )
        db.upsert_daily_bars(
            [
                {
                    "trade_date": f"202610{day}",
                    "ts_code": "600000.SH",
                    "close": close,
                    "high": 11,
                    "pre_close": 10,
                }
            ]
        )
    assert db.previous_limit_streaks("20261026", ["600000.SH"])["600000.SH"] == 2
    assert db.previous_limit_streaks("20261026", ["600001.SH"])["600001.SH"] is None
    row = event(0, status=None)
    observe(service, 26, [row, event(1), event(2), event(3)])
    cycle = service.market_cycles.dashboard(current(26))[0]
    first = next(m for m in cycle["latest"]["members"] if m["code"] == row["code"])
    assert first["board_height"] == 3
    assert first["board_basis"] == "local_price_streak_estimate"


def test_broad_parent_is_not_an_investable_market_node_and_overlaps_are_unique(service):
    rows = [event(i, "文化传媒") for i in range(6)]
    observe(service, 16, rows)
    assert service.market_cycles.dashboard(current()) == []
    rows = [event(i) for i in range(4)]
    for row in rows:
        row["themes"].append("人形机器人")
    observe(service, 19, rows)
    cycle = service.market_cycles.dashboard(current(19))[0]
    assert cycle["latest"]["touch_count"] == 4
    assert len(cycle["latest"]["member_codes"]) == 4
    assert json.dumps(cycle).find("formal_candidate") == -1
