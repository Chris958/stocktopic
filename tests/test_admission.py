import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocktopic.config import Settings
from stocktopic.service import StockTopicService

CN = ZoneInfo("Asia/Shanghai")


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
                    "evidence_level": "明确证据",
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
                    "ts_code": "600004.SH",
                    "name": "扩展股票",
                    "con_name": "液冷超节点",
                    "con_code": "885999",
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
        assert service.notifier.messages[0][0] == "新重点题材：液冷超节点"
