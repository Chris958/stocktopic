import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.service import (
    StockTopicService,
    _admission_candidate_due,
    _semantic_event_signature,
)

CN = ZoneInfo("Asia/Shanghai")


class FailingIfCalledProvider:
    def realtime_quotes(self, captured_at):
        raise AssertionError("rt_k must not be called outside a valid market window")


class ServiceGuardTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = Settings(
            tushare_token="test",
            db_path=root / "test.sqlite3",
            archive_dir=root / "archive",
            admin_password="test",
            app_api_token="test",
        )
        self.service = StockTopicService(settings)
        self.service.database.initialize()
        self.service.provider = FailingIfCalledProvider()

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_calendar_never_calls_realtime_api(self):
        result = self.service.collect_once(datetime(2026, 8, 26, 10, 0, tzinfo=CN))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"], "calendar_unknown_fail_closed")

    def test_after_hours_never_calls_realtime_api(self):
        self.service.database.replace_calendar(
            [{"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"}]
        )
        result = self.service.collect_once(datetime(2026, 8, 26, 17, 0, tzinfo=CN))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["session"], "closed")

    def test_lunch_break_never_calls_realtime_api(self):
        self.service.database.replace_calendar(
            [{"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"}]
        )
        result = self.service.collect_once(datetime(2026, 8, 26, 12, 0, tzinfo=CN))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["session"], "lunch_break")

    def test_two_day_discovery_backfill_never_calls_realtime_api(self):
        self.service.database.replace_calendar(
            [
                {"cal_date": "20260825", "is_open": "1", "pretrade_date": "20260824"},
                {"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"},
            ]
        )
        self.service.database.upsert_stocks(
            [
                {"ts_code": f"60000{i}.SH", "name": f"股票{i}", "market": "主板"}
                for i in range(4)
            ]
        )
        self.service.database.upsert_kpl_events(
            [
                {
                    "trade_date": "20260825",
                    "ts_code": f"60000{i}.SH",
                    "name": f"股票{i}",
                    "tag": "涨停",
                    "theme": "停机期间题材",
                    "status": "首板",
                }
                for i in range(4)
            ]
        )
        result = self.service.backfill_recent_trade_days(
            datetime(2026, 8, 26, 18, 0, tzinfo=CN),
            refresh_sources=False,
            source="test",
        )
        self.assertEqual(result["trade_dates"], ["20260826", "20260825"])
        self.assertEqual(len(result["candidate_ids"]), 1)

    def test_catalyst_schedule_catches_up_to_latest_due_slot(self):
        self.assertEqual(
            self.service._catalyst_refresh_slot(datetime(2026, 8, 26, 9, 5, tzinfo=CN)),
            "08:40",
        )
        self.assertEqual(
            self.service._catalyst_refresh_slot(datetime(2026, 8, 26, 14, 10, tzinfo=CN)),
            "08:40",
        )

    def test_catalyst_refresh_skips_pending_and_reassesses_only_new_watching_evidence(self):
        def create(fingerprint, status):
            theme_id = self.service.database.upsert_candidate(
                fingerprint=fingerprint,
                provisional_name=f"{fingerprint}待审",
                shared_tag=fingerprint,
                direction="positive",
                discovered_at="2026-08-26T10:00:00+08:00",
                day1_date="2026-08-26",
                discovery_reason="4只共同强势股票",
                members=[],
            )
            if status != "pending":
                self.service.database.set_theme_status(theme_id, status, fingerprint)
            return theme_id

        watching_id = create("watching", "watching")
        confirmed_id = create("confirmed", "confirmed")
        pending_id = create("pending", "pending")
        calls = []

        class Explainer:
            enabled = True

            def explain(self, theme, names, existing):
                calls.append(int(theme["id"]))
                return {
                    "model": "test",
                    "suggested_name": theme["shared_tag"],
                    "explanation": "测试",
                    "catalyst_summary": "测试催化",
                    "catalyst_duration": "数日",
                    "merge_suggestions": [],
                    "sources": [],
                    "catalysts": [
                        {
                            "title": "同一催化",
                            "summary": "同一证据不应重复触发准入",
                            "url": "https://example.com/catalyst",
                        }
                    ],
                    "raw": {},
                }

        self.service.explainer = Explainer()
        reassessed = []
        self.service._assess_and_admit_candidates = lambda ids: reassessed.extend(ids)

        first = self.service.refresh_theme_catalysts(slot="08:40")
        second = self.service.refresh_theme_catalysts(slot="08:40")
        close = self.service.refresh_theme_catalysts(slot="15:30")

        self.assertEqual(first["new_catalysts"], 1)
        self.assertEqual(second["new_catalysts"], 0)
        self.assertEqual(reassessed, [watching_id])
        self.assertEqual(calls.count(watching_id), 3)
        self.assertEqual(calls.count(confirmed_id), 1)
        self.assertNotIn(pending_id, calls)
        self.assertEqual(close["reassessed"], 0)

    def test_failed_ai_candidate_retries_only_after_thirty_minute_cooldown(self):
        now = datetime(2026, 8, 29, 10, 0, tzinfo=CN)
        self.assertTrue(
            _admission_candidate_due({"admission_status": "awaiting_ai"}, now)
        )
        self.assertFalse(
            _admission_candidate_due(
                {
                    "admission_status": "analysis_failed",
                    "admission_reviewed_at": "2026-08-29T09:45:00+08:00",
                },
                now,
            )
        )
        self.assertTrue(
            _admission_candidate_due(
                {
                    "admission_status": "analysis_failed",
                    "admission_reviewed_at": "2026-08-29T09:29:59+08:00",
                },
                now,
            )
        )

    def test_stale_analyzing_candidate_is_retried_after_watchdog_timeout(self):
        now = datetime(2026, 8, 29, 10, 0, tzinfo=CN)
        self.assertFalse(
            _admission_candidate_due(
                {
                    "admission_status": "analyzing",
                    "admission_reviewed_at": "2026-08-29T09:50:00+08:00",
                },
                now,
            )
        )
        self.assertTrue(
            _admission_candidate_due(
                {
                    "admission_status": "analyzing",
                    "admission_reviewed_at": "2026-08-29T09:44:59+08:00",
                },
                now,
            )
        )

    def test_semantic_cache_ignores_board_open_close_but_not_new_reason_or_stock(self):
        base = [
            {
                "code": "600000.SH",
                "market": "主板",
                "board_tag": "涨停",
                "status": "首板",
                "themes": ["PTFE"],
                "limit_reason": "背板材料升级",
                "concept_tags": [{"tag": "英伟达"}],
            }
        ]
        changed_board = [{**base[0], "board_tag": "炸板", "status": "炸板"}]
        changed_reason = [{**base[0], "limit_reason": "新的独立事件"}]
        new_stock = [*base, {**base[0], "code": "600001.SH"}]

        self.assertEqual(
            _semantic_event_signature(base), _semantic_event_signature(changed_board)
        )
        self.assertNotEqual(
            _semantic_event_signature(base), _semantic_event_signature(changed_reason)
        )
        self.assertNotEqual(
            _semantic_event_signature(base), _semantic_event_signature(new_stock)
        )

    def test_auction_zero_prices_are_skipped_without_minus_one_hundred_events(self):
        self.service.database.replace_calendar(
            [{"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"}]
        )
        stocks = [
            {"ts_code": f"{600000 + index:06d}.SH", "name": f"股票{index}", "market": "主板"}
            for index in range(2000)
        ]
        self.service.database.upsert_stocks(stocks)
        captured_at = datetime(2026, 8, 26, 9, 20, tzinfo=CN)

        class ZeroAuctionProvider:
            def realtime_quotes(self, _):
                return [
                    Quote(
                        code=stock["ts_code"],
                        name=stock["name"],
                        pre_close=10,
                        open=0,
                        high=0,
                        low=0,
                        close=0,
                        volume=0,
                        amount=0,
                        trades=0,
                        trade_time="09:20:00",
                        captured_at=captured_at,
                    )
                    for stock in stocks
                ]

        self.service.provider = ZeroAuctionProvider()
        result = self.service.collect_once(captured_at)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["invalid_quotes"], 2000)
        self.assertEqual(self.service.database.latest_quotes(), [])
        self.assertIsNone(self.service.database.latest_anomaly_trade_date())
