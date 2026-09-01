import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.config import Settings
from stocktopic.providers import NumcatError
from stocktopic.service import StockTopicService

CN = ZoneInfo("Asia/Shanghai")


class Level2ProviderStub:
    enabled = True

    def __init__(self, available_date: str | None):
        self.available_date = available_date
        self.trade_calls: list[str] = []

    def trade_history(self, symbol: str, trade_date: str):
        self.trade_calls.append(trade_date)
        if trade_date != self.available_date:
            raise NumcatError(1002, "未找到 Level-2 数据")
        return [
            {
                "symbol": symbol,
                "tradedate": trade_date,
                "time": "09:35:01.001",
                "trade_id": "1",
                "price": 12.5,
                "volume": 50000,
                "amount": 625000,
                "bs_flag": "B",
                "buy_order_id": "10086",
                "sell_order_id": "20001",
            }
        ]

    def order_history(self, symbol: str, trade_date: str):
        return []


class Level2ServiceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = Settings(
            tushare_token="test",
            db_path=root / "test.sqlite3",
            archive_dir=root / "archive",
            numcat_api_key="test",
            admin_password="test",
            app_api_token="test",
        )
        self.service = StockTopicService(settings)
        self.service.database.initialize()
        self.service.database.replace_calendar(
            [
                {"cal_date": "20260828", "is_open": "1", "pretrade_date": "20260827"},
                {"cal_date": "20260831", "is_open": "1", "pretrade_date": "20260828"},
                {"cal_date": "20260901", "is_open": "1", "pretrade_date": "20260831"},
            ]
        )
        self.service.database.upsert_stocks(
            [{"ts_code": "603269.SH", "name": "海鸥股份", "market": "主板"}]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_automatic_date_falls_back_when_today_is_not_available(self):
        provider = Level2ProviderStub("20260831")
        self.service.level2_provider = provider

        report = self.service.analyze_level2_stock(
            "603269.SH", now=datetime(2026, 9, 1, 17, 0, tzinfo=CN)
        )

        self.assertEqual(report["trade_date"], "20260831")
        self.assertEqual(provider.trade_calls, ["20260901", "20260831"])

    def test_intraday_skips_current_unsettled_date(self):
        provider = Level2ProviderStub("20260831")
        self.service.level2_provider = provider

        report = self.service.analyze_level2_stock(
            "603269.SH", now=datetime(2026, 9, 1, 10, 0, tzinfo=CN)
        )

        self.assertEqual(report["trade_date"], "20260831")
        self.assertEqual(provider.trade_calls, ["20260831"])

    def test_explicit_date_does_not_silently_fall_back(self):
        provider = Level2ProviderStub(None)
        self.service.level2_provider = provider

        with self.assertRaisesRegex(
            RuntimeError, r"海鸥股份\(603269\.SH\).*20260901.*1002"
        ):
            self.service.analyze_level2_stock(
                "603269.SH",
                "20260901",
                now=datetime(2026, 9, 1, 17, 0, tzinfo=CN),
            )
        self.assertEqual(provider.trade_calls, ["20260901"])
