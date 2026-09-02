import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.db import Database
from stocktopic.domain import Anomaly, Direction, Quote
from stocktopic.themes import ThemeDiscovery


class ThemeDatabaseTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "test.sqlite3", root / "archive")
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def create_candidate(self):
        return self.db.upsert_candidate(
            fingerprint="abc",
            provisional_name="农业异动",
            shared_tag="农业",
            direction="positive",
            discovered_at="2026-08-26T10:00:00+08:00",
            day1_date="2026-08-26",
            discovery_reason="4只股票共享农业标签",
            members=[
                {"code": f"60000{i}.SH", "name": f"股票{i}", "evidence": {"rule": "test"}}
                for i in range(4)
            ],
        )

    def test_pending_candidate_has_no_score(self):
        theme_id = self.create_candidate()
        theme = self.db.get_theme(theme_id)
        self.assertEqual(theme["status"], "pending")
        self.assertIsNone(theme["score"])

    def test_ai_usage_is_aggregated_by_task_without_estimating_missing_usage(self):
        self.db.record_ai_usage(
            {
                "task_type": "catalyst_refresh",
                "subject_id": "1",
                "model": "test-model",
                "prompt_chars": 1200,
                "input_tokens": 900,
                "cached_input_tokens": 500,
                "output_tokens": 200,
                "reasoning_tokens": 50,
                "total_tokens": 1100,
                "web_search_calls": 1,
                "usage_reported": 1,
                "request_controls_mode": "full",
            }
        )
        self.db.record_ai_usage(
            {
                "task_type": "semantic_event_clustering",
                "model": "relay-model",
                "prompt_chars": 3000,
                "usage_reported": 0,
                "request_controls_mode": "legacy",
            }
        )
        summary = self.db.ai_usage_summary("2000-01-01T00:00:00+00:00")
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["reported_calls"], 1)
        self.assertEqual(summary["total_tokens"], 1100)
        self.assertEqual(summary["prompt_chars"], 4200)
        self.assertFalse(summary["usage_complete"])

    def test_confirmation_preserves_day_one(self):
        theme_id = self.create_candidate()
        discovery = ThemeDiscovery(self.db)
        discovery.confirm(theme_id, "粮食安全")
        theme = self.db.get_theme(theme_id)
        self.assertEqual(theme["status"], "confirmed")
        self.assertEqual(theme["day1_date"], "2026-08-26")
        self.assertEqual(theme["final_name"], "粮食安全")

    def test_split_is_human_controlled(self):
        theme_id = self.create_candidate()
        discovery = ThemeDiscovery(self.db)
        new_id = discovery.split(theme_id, ["600000.SH"], "种业")
        source = self.db.get_theme(theme_id)
        child = self.db.get_theme(new_id)
        self.assertEqual(sum(member["active"] for member in source["members"]), 3)
        self.assertEqual(child["members"][0]["membership_source"], "human_split")

    def test_universe_includes_main_board_and_chinext_only(self):
        count = self.db.upsert_stocks(
            [
                {"ts_code": "600000.SH", "name": "浦发银行", "market": "主板"},
                {"ts_code": "002001.SZ", "name": "新和成", "market": "主板"},
                {"ts_code": "300001.SZ", "name": "特锐德", "market": "创业板"},
                {"ts_code": "301001.SZ", "name": "凯淳股份", "market": "创业板"},
                {"ts_code": "688001.SH", "name": "科创公司", "market": "科创板"},
                {"ts_code": "200001.SZ", "name": "深发展B", "market": "主板"},
                {"ts_code": "900901.SH", "name": "云赛B股", "market": "主板"},
                {"ts_code": "600001.SH", "name": "*ST测试", "market": "主板"},
            ]
        )
        self.assertEqual(count, 4)
        self.assertEqual(
            set(self.db.active_stock_map()),
            {"600000.SH", "002001.SZ", "300001.SZ", "301001.SZ"},
        )

    def test_chinext_intraday_high_above_ten_percent_is_a_qualifying_signal(self):
        self.db.upsert_stocks(
            [{"ts_code": "300001.SZ", "name": "特锐德", "market": "创业板"}]
        )
        self.db.upsert_kpl_concept_members(
            [
                {
                    "trade_date": "20260831",
                    "ts_code": "000001.KP",
                    "name": "AI影视",
                    "con_code": "300001.SZ",
                    "con_name": "特锐德",
                }
            ]
        )
        captured = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        count = self.db.upsert_chinext_growth_events(
            "20260901",
            [
                Quote(
                    code="300001.SZ",
                    name="特锐德",
                    pre_close=10,
                    open=10.2,
                    high=11.2,
                    low=10.1,
                    close=10.8,
                    volume=100,
                    amount=1000,
                    trades=20,
                    trade_time="10:00:00",
                    captured_at=captured,
                )
            ],
        )
        events = self.db.limit_touch_events("20260901")
        self.assertEqual(count, 1)
        self.assertEqual(events[0]["board_tag"], "创业板涨幅超10%")
        self.assertAlmostEqual(events[0]["pct_change"], 12.0)
        self.assertAlmostEqual(events[0]["realtime_pct_change"], 8.0)
        self.assertEqual(events[0]["themes"], ["AI影视"])
        self.assertEqual(events[0]["market"], "创业板")

    def test_chinext_growth_signal_can_be_rebuilt_from_daily_high(self):
        self.db.upsert_stocks(
            [{"ts_code": "300001.SZ", "name": "特锐德", "market": "创业板"}]
        )
        count = self.db.upsert_chinext_daily_growth_events(
            "20260901",
            [
                {
                    "ts_code": "300001.SZ",
                    "pre_close": 10,
                    "open": 10.1,
                    "high": 11.5,
                    "low": 9.9,
                    "close": 10.6,
                    "amount": 1000,
                }
            ],
        )
        event = self.db.limit_touch_events("20260901")[0]
        self.assertEqual(count, 1)
        self.assertEqual(event["board_tag"], "创业板涨幅超10%")
        self.assertAlmostEqual(event["pct_change"], 15.0)
        self.assertAlmostEqual(event["realtime_pct_change"], 6.0)

    def test_main_board_and_chinext_signals_share_the_four_stock_threshold(self):
        self.db.upsert_stocks(
            [
                *[
                    {"ts_code": f"60000{i}.SH", "name": f"主板{i}", "market": "主板"}
                    for i in range(3)
                ],
                {"ts_code": "300001.SZ", "name": "创业板0", "market": "创业板"},
            ]
        )
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260901",
                    "ts_code": f"60000{i}.SH",
                    "name": f"主板{i}",
                    "tag": "涨停",
                    "theme": "AI漫剧上星",
                    "status": "首板",
                    "lu_desc": "AI漫剧新作进入电视台播出",
                }
                for i in range(3)
            ]
        )
        self.db.upsert_kpl_concept_members(
            [
                {
                    "trade_date": "20260901",
                    "ts_code": "000001.KP",
                    "name": "AI漫剧上星",
                    "con_code": "300001.SZ",
                    "con_name": "创业板0",
                }
            ]
        )
        captured = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.db.upsert_chinext_growth_events(
            "20260901",
            [
                Quote(
                    code="300001.SZ",
                    name="创业板0",
                    pre_close=10,
                    open=10.2,
                    high=11.2,
                    low=10.1,
                    close=11.1,
                    volume=100,
                    amount=1000,
                    trades=20,
                    trade_time="10:00:00",
                    captured_at=captured,
                )
            ],
        )
        ids = ThemeDiscovery(self.db, minimum_limit_touches=4).discover(captured)
        theme = self.db.get_theme(ids[0])
        self.assertEqual(len(theme["members"]), 4)
        self.assertIn("创业板涨幅超10%1只", theme["discovery_reason"])

    def test_kpl_themes_become_deterministic_stock_tags(self):
        self.db.upsert_stocks([{"ts_code": "600000.SH", "name": "浦发银行", "market": "主板"}])
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "tag": "涨停",
                    "theme": "金融科技、并购重组",
                    "status": "2连板",
                }
            ]
        )
        tags = self.db.tags_for_codes(["600000.SH"])["600000.SH"]
        self.assertEqual({item["tag"] for item in tags}, {"金融科技", "并购重组"})

    def test_latest_trade_day_anomalies_remain_available_after_hours(self):
        captured_at = datetime(2026, 8, 26, 14, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.db.save_anomalies(
            [
                Anomaly(
                    code="600000.SH",
                    name="浦发银行",
                    captured_at=captured_at,
                    direction=Direction.POSITIVE,
                    severity=80,
                    pct_change=7.5,
                    change_5m=2.1,
                    amount_delta=10_000_000,
                    trade_delta=500,
                    is_hard_event=False,
                    event_types=("rapid_rise",),
                    reasons=("5分钟快速拉升",),
                )
            ]
        )
        self.assertEqual(self.db.latest_anomaly_trade_date(), "2026-08-26")
        items = self.db.anomalies_for_trade_date("2026-08-26")
        self.assertEqual(items[0]["code"], "600000.SH")
        self.assertEqual(items[0]["event_types"], ["rapid_rise"])

    def test_theme_members_include_market_stage_and_are_sorted_by_current_gain(self):
        self.db.upsert_stocks(
            [
                {"ts_code": "600000.SH", "name": "龙头股份", "market": "主板"},
                {"ts_code": "600001.SH", "name": "跟随股份", "market": "主板"},
            ]
        )
        theme_id = self.db.upsert_candidate(
            fingerprint="market-context",
            provisional_name="算力配套异动",
            shared_tag="算力配套",
            direction="positive",
            discovered_at="2026-08-26T10:00:00+08:00",
            day1_date="2026-08-26",
            discovery_reason="测试",
            members=[
                {"code": "600000.SH", "name": "龙头股份"},
                {"code": "600001.SH", "name": "跟随股份"},
            ],
        )
        self.db.set_theme_status(theme_id, "confirmed", "算力配套")
        captured_at = datetime(2026, 8, 26, 14, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.db.save_quotes(
            [
                Quote(
                    code="600000.SH",
                    name="龙头股份",
                    pre_close=10,
                    open=10,
                    high=11,
                    low=10,
                    close=11,
                    volume=100,
                    amount=1000,
                    trades=50,
                    trade_time="14:55:00",
                    captured_at=captured_at,
                ),
                Quote(
                    code="600001.SH",
                    name="跟随股份",
                    pre_close=10,
                    open=10,
                    high=10.5,
                    low=10,
                    close=10.5,
                    volume=100,
                    amount=1000,
                    trades=50,
                    trade_time="14:55:00",
                    captured_at=captured_at,
                ),
            ],
            "2026-08-26",
            "14:55",
        )
        self.db.upsert_daily_metrics(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": "600000.SH",
                    "circ_mv": 350000,
                    "turnover_rate": 12.5,
                },
                {
                    "trade_date": "20260826",
                    "ts_code": "600001.SH",
                    "circ_mv": 900000,
                    "turnover_rate": 6.2,
                },
            ]
        )
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": "600000.SH",
                    "name": "龙头股份",
                    "tag": "涨停",
                    "theme": "算力配套",
                    "status": "3连板",
                    "lu_time": "093105",
                    "last_time": "093105",
                    "limit_order": 50000000,
                }
            ]
        )
        theme = self.db.get_theme(theme_id)
        self.assertEqual(theme["members"][0]["code"], "600000.SH")
        self.assertEqual(theme["members"][0]["board_height"], 3)
        self.assertEqual(theme["members"][0]["limit_sequence"], 1)
        self.assertEqual(theme["members"][0]["circ_mv_billion"], 35.0)
        self.assertEqual(theme["market_summary"]["limit_up_count"], 1)

    def test_high_signal_anomalies_are_deduplicated_and_include_tags(self):
        self.db.upsert_stocks(
            [{"ts_code": "600000.SH", "name": "浦发银行", "market": "主板", "industry": "银行"}]
        )
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "tag": "涨停",
                    "theme": "金融科技",
                    "status": "首板",
                }
            ]
        )
        base = datetime(2026, 8, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        for minute, severity in ((0, 70), (5, 82)):
            self.db.save_anomalies(
                [
                    Anomaly(
                        code="600000.SH",
                        name="浦发银行",
                        captured_at=base.replace(minute=minute),
                        direction=Direction.POSITIVE,
                        severity=severity,
                        pct_change=7.5,
                        change_5m=2.8,
                        amount_delta=80_000_000,
                        trade_delta=900,
                        is_hard_event=False,
                        event_types=("rapid_rise",),
                        reasons=("量价齐升",),
                    )
                ]
            )
        items = self.db.high_signal_anomalies_for_trade_date("2026-08-26")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["severity"], 82)
        self.assertIn("金融科技", items[0]["themes"])
        self.assertIn("银行", items[0]["industries"])

    def test_catalysts_are_deduplicated(self):
        theme_id = self.create_candidate()
        catalyst = {
            "title": "隔夜算力需求超预期",
            "summary": "海外业绩强化算力基础设施需求预期",
            "source": "测试媒体",
            "url": "https://example.com/news",
            "published_at": "2026-08-26T22:00:00+08:00",
            "catalyst_type": "强化催化",
            "evidence_level": "合理推断",
        }
        self.assertEqual(self.db.save_theme_catalysts(theme_id, [catalyst]), 1)
        self.assertEqual(self.db.save_theme_catalysts(theme_id, [catalyst]), 0)
        self.assertEqual(len(self.db.get_theme(theme_id)["catalysts"]), 1)

    def test_limit_touch_discovery_requires_four_and_counts_failed_boards(self):
        self.db.upsert_stocks(
            [{"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"} for i in range(4)]
        )
        now = datetime(2026, 8, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        first_three = [
            {
                "trade_date": "20260826",
                "ts_code": f"60000{i}.SH",
                "name": f"股票{i}",
                "tag": "涨停" if i < 2 else "炸板",
                "theme": "新型储能",
                "status": "首板",
                "lu_time": f"093{i}00",
            }
            for i in range(3)
        ]
        self.db.upsert_kpl_events(first_three)
        discovery = ThemeDiscovery(self.db, minimum_limit_touches=4)
        self.assertEqual(discovery.discover(now), [])
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": "600003.SH",
                    "name": "股票3",
                    "tag": "炸板",
                    "theme": "新型储能",
                    "status": "首板",
                    "lu_time": "093300",
                }
            ]
        )
        ids = discovery.discover(now)
        self.assertEqual(len(ids), 1)
        theme = self.db.get_theme(ids[0])
        self.assertEqual(len(theme["members"]), 4)
        self.assertEqual(theme["admission_status"], "awaiting_ai")
        self.assertIn("炸板2只", theme["discovery_reason"])

    def test_pin_archive_and_restore_are_reversible(self):
        theme_id = self.create_candidate()
        self.db.set_theme_status(theme_id, "confirmed", "粮食安全")
        self.db.set_theme_pin(theme_id, True)
        self.assertEqual(self.db.get_theme(theme_id)["pinned"], 1)
        self.db.archive_theme(theme_id)
        archived = self.db.get_theme(theme_id)
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["pinned"], 0)
        self.db.restore_theme(theme_id)
        self.assertEqual(self.db.get_theme(theme_id)["status"], "confirmed")

    def test_early_watch_restore_preserves_its_evidence_level(self):
        theme_id = self.create_candidate()
        self.db.set_theme_status(theme_id, "watching", "供应链待确认题材")
        self.db.archive_theme(theme_id)
        self.db.restore_theme(theme_id)
        theme = self.db.get_theme(theme_id)
        self.assertEqual(theme["status"], "watching")
        self.assertEqual(theme["theme_level"], "early_watch")

    def test_kpl_concept_member_maps_stock_code_and_concept_name(self):
        self.db.upsert_stocks([{"ts_code": "600000.SH", "name": "测试股份", "market": "主板"}])
        count = self.db.upsert_kpl_concept_members(
            [
                {
                    "ts_code": "000229.KP",
                    "name": "液冷服务器",
                    "con_name": "测试股份",
                    "con_code": "600000.SH",
                    "trade_date": "20260826",
                }
            ]
        )
        self.assertEqual(count, 1)
        pool = self.db.eligible_members_for_tag("液冷服务器")
        self.assertEqual(pool[0]["code"], "600000.SH")
        self.assertEqual(pool[0]["matched_tags"], ["液冷服务器"])

    def test_semantic_event_cluster_combines_different_source_labels(self):
        self.db.upsert_stocks(
            [{"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"} for i in range(4)]
        )
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260827",
                    "ts_code": f"60000{i}.SH",
                    "name": f"股票{i}",
                    "tag": "涨停" if i < 3 else "炸板",
                    "theme": "PTFE" if i == 0 else "氟化工" if i == 1 else "PCB材料",
                    "status": "首板",
                    "lu_time": f"13{i}000",
                    "lu_desc": "Rubin Ultra正交背板材料升级",
                }
                for i in range(4)
            ]
        )
        events = self.db.limit_touch_events("20260827")
        semantic = [
            {
                "tag": "英伟达PTFE正交背板",
                "common_logic": "Rubin Ultra背板材料升级",
                "members": events,
                "touch_count": 4,
                "sealed_count": 3,
                "failed_count": 1,
                "aliases": ["PTFE", "氟化工", "PCB材料"],
                "cluster_confidence": 91,
                "cluster_method": "semantic_event",
                "catalysts": [],
            }
        ]
        discovery = ThemeDiscovery(self.db, minimum_limit_touches=4)
        ids = discovery.discover_for_date(
            "20260827",
            datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            semantic,
        )
        theme = self.db.get_theme(ids[0])
        self.assertEqual(theme["shared_tag"], "英伟达PTFE正交背板")
        self.assertEqual(theme["day1_date"], "2026-08-27")
        self.assertEqual(theme["cluster_method"], "semantic_event")
        self.assertEqual(set(theme["cluster_aliases"]), {"PTFE", "氟化工", "PCB材料"})

    def test_every_qualifying_semantic_cluster_is_persisted_without_a_run_cap(self):
        self.db.upsert_stocks(
            [
                {"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"}
                for i in range(8)
            ]
        )
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260827",
                    "ts_code": f"60000{i}.SH",
                    "name": f"股票{i}",
                    "tag": "涨停",
                    "theme": "事件甲" if i < 4 else "事件乙",
                    "status": "首板",
                    "lu_time": f"10{i}000",
                }
                for i in range(8)
            ]
        )
        by_code = {
            item["code"]: item for item in self.db.limit_touch_events("20260827")
        }
        semantic = []
        for name, codes in (
            ("事件甲", [f"60000{i}.SH" for i in range(4)]),
            ("事件乙", [f"60000{i}.SH" for i in range(4, 8)]),
        ):
            semantic.append(
                {
                    "tag": name,
                    "common_logic": f"{name}的共同催化",
                    "members": [by_code[code] for code in codes],
                    "touch_count": 4,
                    "sealed_count": 4,
                    "failed_count": 0,
                    "aliases": [name],
                    "cluster_confidence": 90,
                    "cluster_method": "semantic_event",
                    "catalysts": [],
                }
            )
        ids = ThemeDiscovery(self.db, minimum_limit_touches=4).discover_for_date(
            "20260827",
            datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            semantic,
        )
        self.assertEqual(len(ids), 2)
        self.assertEqual(
            {theme["shared_tag"] for theme in self.db.list_themes()},
            {"事件甲", "事件乙"},
        )

    def test_existing_v2_database_is_migrated_without_losing_theme(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE candidate_themes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    provisional_name TEXT NOT NULL,
                    suggested_name TEXT,
                    final_name TEXT,
                    shared_tag TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    direction TEXT NOT NULL DEFAULT 'positive',
                    discovered_at TEXT NOT NULL,
                    day1_date TEXT NOT NULL,
                    confirmed_at TEXT,
                    merged_into_id INTEGER,
                    discovery_reason TEXT NOT NULL,
                    catalyst_strength REAL NOT NULL DEFAULT 0,
                    catalyst_duration TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO candidate_themes(
                    fingerprint, provisional_name, shared_tag, discovered_at,
                    day1_date, discovery_reason, updated_at
                ) VALUES ('legacy', '旧候选', '旧题材', '2026-08-20T10:00:00+08:00',
                          '2026-08-20', '旧规则产生', '2026-08-20T10:00:00+08:00')
                """
            )
        legacy = Database(legacy_path, Path(self.temp.name) / "legacy-archive")
        legacy.initialize()
        theme = legacy.get_theme(1)
        self.assertEqual(theme["provisional_name"], "旧候选")
        self.assertEqual(theme["admission_status"], "legacy")
        self.assertEqual(theme["pinned"], 0)
        self.assertEqual(theme["theme_level"], "candidate")
        self.assertEqual(theme["cluster_aliases"], [])
