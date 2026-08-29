import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.config import Settings
from stocktopic.domain import Quote
from stocktopic.service import StockTopicService, _admission_candidate_due

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
            "13:30",
        )

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
