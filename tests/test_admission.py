import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocktopic.config import Settings
from stocktopic.service import StockTopicService, _admission_evidence_grade

CN = ZoneInfo("Asia/Shanghai")


def test_two_independent_supply_chain_sources_count_as_multi_source_evidence():
    catalysts = [
        {
            "url": "https://industry-a.example/report",
            "evidence_level": "供应链未确认",
            "source_kind": "supply_chain_report",
        },
        {
            "url": "https://industry-b.example/research",
            "evidence_level": "供应链未确认",
            "source_kind": "brokerage_research",
        },
    ]
    assert _admission_evidence_grade(catalysts) == "multi_source"


class PassingAdmissionExplainer:
    enabled = True

    def assess_for_admission(self, theme, historical_matches, eligible_stock_pool):
        return {
            "model": "test-model",
            "suggested_name": "液冷超节点",
            "is_new_theme": True,
            "novelty_confidence": 88,
            "novelty_reason": "60个交易日内没有同逻辑主升",
            "catalyst_summary": "头部厂商发布新一代液冷超节点",
            "catalyst_confidence": 82,
            "expected_duration_days": 4,
            "duration_reason": "订单和产品发布存在后续节点",
            "leader_candidate_code": "600000.SH",
            "leader_upside_scenario_pct": 35,
            "upside_scenario_reason": "龙头在订单兑现情景下存在空间",
            "counter_evidence": ["订单延期"],
            "proposed_members": [
                {
                    "code": "600004.SH",
                    "name": "扩展股票",
                    "role": "核心",
                    "reason": "高置信概念成员",
                }
            ],
            "sources": [{"url": "https://example.com/official", "title": "官方发布"}],
            "catalysts": [
                {
                    "title": "新一代液冷超节点发布",
                    "summary": "产品发布形成首次催化",
                    "source": "官方",
                    "url": "https://example.com/official",
                    "published_at": "2026-08-26T08:00:00+08:00",
                    "catalyst_type": "首次催化",
                    "evidence_level": "官方确认",
                    "source_kind": "company_disclosure",
                }
            ],
            "raw": {"test": True},
        }


class CapturingNotifier:
    enabled = True

    def __init__(self):
        self.messages = []

    def send_text(self, title, body):
        self.messages.append((title, body))


def test_candidate_is_auto_admitted_only_after_ai_and_member_validation():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        service = StockTopicService(
            Settings(
                tushare_token="test",
                db_path=root / "test.sqlite3",
                archive_dir=root / "archive",
                openai_api_key="test",
                admin_password="test",
                app_api_token="test",
            )
        )
        service.database.initialize()
        service.explainer = PassingAdmissionExplainer()
        service.notifier = CapturingNotifier()
        service.database.replace_calendar(
            [{"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"}]
        )
        service.database.upsert_stocks(
            [{"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"} for i in range(5)]
        )
        service.database.upsert_kpl_events(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": f"60000{i}.SH",
                    "name": f"股票{i}",
                    "tag": "涨停" if i < 3 else "炸板",
                    "theme": "液冷超节点",
                    "status": "首板",
                    "lu_time": f"093{i}00",
                }
                for i in range(4)
            ]
        )
        service.database.upsert_kpl_concept_members(
            [
                {
                    "ts_code": "000999.KP",
                    "name": "液冷超节点",
                    "con_name": "扩展股票",
                    "con_code": "600004.SH",
                    "trade_date": "20260826",
                }
            ]
        )
        now = datetime(2026, 8, 26, 10, 0, tzinfo=CN)
        candidate_ids = service.discovery.discover(now)
        assert len(candidate_ids) == 1
        service._assess_and_admit_candidates(candidate_ids)
        theme = service.database.get_theme(candidate_ids[0])
        assert theme["status"] == "confirmed"
        assert theme["admission_status"] == "admitted"
        assert theme["admission_review"]["expected_duration_days"] == 4
        assert {member["code"] for member in theme["members"]} == {
            "600000.SH",
            "600001.SH",
            "600002.SH",
            "600003.SH",
            "600004.SH",
        }
        assert service.notifier.messages[0][0] == "正式题材：液冷超节点"


class SupplyChainAdmissionExplainer(PassingAdmissionExplainer):
    def assess_for_admission(self, theme, historical_matches, eligible_stock_pool):
        item = super().assess_for_admission(theme, historical_matches, eligible_stock_pool)
        item["catalysts"][0]["evidence_level"] = "供应链未确认"
        item["catalysts"][0]["source_kind"] = "supply_chain_report"
        item["suggested_name"] = "英伟达PTFE正交背板"
        return item


def test_supply_chain_theme_enters_early_watch_and_pushes_without_formal_confirmation():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        service = StockTopicService(
            Settings(
                tushare_token="test",
                db_path=root / "test.sqlite3",
                archive_dir=root / "archive",
                openai_api_key="test",
                admin_password="test",
                app_api_token="test",
            )
        )
        service.database.initialize()
        service.explainer = SupplyChainAdmissionExplainer()
        service.notifier = CapturingNotifier()
        service.database.replace_calendar(
            [{"cal_date": "20260827", "is_open": "1", "pretrade_date": "20260826"}]
        )
        service.database.upsert_stocks(
            [
                {"ts_code": f"60000{i}.SH", "name": f"PTFE股票{i}", "market": "主板"}
                for i in range(4)
            ]
        )
        service.database.upsert_kpl_events(
            [
                {
                    "trade_date": "20260827",
                    "ts_code": f"60000{i}.SH",
                    "name": f"PTFE股票{i}",
                    "tag": "涨停",
                    "theme": "PTFE" if i < 2 else "氟化工、PCB材料",
                    "status": "首板",
                    "lu_time": f"13{i}000",
                    "lu_desc": "Rubin Ultra正交背板PTFE选材",
                }
                for i in range(4)
            ]
        )
        clusters = [
            {
                "tag": "英伟达PTFE正交背板",
                "common_logic": "Rubin Ultra背板材料升级",
                "member_codes": [f"60000{i}.SH" for i in range(4)],
                "aliases": ["PTFE", "氟化工", "PCB材料"],
                "cluster_confidence": 89,
                "cluster_method": "semantic_event",
                "catalysts": [],
            }
        ]
        events = service.database.limit_touch_events("20260827")
        by_code = {item["code"]: item for item in events}
        semantic = [
            {
                **clusters[0],
                "members": [by_code[code] for code in clusters[0]["member_codes"]],
                "touch_count": 4,
                "sealed_count": 4,
                "failed_count": 0,
            }
        ]
        ids = service.discovery.discover_for_date(
            "20260827", datetime(2026, 8, 28, 9, 0, tzinfo=CN), semantic
        )
        service._assess_and_admit_candidates(ids)
        theme = service.database.get_theme(ids[0])
        assert theme["status"] == "watching"
        assert theme["theme_level"] == "early_watch"
        assert theme["evidence_grade"] == "supply_chain_unconfirmed"
        assert theme["admission_review"]["decision_level"] == "early_watch"
        assert service.notifier.messages[0][0] == "早期观察：英伟达PTFE正交背板"


class RepeatedThemeExplainer(PassingAdmissionExplainer):
    def assess_for_admission(self, theme, historical_matches, eligible_stock_pool):
        item = super().assess_for_admission(theme, historical_matches, eligible_stock_pool)
        item["is_new_theme"] = False
        item["novelty_confidence"] = 25
        item["novelty_reason"] = "过去60个交易日已反复发酵"
        return item


def test_four_touch_candidate_keeps_a_visible_rejection_reason():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        service = StockTopicService(
            Settings(
                tushare_token="test",
                db_path=root / "test.sqlite3",
                archive_dir=root / "archive",
                openai_api_key="test",
                admin_password="test",
                app_api_token="test",
            )
        )
        service.database.initialize()
        service.explainer = RepeatedThemeExplainer()
        service.notifier = CapturingNotifier()
        service.database.replace_calendar(
            [{"cal_date": "20260827", "is_open": "1", "pretrade_date": "20260826"}]
        )
        service.database.upsert_stocks(
            [
                {"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"}
                for i in range(4)
            ]
        )
        service.database.upsert_kpl_events(
            [
                {
                    "trade_date": "20260827",
                    "ts_code": f"60000{i}.SH",
                    "name": f"股票{i}",
                    "tag": "涨停",
                    "theme": "反复轮动题材",
                    "status": "首板",
                    "lu_time": f"10{i}000",
                }
                for i in range(4)
            ]
        )
        ids = service.discovery.discover_for_date(
            "20260827", datetime(2026, 8, 27, 14, 0, tzinfo=CN)
        )
        service._assess_and_admit_candidates(ids)
        theme = service.database.get_theme(ids[0])
        assert theme["status"] == "rejected"
        assert theme["theme_level"] == "rejected"
        assert theme["admission_status"] == "not_admitted"
        assert "不属于首次广泛发酵" in theme["admission_reason"]
        assert theme["admission_review"]["decision_level"] == "rejected"
        assert service.notifier.messages == []


class BackfillExplainer(PassingAdmissionExplainer):
    def cluster_limit_events(self, trade_date, events, minimum_members):
        return [
            {
                "tag": "英伟达PTFE正交背板",
                "common_logic": "Rubin Ultra背板材料升级",
                "member_codes": [event["code"] for event in events],
                "aliases": ["PTFE", "氟化工", "PCB材料"],
                "cluster_confidence": 90,
                "cluster_method": "semantic_event",
                "catalysts": [],
                "model": "test-model",
            }
        ]


def test_two_trade_day_backfill_replays_missed_previous_day_candidate():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        service = StockTopicService(
            Settings(
                tushare_token="test",
                db_path=root / "test.sqlite3",
                archive_dir=root / "archive",
                openai_api_key="test",
                admin_password="test",
                app_api_token="test",
            )
        )
        service.database.initialize()
        service.explainer = BackfillExplainer()
        service.notifier = CapturingNotifier()
        service.database.replace_calendar(
            [
                {"cal_date": "20260827", "is_open": "1", "pretrade_date": "20260826"},
                {"cal_date": "20260828", "is_open": "1", "pretrade_date": "20260827"},
            ]
        )
        service.database.upsert_stocks(
            [{"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"} for i in range(4)]
        )
        service.database.upsert_kpl_events(
            [
                {
                    "trade_date": "20260827",
                    "ts_code": f"60000{i}.SH",
                    "name": f"股票{i}",
                    "tag": "涨停",
                    "theme": "PTFE" if i < 2 else "PCB材料",
                    "status": "首板",
                    "lu_time": f"13{i}000",
                    "lu_desc": "Rubin Ultra正交背板材料升级",
                }
                for i in range(4)
            ]
        )
        result = service.backfill_recent_trade_days(
            datetime(2026, 8, 28, 18, 0, tzinfo=CN),
            refresh_sources=False,
            source="test",
        )
        assert result["trade_dates"] == ["20260828", "20260827"]
        assert len(result["candidate_ids"]) == 1
        theme = service.database.get_theme(result["candidate_ids"][0])
        assert theme["day1_date"] == "2026-08-27"
        assert theme["status"] == "confirmed"
        assert service.database.get_metadata("last_discovery_backfill")
