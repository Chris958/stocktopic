"""Minimal synthetic regressions from the production omission audit; no live APIs."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from stocktopic.ai import _compact_evidence
from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.event_bridge import EventBridgeDetector
from stocktopic.market_clock import SHANGHAI
from stocktopic.service import StockTopicService, _semantic_event_signature
from stocktopic.theme_policy import policy_structured_event_clusters

DAY = "20261009"
NOW = datetime(2026, 10, 9, 9, 45, tzinfo=SHANGHAI)


@pytest.fixture
def service(tmp_path):
    app = StockTopicService(Settings(
        tushare_token="test", openai_api_key="test", admin_password="test",
        app_api_token="test", db_path=tmp_path / "db.sqlite3", archive_dir=tmp_path / "archive",
    ))
    app.database.initialize()
    app.database.replace_calendar([
        {"cal_date": DAY, "is_open": "1", "pretrade_date": "20261008"},
    ])
    app.database.upsert_stocks([
        {"ts_code": f"60000{i}.SH", "name": f"测试{i}", "market": "主板"} for i in range(8)
    ])
    app.database.upsert_kpl_concept_members([
        {"trade_date": "20261008", "ts_code": "concept", "name": "AI影视",
         "con_code": f"60000{i}.SH", "desc": "AI漫剧制作与IP改编授权"} for i in range(4)
    ])
    return app


def quote(index=0, **changes):
    return replace(Quote(
        code=f"60000{index}.SH", name=f"测试{index}", pre_close=10, open=10.1,
        high=11, low=10, close=11, volume=10000, amount=110000, trades=100,
        trade_time="09:35:00", captured_at=NOW - timedelta(minutes=10-index),
    ), **changes)


def event(index, *tags):
    return {"code": f"60000{index}.SH", "name": f"测试{index}", "themes": list(tags),
            "board_tag": "涨停", "pct_change": 10, "limit_up_time": f"093{index}00"}


def test_high_overlap_does_not_connect_ai_drama_batteries_and_agriculture():
    events = [event(i, "DeepSeek概念", "AI影视" if i < 3 else "锂电池" if i < 6 else "种业")
              for i in range(8)]
    clusters = policy_structured_event_clusters(events)
    by_tag = {c["tag"]: c for c in clusters}
    assert set(by_tag) == {"DeepSeek概念", "AI影视", "锂电池", "种业"}
    assert by_tag["AI影视"]["touch_count"] == 3
    assert by_tag["锂电池"]["touch_count"] == 3
    assert not any(c["cluster_method"] == "knowledge_graph_family" for c in clusters)


def test_transitive_name_overlap_cannot_erase_concrete_nodes():
    events = [event(i, "低空经济", "低空经济设备维护", "设备维护") for i in range(2)]
    events += [event(2, "低空经济"), event(3, "低空经济设备维护"), event(4, "设备维护")]
    clusters = policy_structured_event_clusters(events)
    assert {"低空经济", "低空经济设备维护", "设备维护"} <= {c["tag"] for c in clusters}
    assert not any(c["cluster_method"] == "knowledge_graph_family" and c["touch_count"] == 5
                   for c in clusters)


def test_official_price_touch_fallback_preserves_first_observation_and_kpl_reason(service):
    db = service.database
    limits = {"600000.SH": (11, 9)}
    assert db.upsert_mainboard_limit_events(DAY, [quote()], limits) == 1
    first = db.limit_touch_events(DAY)[0]
    assert first["signal_source"] == "local_rt_k_limit"
    assert first["limit_time_basis"] == "first_observed_5m"
    assert first["limit_reason"] == ""
    assert first["themes"] == []  # Membership comes from the local graph, not fabricated tags.
    assert first["concept_tags"][0]["reason"] == "AI漫剧制作与IP改编授权"
    db.upsert_mainboard_limit_events(
        DAY, [quote(close=10.8, captured_at=NOW)], limits,
    )
    opened = db.limit_touch_events(DAY)
    assert len(opened) == 1
    assert opened[0]["board_tag"] == "炸板"
    assert opened[0]["limit_up_time"] == first["limit_up_time"]
    db.upsert_kpl_events([{
        "trade_date": DAY, "ts_code": "600000.SH", "tag": "炸板", "theme": "AI影视",
        "lu_desc": "公司发布AI漫剧产品", "lu_time": "093200", "status": "首板",
    }])
    db.upsert_mainboard_limit_events(DAY, [quote(close=10.7)], limits)
    sourced = db.limit_touch_events(DAY)[0]
    assert sourced["limit_reason"] == "公司发布AI漫剧产品"
    assert sourced["limit_up_time"] == "093200"
    assert sourced["signal_source"] == "tushare_kpl_list"


@pytest.mark.parametrize("changes,limits", [
    ({"high": 10.99}, {"600000.SH": (11, 9)}),
    ({}, {}),
    ({"pre_close": 0}, {"600000.SH": (11, 9)}),
    ({"high": 12}, {"600000.SH": (11, 9)}),
    ({"captured_at": NOW - timedelta(days=1)}, {"600000.SH": (11, 9)}),
    ({"captured_at": NOW.replace(hour=9, minute=20)}, {"600000.SH": (11, 9)}),
])
def test_fallback_requires_exact_official_price_and_same_day_session(service, changes, limits):
    assert service.database.upsert_mainboard_limit_events(DAY, [quote(**changes)], limits) == 0
    assert service.database.limit_touch_events(DAY) == []


def test_fallback_does_not_apply_mainboard_rule_to_chinext(service):
    service.database.upsert_stocks([
        {"ts_code": "301000.SZ", "name": "测试创业板", "market": "创业板"},
    ])
    q = quote(code="301000.SZ")
    assert service.database.upsert_mainboard_limit_events(DAY, [q], {q.code: (11, 9)}) == 0


@pytest.mark.parametrize("gap,expected", [(5, "observed_interval_5m"),
                                          (30, "first_observed_5m")])
def test_only_a_recent_non_touch_snapshot_bounds_the_first_touch(service, gap, expected):
    q = quote()
    prior = {q.code: [{"high": 10.8, "captured_at": (
        q.captured_at - timedelta(minutes=gap)).isoformat()}]}
    service.database.upsert_mainboard_limit_events(DAY, [q], {q.code: (11, 9)}, prior)
    assert service.database.limit_touch_events(DAY)[0]["limit_time_basis"] == expected


def test_unknown_first_observation_cannot_manufacture_bridge_time_synchronization():
    events = [event(i, "出版" if i < 2 else "影视") for i in range(4)]
    for item in events:
        item.update(limit_reason="AI漫剧IP改编授权", limit_time_basis="first_observed_5m")
    detector = EventBridgeDetector()
    assert detector.detect(DAY, events, policy_structured_event_clusters(events)) == []
    assert detector.trace[0]["time_sync"] is None
    assert "missing_launch_times" in detector.trace[0]["excluded_reasons"]
    for item in events:
        item["limit_time_basis"] = "observed_interval_5m"
    assert len(detector.detect(DAY, events, policy_structured_event_clusters(events))) == 1


def test_three_local_stocks_are_silent_without_ai_then_four_require_formal_review(service):
    db = service.database
    calls = []
    messages = []

    class Notifier:
        enabled = True

        def send_text(self, title, body):
            messages.append((title, body))

    service.notifier = Notifier()

    def verify(prompt, **kwargs):
        calls.append(prompt)
        return {}, {"clusters": [{
            "anchor_tag": "AI影视", "canonical_name": "AI短剧IP内容生产催化",
            "common_logic": "AI漫剧制作带动IP改编授权", "cluster_confidence": 90,
            "member_codes": [f"60000{i}.SH" for i in range(4)],
        }]}, []

    service.explainer._call_prompt = verify
    limits = {f"60000{i}.SH": (11, 9) for i in range(4)}
    db.upsert_mainboard_limit_events(DAY, [quote(i) for i in range(3)], limits)
    assert service.discover_trade_date(DAY, NOW) == []
    early = db.list_themes()[0]
    assert early["admission_status"] == "early_observation"
    assert calls == []
    assert messages == []
    assert db.recent_alerts(10) == []
    service.discover_trade_date(DAY, NOW + timedelta(minutes=5))
    assert messages == []
    db.upsert_mainboard_limit_events(DAY, [quote(3)], limits)
    ids = service.discover_trade_date(DAY, NOW + timedelta(minutes=10))
    assert ids == [early["id"]]  # Same stable anchor/theme ID as it grows.
    assert len(calls) == 1
    assert "AI漫剧制作与IP改编授权" in calls[0]
    candidate = db.get_theme(ids[0])
    assert candidate["status"] == "pending"  # Admission has not been bypassed.
    assert candidate["admission_status"] == "awaiting_ai"
    assert messages == []  # Formal admission must succeed before a notification is emitted.
    compact = _compact_evidence(candidate["members"][0]["evidence"])
    assert compact["canonical_name"] == "AI短剧IP内容生产催化"
    assert compact["common_logic"] == "AI漫剧制作带动IP改编授权"
    trace = json.loads(db.get_metadata(f"discovery_trace:{DAY}:" + db.get_metadata(
        f"discovery_trace_latest:{DAY}")))
    assert trace["input_coverage"]["mainboard_count"] == 4
    assert trace["input_coverage"]["source_counts"] == {"local_rt_k_limit": 4}


def test_historical_replay_does_not_push_retrospective_observations(service):
    service.database.upsert_mainboard_limit_events(
        DAY, [quote(i) for i in range(3)], {f"60000{i}.SH": (11, 9) for i in range(3)},
    )
    service.discover_trade_date(DAY, NOW + timedelta(days=1))
    assert service.database.list_themes()[0]["admission_status"] == "early_observation"
    assert service.database.recent_alerts(10) == []


def test_reason_change_invalidates_cache_without_resync_timestamp_churn():
    events = [event(i, "AI影视") for i in range(4)]
    events[0]["concept_tags"] = [{"tag": "AI影视", "reason": "AI平台原有功能"}]
    first = _semantic_event_signature(events)
    events[0]["concept_tags"][0]["observed_at"] = "2026-10-09T09:50:00+08:00"
    assert _semantic_event_signature(events) == first
    events[0]["concept_tags"][0]["reason"] = "AI平台上线IP改编功能"
    assert _semantic_event_signature(events) != first


def test_generic_price_backfill_reason_cannot_link_unrelated_sectors():
    events = [event(i, "出版" if i < 2 else "农业设备") for i in range(4)]
    for item in events:
        item["limit_reason"] = (
            "创业板当日最高涨幅12.00%，由正式日线回补为涨停等效样本；"
            "具体上涨原因结合题材标签、公司关联和当日新闻核查"
        )
    detector = EventBridgeDetector()
    assert detector.detect(DAY, events, policy_structured_event_clusters(events)) == []
    assert "insufficient_business_text" in detector.trace[0]["excluded_reasons"]
    assert detector.trace[0]["text_similarity"] == 0


def test_same_two_stocks_in_different_nodes_are_traced_as_below_four():
    events = [event(i, "AI影视", "版权") for i in range(2)]
    for item in events:
        item["limit_reason"] = "AI漫剧IP改编"
    detector = EventBridgeDetector()
    assert detector.detect(DAY, events, policy_structured_event_clusters(events)) == []
    assert detector.trace[0]["unique_member_count"] == 2
    assert detector.trace[0]["excluded_reason"] == "below_four_unique_members"


def test_empty_kpl_is_degraded_and_visible_in_health(service):
    class EmptyProvider:
        transport = "test"

        def kpl_list(self, *_args):
            return []

    service.provider = EmptyProvider()
    service.clock.china_now = lambda: NOW
    assert service.sync_kpl_events(DAY) == 0
    assert service.database.latest_run("sync_kpl_events")["status"] == "degraded"
    health = service.health()
    assert health["discovery_input_coverage"]["state"] == "empty_strong_feed"
    assert any("KPL" in warning for warning in health["warnings"])


def test_graph_rejection_preserves_specific_node_reason(service):
    service.database.upsert_mainboard_limit_events(
        DAY, [quote(i) for i in range(4)], {f"60000{i}.SH": (11, 9) for i in range(4)},
    )
    service.explainer._call_prompt = lambda *_args, **_kw: ({}, {
        "clusters": [], "rejected_nodes": [{"anchor_tag": "AI影视", "reason": "缺少新增催化"}],
    }, [])
    assert service.discover_trade_date(DAY, NOW) == []
    signature = service.database.get_metadata(f"discovery_trace_latest:{DAY}")
    trace = json.loads(service.database.get_metadata(f"discovery_trace:{DAY}:{signature}"))
    assert trace["single_node_verifications"][0]["reject_reason"] == "缺少新增催化"
    assert service.database.list_themes() == []


def test_collector_still_schedules_local_discovery_when_kpl_is_not_published(service):
    stocks = [{"ts_code": f"{600000+i:06d}.SH", "name": f"测试{i}", "market": "主板"}
              for i in range(2000)]
    service.database.upsert_stocks(stocks)
    service.database.upsert_daily_limits("2026-10-09", [
        {"ts_code": "600000.SH", "up_limit": 11, "down_limit": 9, "pre_close": 10},
    ])

    class Provider:
        def realtime_quotes(self, now):
            return [quote(code=s["ts_code"], name=s["name"], captured_at=now) for s in stocks]

        def kpl_list(self, *_args):
            raise AssertionError("The official KPL feed must not be polled intraday")

    service.provider = Provider()
    result = service.collect_once(NOW)
    assert result["status"] == "degraded"  # Quote ingestion continues with partial limit coverage.
    assert result["mainboard_touch_signals"] == 1
    assert result["discovery_trade_date"] == DAY
    coverage = json.loads(service.database.get_metadata(f"kpl_event_coverage:{DAY}"))
    assert coverage["state"] == "pending_publication"
    assert service.database.limit_touch_events(DAY)[0]["signal_source"] == "local_rt_k_limit"
