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
        self.order_calls: list[str] = []
        self.trade_windows: list[tuple[str, str, str | None]] = []

    def trade_history(
        self,
        symbol: str,
        trade_date: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ):
        self.trade_calls.append(trade_date)
        self.trade_windows.append((symbol, trade_date, end_time))
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

    def order_history(
        self,
        symbol: str,
        trade_date: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ):
        self.order_calls.append(trade_date)
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

    def test_completed_report_is_reused_without_provider_calls(self):
        provider = Level2ProviderStub("20260831")
        self.service.level2_provider = provider
        now = datetime(2026, 9, 1, 17, 0, tzinfo=CN)
        self.service.analyze_level2_stock("603269.SH", now=now)
        cached_provider = Level2ProviderStub(None)
        self.service.level2_provider = cached_provider

        report = self.service.analyze_level2_stock("603269.SH", now=now)

        self.assertTrue(report["cache_hit"])
        self.assertEqual(report["trade_date"], "20260831")
        self.assertEqual(cached_provider.trade_calls, [])
        self.assertEqual(cached_provider.order_calls, [])

    def test_force_refresh_bypasses_completed_report(self):
        provider = Level2ProviderStub("20260831")
        self.service.level2_provider = provider
        now = datetime(2026, 9, 1, 10, 0, tzinfo=CN)
        self.service.analyze_level2_stock("603269.SH", now=now)
        provider.trade_calls.clear()
        provider.order_calls.clear()

        report = self.service.analyze_level2_stock(
            "603269.SH", now=now, force_refresh=True
        )

        self.assertFalse(report["cache_hit"])
        self.assertEqual(provider.trade_calls, ["20260831"])
        self.assertEqual(provider.order_calls, ["20260831"])

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

    def test_scheduled_flow_tracks_theme_top5_and_unsold_test_pool_only(self):
        theme_codes = [f"60010{index}.SH" for index in range(6)]
        awaiting_code = "000001.SZ"
        sold_code = "000002.SZ"
        archived_code = "601000.SH"
        self.service.database.upsert_stocks(
            [
                {"ts_code": code, "name": f"测试{code[:6]}", "market": "主板"}
                for code in [*theme_codes, awaiting_code, sold_code, archived_code]
            ]
        )
        theme_id = self.service.database.upsert_candidate(
            fingerprint="fund-flow-active",
            provisional_name="资金测试题材",
            shared_tag="资金测试",
            direction="positive",
            discovered_at="2026-09-01T09:40:00+08:00",
            day1_date="2026-09-01",
            discovery_reason="测试TOP5资金调度",
            members=[{"code": code, "name": f"测试{code[:6]}"} for code in theme_codes],
        )
        self.service.database.set_theme_status(theme_id, "confirmed", "资金测试题材")
        archived_id = self.service.database.upsert_candidate(
            fingerprint="fund-flow-archived",
            provisional_name="已归档题材",
            shared_tag="归档",
            direction="positive",
            discovered_at="2026-09-01T09:40:00+08:00",
            day1_date="2026-09-01",
            discovery_reason="不应更新",
            members=[{"code": archived_code, "name": "已归档股票"}],
        )
        self.service.database.set_theme_status(archived_id, "confirmed", "已归档题材")
        self.service.database.archive_theme(archived_id)
        awaiting, _ = self.service.database.add_test_pool_entry(
            code=awaiting_code,
            name="待卖股票",
            signal_trade_date="20260831",
            planned_buy_date="20260901",
            planned_exit_date="20260902",
            source_theme={"id": theme_id, "name": "资金测试题材"},
        )
        self.service.database.update_test_pool_entry(
            int(awaiting["id"]), status="awaiting_exit", buy_open=10
        )
        sold, _ = self.service.database.add_test_pool_entry(
            code=sold_code,
            name="已卖股票",
            signal_trade_date="20260828",
            planned_buy_date="20260831",
            planned_exit_date="20260901",
            source_theme={"id": theme_id, "name": "资金测试题材"},
        )
        self.service.database.update_test_pool_entry(
            int(sold["id"]), status="awaiting_settlement", buy_open=10, exit_open=11
        )
        provider = Level2ProviderStub("20260901")
        self.service.level2_provider = provider
        now = datetime(2026, 9, 1, 10, 0, tzinfo=CN)
        expected_targets = self.service._fund_flow_targets()

        result = self.service.refresh_fund_flows("morning", now)

        expected_codes = {str(item["code"]) for item in expected_targets}
        expected_symbols = {code.split(".", 1)[0] for code in expected_codes}
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["stock_count"], 6)
        self.assertEqual({item[0] for item in provider.trade_windows}, expected_symbols)
        self.assertTrue(all(item[2] == "10:00:00" for item in provider.trade_windows))
        self.assertNotIn(archived_code, expected_codes)
        self.assertNotIn(sold_code, expected_codes)
        self.assertEqual(provider.order_calls, [])

        themes = self.service.database.list_themes()
        self.service.database.attach_theme_fund_flows(
            themes, "20260901", "morning"
        )
        active_theme = next(item for item in themes if int(item["id"]) == theme_id)
        self.assertEqual(active_theme["fund_flow"]["status"], "completed")
        self.assertEqual(active_theme["fund_flow"]["completed_count"], 5)
        self.assertEqual(active_theme["fund_flow"]["summary"]["report_count"], 5)
        entries = self.service.database.list_test_pool_entries()
        self.service.database.attach_test_pool_fund_flows(
            entries, "20260901", "morning"
        )
        awaiting_entry = next(item for item in entries if int(item["id"]) == awaiting["id"])
        sold_entry = next(item for item in entries if int(item["id"]) == sold["id"])
        self.assertEqual(awaiting_entry["fund_flow"]["status"], "completed")
        self.assertEqual(sold_entry["fund_flow"]["status"], "stopped")

    def test_fund_flow_slots_catch_up_after_due_times(self):
        morning = datetime(2026, 9, 1, 10, 0, tzinfo=CN)
        close = datetime(2026, 9, 1, 17, 10, tzinfo=CN)
        self.assertEqual(self.service._due_fund_flow_slots(morning, True), ["morning"])
        self.assertEqual(
            self.service._due_fund_flow_slots(close, True), ["morning", "close"]
        )
        self.assertEqual(self.service._due_fund_flow_slots(close, False), [])
