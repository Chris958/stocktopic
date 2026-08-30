import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.backtest import PaperTradeTracker
from stocktopic.db import Database

CN = ZoneInfo("Asia/Shanghai")


class TestPoolTrackerTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "test.sqlite3", root / "archive")
        self.db.initialize()
        self.db.replace_calendar(
            [
                {"cal_date": "20260826", "is_open": "1", "pretrade_date": "20260825"},
                {"cal_date": "20260827", "is_open": "1", "pretrade_date": "20260826"},
                {"cal_date": "20260828", "is_open": "1", "pretrade_date": "20260827"},
                {"cal_date": "20260831", "is_open": "1", "pretrade_date": "20260828"},
            ]
        )
        self.theme_id = self._theme("测试题材")
        self.tracker = PaperTradeTracker(self.db)
        self.now = datetime(2026, 8, 26, 14, 0, tzinfo=CN)

    def tearDown(self):
        self.temp.cleanup()

    def _theme(self, name: str) -> int:
        return self.db.upsert_candidate(
            fingerprint=f"fingerprint-{name}",
            provisional_name=name,
            shared_tag=name,
            direction="positive",
            discovered_at="2026-08-26T10:00:00+08:00",
            day1_date="2026-08-26",
            discovery_reason="测试回测",
            members=[{"code": "600000.SH", "name": "浦发银行", "evidence": {}}],
        )

    def _sync(self, trade_date: str, **bar):
        defaults = {
            "ts_code": "600000.SH",
            "trade_date": trade_date,
            "open": 10,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "pre_close": 9.8,
            "pct_chg": 4.0,
            "vol": 100,
            "amount": 1000,
        }
        defaults.update(bar)
        self.db.upsert_daily_bars([defaults])
        self.db.set_metadata(f"daily_prices_synced:{trade_date}", "true")

    def test_normal_trade_uses_t_plus_one_open_and_t_plus_two_open_high(self):
        entry, created = self.tracker.add(self.theme_id, "600000.SH", self.now)
        self.assertTrue(created)
        self.assertEqual(entry["planned_buy_date"], "20260827")
        self.assertEqual(entry["planned_exit_date"], "20260828")
        self._sync("20260827", open=10, high=10.8)
        self._sync("20260828", open=11, high=12)

        result = self.tracker.settle()
        settled = self.db.list_test_pool_entries()[0]

        self.assertEqual(result["settled"], 1)
        self.assertEqual(settled["status"], "success")
        self.assertEqual(settled["standard_return_pct"], 10.0)
        self.assertEqual(settled["maximum_return_pct"], 20.0)
        self.assertEqual(self.db.test_pool_summary()["success_rate"], 100.0)

    def test_same_stock_and_signal_date_merges_theme_sources(self):
        second_theme = self._theme("第二题材")
        first, first_created = self.tracker.add(self.theme_id, "600000.SH", self.now)
        second, second_created = self.tracker.add(second_theme, "600000.SH", self.now)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            {item["name"] for item in second["source_themes"]},
            {"测试题材", "第二题材"},
        )

    def test_one_price_limit_up_is_unfilled_and_excluded(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync(
            "20260827",
            open=11,
            high=11,
            low=11,
            close=11,
            pre_close=10,
            pct_chg=10,
        )

        self.tracker.settle()
        entry = self.db.list_test_pool_entries()[0]
        summary = self.db.test_pool_summary()

        self.assertEqual(entry["status"], "unfilled")
        self.assertEqual(summary["completed_count"], 0)
        self.assertEqual(summary["unfilled_count"], 1)

    def test_one_price_limit_down_delays_exit_until_tradable_open(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync("20260827", open=10, high=10.5)
        self._sync(
            "20260828",
            open=9,
            high=9,
            low=9,
            close=9,
            pre_close=10,
            pct_chg=-10,
        )

        self.tracker.settle()
        delayed = self.db.list_test_pool_entries()[0]
        self.assertEqual(delayed["status"], "awaiting_exit")
        self.assertEqual(delayed["exit_attempt_date"], "20260831")

        self._sync("20260831", open=8.5, high=9)
        self.tracker.settle()
        settled = self.db.list_test_pool_entries()[0]

        self.assertEqual(settled["status"], "failure")
        self.assertEqual(settled["actual_exit_date"], "20260831")
        self.assertEqual(settled["exit_delay_trade_days"], 1)
        self.assertEqual(settled["standard_return_pct"], -15.0)

    def test_flat_trade_is_separate_from_success_rate(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync("20260827", open=10)
        self._sync("20260828", open=10, high=10.5)

        self.tracker.settle()
        summary = self.db.test_pool_summary()

        self.assertEqual(self.db.list_test_pool_entries()[0]["status"], "flat")
        self.assertEqual(summary["flat_count"], 1)
        self.assertIsNone(summary["success_rate"])
        self.assertEqual(summary["average_standard_return_pct"], 0.0)
