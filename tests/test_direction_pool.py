"""Direction identity, renewed resonance and daily pool updates; no live APIs."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.market_clock import SHANGHAI
from stocktopic.service import StockTopicService
from stocktopic.theme_directions import event_direction, member_relation


def now(day=16, hour=10, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI)


def event(i, tag="人形机器人", day=16, reason="人形机器人量产推进"):
    return {
        "trade_date": f"202610{day}",
        "ts_code": f"6000{i:02}.SH",
        "name": f"合成股票{i}",
        "tag": "涨停",
        "theme": tag,
        "status": "首板",
        "lu_time": "093500",
        "lu_desc": reason,
    }


@pytest.fixture
def service(tmp_path, monkeypatch):
    s = StockTopicService(
        Settings(
            tushare_token="test",
            openai_api_key="test",
            db_path=tmp_path / "db.sqlite3",
            archive_dir=tmp_path / "archive",
        )
    )
    s.initialize_storage()
    s.database.replace_calendar(
        [{"cal_date": f"202610{d:02}", "is_open": int(now(d).weekday() < 5)} for d in range(8, 31)]
    )
    s.database.upsert_stocks(
        [{"ts_code": f"6000{i:02}.SH", "name": f"合成股票{i}", "market": "主板"} for i in range(30)]
    )
    s.test_now = now()
    s.clock.china_now = lambda: s.test_now
    monkeypatch.setattr("stocktopic.db.utc_now_iso", lambda: s.test_now.isoformat())
    s.graph_calls, s.admission_calls = [], []
    s.ai_reject = False
    s.old_rotation = False
    s.low_quality = False
    s.during_admission = None

    def graph(prompt, **kwargs):
        nodes = json.loads(prompt.split("图谱候选：")[1].split("\n输入强势股票：")[0])
        s.graph_calls.append(nodes)
        assert all(len(set(n["member_codes"])) >= 4 for n in nodes)
        return (
            {},
            {
                "clusters": []
                if s.ai_reject
                else [
                    {
                        "anchor_tag": n["anchor_tag"],
                        "canonical_name": "低空运营政策落地"
                        if n["anchor_tag"] == "低空经济"
                        else "特斯拉人形机器人量产进展",
                        "common_logic": "低空运营与基础设施"
                        if n["anchor_tag"] == "低空经济"
                        else "人形机器人产业化与部件需求",
                        "cluster_confidence": 90,
                        "member_codes": [*n["member_codes"], "999999.SH"],
                        "member_reasons": [
                            {"code": code, "reason": "已核查同方向业务"}
                            for code in n["member_codes"]
                        ],
                        "catalysts": [
                            {
                                "title": "合成产品发布",
                                "summary": "合成产业化催化",
                                "url": "https://example.com/robot",
                                "source_kind": "official",
                                "evidence_level": "官方确认",
                            }
                        ],
                    }
                    for n in nodes
                ]
            },
            [{"url": "https://example.com/robot", "title": "合成公告"}],
        )

    def assess(theme, history, pool):
        s.admission_calls.append(theme)
        if s.during_admission:
            s.during_admission()
        return {
            "model": "test",
            "suggested_name": "特斯拉人形机器人量产催化",
            "event_name": "特斯拉人形机器人量产进展",
            "novelty_mode": "old_rotation" if s.old_rotation else "new_catalyst",
            "is_new_theme": not s.old_rotation,
            "novelty_confidence": 5 if s.old_rotation else 90,
            "novelty_reason": "已存在方向的延续" if s.old_rotation else "产业化推进",
            "within_window_match_ids": [h["id"] for h in history],
            "catalyst_confidence": 10 if s.low_quality else 90,
            "catalyst_summary": "合成机器人产品催化",
            "expected_duration_days": 4,
            "duration_reason": "量产节点",
            "leader_candidate_code": theme["members"][0]["code"],
            "leader_upside_scenario_pct": 35,
            "upside_scenario_reason": "条件情景",
            "proposed_members": [],
            "sources": [{"url": "https://example.com/robot"}],
            "catalysts": [
                {
                    "title": "合成产品发布",
                    "summary": "产业化需求",
                    "url": "https://example.com/robot",
                    "source_kind": "official",
                    "evidence_level": "官方确认",
                }
            ],
            "raw": {},
        }

    s.explainer._call_prompt = graph
    s.explainer.assess_for_admission = assess
    return s


def discover(service, rows, current=None):
    if current:
        service.test_now = current
    day = service.test_now.strftime("%Y%m%d")
    service.database.upsert_kpl_events(rows)
    ids = service.discover_trade_date(day, service.test_now)
    service._assess_and_admit_candidates(ids)
    return ids


def formal(service):
    return service.database.list_themes(status="confirmed")


def test_tesla_robot_catalyst_creates_robot_direction_not_a_small_event(service):
    ids = discover(service, [event(i) for i in range(4)])
    assert len(ids) == 1
    theme = formal(service)[0]
    assert theme["final_name"] == "机器人"
    assert theme["direction_state"]["latest_event_name"] == "特斯拉人形机器人量产进展"
    assert len(theme["members"]) == 4
    assert "999999.SH" not in {m["code"] for m in theme["members"]}
    assert theme["catalysts"]
    assert service.database.recent_alerts()[0]["title"] == "正式题材：机器人"


def test_later_catalysts_and_tags_reuse_the_same_formal_direction(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    old_calls = len(service.admission_calls)
    discover(service, [event(i, tag="具身智能", day=19) for i in range(2, 7)], now(19))
    assert [t["id"] for t in formal(service)] == [original]
    assert len(service.admission_calls) == old_calls
    assert {m["code"] for m in formal(service)[0]["members"]} == {
        f"6000{i:02}.SH" for i in range(7)
    }
    assert len(service.database.recent_alerts()) == 1


def test_one_new_limit_up_is_added_to_existing_pool_without_ai(service):
    discover(service, [event(i) for i in range(4)])
    graph_calls = len(service.graph_calls)
    discover(service, [event(4, day=19)], now(19))
    theme = formal(service)[0]
    added = next(m for m in theme["members"] if m["code"] == "600004.SH")
    assert added["membership_source"] == "direction_daily_strength"
    assert added["evidence"]["trade_date"] == "20261019"
    assert added["first_seen_at"] == now(19).isoformat()
    assert len(service.graph_calls) == graph_calls


def test_archived_three_stock_observation_tracks_without_ai_or_reentry(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    old_calls, old_alerts = len(service.graph_calls), len(service.database.recent_alerts())
    discover(service, [event(i, day=19) for i in range(3)], now(19))
    assert formal(service) == []
    assert service.database.get_theme(original)["status"] == "archived"
    assert len(service.graph_calls) == old_calls
    assert len(service.database.recent_alerts()) == old_alerts
    trace = json.loads(service.database.get_metadata(f"direction_tracking:20261019:{original}"))
    assert trace["touch_count"] == 3


def test_archived_direction_reenters_on_four_fresh_stocks_despite_old_rotation(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    discover(service, [event(i, day=19) for i in range(1, 6)], now(19))
    assert [t["id"] for t in formal(service)] == [original]
    theme = formal(service)[0]
    assert theme["final_name"] == "机器人" and theme["archived_at"] is None
    assert theme["day1_date"] == "2026-10-16"  # Reentry must not disguise an old cycle as Day 1.
    assert theme["admission_review"]["is_new_theme"] == 0
    assert theme["admission_review"]["novelty_confidence"] == 5
    assert theme["admission_review"]["admitted"] == 1
    assert len(service.database.recent_alerts()) == 2
    assert any(
        n.get("known_direction", {}).get("known_formal")
        for call in service.graph_calls
        for n in call
    )


def test_unchanged_same_day_snapshot_does_not_immediately_undo_manual_archive(service):
    rows = [event(i) for i in range(4)]
    discover(service, rows)
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    discover(service, rows, now(hour=11, minute=5))
    assert formal(service) == []
    assert service.database.get_theme(original)["status"] == "archived"


def test_same_day_new_limit_up_can_reactivate_an_archived_direction(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    discover(service, [event(i) for i in range(5)], now(hour=11, minute=5))
    assert [t["id"] for t in formal(service)] == [original]
    assert len(formal(service)[0]["members"]) == 5


@pytest.mark.parametrize("reject,low_quality", [(True, False), (False, True)])
def test_reentry_keeps_graph_and_evidence_gates(service, reject, low_quality):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.ai_reject, service.low_quality = reject, low_quality
    service.old_rotation = True
    discover(service, [event(i, day=19) for i in range(4)], now(19))
    assert formal(service) == []
    assert service.database.get_theme(original)["status"] == "archived"


def test_archive_during_admission_cannot_be_overridden_by_inflight_response(service):
    def archive_candidate():
        service.database.archive_theme(service.admission_calls[-1]["id"])

    service.during_admission = archive_candidate
    discover(service, [event(i) for i in range(4)])
    assert formal(service) == []
    assert service.database.list_themes()[0]["status"] == "archived"


def test_historical_replay_does_not_reactivate_today_archived_pool(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(20)
    service.database.archive_theme(original)
    service.database.upsert_kpl_events([event(i, day=19) for i in range(5)])
    ids = service.discover_trade_date("20261019", service.test_now)
    service._assess_and_admit_candidates(ids)
    assert service.database.get_theme(original)["status"] == "archived"
    assert formal(service) == []


def test_robot_and_low_air_keep_independent_formal_pools_and_local_updates(service):
    rows = [event(i) for i in range(4)]
    rows += [event(i, "低空经济", reason="低空经济运营政策") for i in range(10, 14)]
    discover(service, rows)
    pools = {t["final_name"]: t for t in formal(service)}
    assert set(pools) == {"机器人", "低空经济"}
    old_calls = len(service.graph_calls)
    discover(
        service,
        [event(i, day=19) for i in range(4, 7)]
        + [event(i, "低空经济", day=19, reason="低空经济运营政策") for i in range(14, 17)],
        now(19),
    )
    assert len(service.graph_calls) == old_calls
    pools = {t["final_name"]: t for t in formal(service)}
    assert len(pools["机器人"]["members"]) == 7
    assert len(pools["低空经济"]["members"]) == 7


def test_existing_duplicate_robot_formal_events_consolidate_without_losing_members(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    source = service.database.upsert_candidate(
        fingerprint="legacy-robot-event",
        provisional_name="旧机器人订单催化",
        shared_tag="机器人概念",
        direction="positive",
        discovered_at=now().isoformat(),
        day1_date="2026-10-16",
        discovery_reason="历史已验证方向",
        members=[{"code": "600004.SH", "name": "合成股票4", "evidence": {}}],
    )
    service.database.set_theme_status(source, "confirmed", "旧机器人订单催化")
    service.direction_pool.normalize_existing(service.test_now)
    assert [t["id"] for t in formal(service)] == [original]
    assert len(formal(service)[0]["members"]) == 5
    assert service.database.get_theme(source)["merged_into_id"] == original


def test_collector_adds_new_limit_up_before_next_discovery_slot(service):
    discover(service, [event(i) for i in range(4)])
    old_calls = len(service.graph_calls)
    service.test_now = now(19, minute=5)
    service.settings = replace(service.settings, cluster_interval_minutes=10)
    stocks = [
        {"ts_code": f"{600000 + i:06}.SH", "name": f"合成{i}", "market": "主板"}
        for i in range(2000)
    ]
    service.database.upsert_stocks(stocks)
    service.database.upsert_kpl_concept_members(
        [
            {
                "trade_date": "20261016",
                "ts_code": "concept",
                "name": "人形机器人",
                "con_code": "600004.SH",
                "desc": "机器人零部件业务",
            }
        ]
    )

    class Provider:
        def stock_limits(self, day):
            return [
                {"ts_code": s["ts_code"], "trade_date": day, "up_limit": 11, "down_limit": 9}
                for s in stocks
            ]

        def realtime_quotes(self, current):
            return [
                Quote(
                    code=s["ts_code"],
                    name=s["name"],
                    pre_close=10,
                    open=10,
                    low=10,
                    high=11 if i == 4 else 10.2,
                    close=11 if i == 4 else 10.1,
                    volume=10000,
                    amount=1000000,
                    trades=100,
                    trade_time="10:05:00",
                    captured_at=current,
                )
                for i, s in enumerate(stocks)
            ]

        def kpl_list(self, *_):
            pytest.fail("Intraday KPL must not be polled")

    service.provider = Provider()
    result = service.collect_once(service.test_now)
    assert result["status"] == "success"
    assert result["discovery_trade_date"] is None
    assert any(m["code"] == "600004.SH" for m in formal(service)[0]["members"])
    assert len(service.graph_calls) == old_calls


def test_readmission_records_novelty_exception_without_forging_confidence(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    discover(service, [event(i, day=19) for i in range(4)], now(19))
    trace = json.loads(service.database.get_metadata(f"admission_trace:{original}"))
    assert trace["admission_route"] == "direction_continuation"
    assert trace["novelty_required"] is False
    assert trace["event_candidate_id"] != original
    assert formal(service)[0]["admission_review"]["novelty_confidence"] == 5


def test_archived_local_observation_can_receive_its_first_formal_admission(service):
    discover(service, [event(i) for i in range(3)])
    original = service.database.list_themes()[0]["id"]
    assert formal(service) == [] and service.graph_calls == []
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    discover(service, [event(i) for i in range(5)], now(hour=11, minute=5))
    assert [t["id"] for t in formal(service)] == [original]
    assert len(formal(service)[0]["members"]) == 5
    assert all(len(n["member_codes"]) >= 4 for call in service.graph_calls for n in call)


def test_rejected_readmission_can_reopen_after_new_market_evidence(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    service.low_quality = True
    discover(service, [event(i, day=19) for i in range(4)], now(19))
    assert formal(service) == []
    before = len(service.admission_calls)
    service.low_quality = False
    discover(service, [event(i, day=19) for i in range(5)], now(19, minute=5))
    assert len(service.admission_calls) > before
    assert [t["id"] for t in formal(service)] == [original]
    assert len(formal(service)[0]["members"]) == 5
    assert service.database.recent_alerts()[0]["title"] == "正式题材重新入池：机器人"


def test_rearchiving_during_readmission_keeps_the_newer_archive(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    service.during_admission = lambda: service.database.archive_theme(original)
    discover(service, [event(i, day=19) for i in range(5)], now(19))
    assert formal(service) == []
    assert service.database.get_theme(original)["status"] == "archived"
    assert len(service.database.recent_alerts()) == 1


def test_other_directions_can_be_generalized_and_keep_validated_local_tag_edges(service):
    original_graph = service.explainer._call_prompt

    def industrial_graph(*args, **kwargs):
        raw, parsed, sources = original_graph(*args, **kwargs)
        for cluster in parsed["clusters"]:
            cluster.update(
                direction_name="工业母机",
                canonical_name="五轴数控系统升级催化",
                common_logic="工业母机的五轴数控系统国产化",
            )
        return raw, parsed, sources

    service.explainer._call_prompt = industrial_graph
    discover(service, [event(i, "五轴数控系统", reason="工业母机数控系统升级") for i in range(4)])
    assert formal(service)[0]["final_name"] == "工业母机"
    assert formal(service)[0]["direction_state"]["validated_tags"] == ["五轴数控系统"]
    calls = len(service.graph_calls)
    discover(service, [event(4, "五轴数控系统", day=19, reason="工业母机数控系统升级")], now(19))
    assert len(formal(service)[0]["members"]) == 5
    assert len(service.graph_calls) == calls


def test_readmitted_direction_links_to_new_market_cycle_without_resetting_original_day1(service):
    discover(service, [event(i) for i in range(4)])
    original = formal(service)[0]["id"]
    service.test_now = now(hour=11)
    service.database.archive_theme(original)
    service.old_rotation = True
    service.database.set_metadata("market_signal_coverage:20261027", '{"mainboard_available":true}')
    discover(service, [event(i, day=27) for i in range(4)], now(27))
    theme = formal(service)[0]
    assert theme["id"] == original and theme["day1_date"] == "2026-10-16"
    robot = next(c for c in service.market_cycles.dashboard(now(27)) if c["family"] == "机器人")
    assert robot["start_trade_date"] == "20261027"
    assert robot["event_admission"] == "linked_active_event"


def test_tesla_or_bearing_labels_alone_do_not_force_robot_membership():
    assert event_direction("轴承", "特斯拉人形机器人量产", "机器人部件需求") == "机器人"
    assert event_direction("轴承", "机器人与低空经济", "两个方向分别活跃") is None
    assert event_direction("轴承", "机器人与低空经济", "两个方向分别活跃", "机器人") == "机器人"
    assert (
        member_relation({"code": "600000.SH", "themes": ["轴承", "特斯拉概念"]}, "机器人", set())
        is None
    )
    assert (
        member_relation(
            {"code": "600000.SH", "themes": ["人形机器人"], "limit_reason": "低空经济订单"},
            "机器人",
            set(),
        )
        is None
    )
