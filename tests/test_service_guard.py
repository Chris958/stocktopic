import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.config import Settings
from stocktopic.service import StockTopicService

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
        self.service.database.replace_calendar([
            {"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"}
        ])
        result = self.service.collect_once(datetime(2026, 8, 26, 17, 0, tzinfo=CN))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["session"], "closed")

    def test_lunch_break_never_calls_realtime_api(self):
        self.service.database.replace_calendar([
            {"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"}
        ])
        result = self.service.collect_once(datetime(2026, 8, 26, 12, 0, tzinfo=CN))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["session"], "lunch_break")

