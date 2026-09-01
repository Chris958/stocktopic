import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.backtest import PaperTradeTracker
from stocktopic.db import Database
from stocktopic.domain import Quote

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

    def _quote(self, captured_at: datetime, **values):
        defaults = {
            "code": "600000.SH",
            "name": "浦发银行",
            "pre_close": 9.8,
            "open": 10.0,
            "high": 10.6,
            "low": 9.9,
            "close": 10.5,
            "volume": 1000,
            "amount": 10000.0,
            "trades": 100,
            "trade_time": captured_at.strftime("%H:%M:%S"),
            "captured_at": captured_at,
        }
        defaults.update(values)
        return Quote(**defaults)

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

    def test_realtime_quote_confirms_buy_and_updates_live_returns(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        captured = datetime(2026, 8, 27, 9, 30, 5, tzinfo=CN)

        result = self.tracker.update_realtime(
            [self._quote(captured, open=10, close=10.5, high=10.8)],
            "20260827",
            {"600000.SH": 10.78},
        )
        entry = self.db.list_test_pool_entries()[0]

        self.assertEqual(
            result,
            {
                "bought": 1,
                "marked": 1,
                "limit_pending": 0,
                "sold": 0,
                "exit_pending": 0,
                "exit_marked": 0,
            },
        )
        self.assertEqual(entry["status"], "awaiting_exit")
        self.assertEqual(entry["buy_open"], 10)
        self.assertEqual(entry["buy_confirmation_source"], "realtime_rt_k")
        self.assertEqual(entry["current_price"], 10.5)
        self.assertEqual(entry["current_return_pct"], 5.0)
        self.assertEqual(entry["current_high_return_pct"], 8.0)

        later = datetime(2026, 8, 27, 10, 0, 5, tzinfo=CN)
        self.tracker.update_realtime(
            [self._quote(later, open=10, close=9.9, high=11)],
            "20260827",
        )
        entry = self.db.list_test_pool_entries()[0]
        self.assertEqual(entry["current_return_pct"], -1.0)
        self.assertEqual(entry["current_high_return_pct"], 10.0)

    def test_realtime_limit_open_waits_until_price_range_opens(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        captured = datetime(2026, 8, 27, 9, 30, 5, tzinfo=CN)
        result = self.tracker.update_realtime(
            [self._quote(captured, open=11, high=11, low=11, close=11)],
            "20260827",
            {"600000.SH": 11},
        )
        pending = self.db.list_test_pool_entries()[0]
        self.assertEqual(result["limit_pending"], 1)
        self.assertEqual(pending["status"], "awaiting_buy")

        opened = datetime(2026, 8, 27, 9, 40, 5, tzinfo=CN)
        self.tracker.update_realtime(
            [self._quote(opened, open=11, high=11, low=10.8, close=10.9)],
            "20260827",
            {"600000.SH": 11},
        )
        bought = self.db.list_test_pool_entries()[0]
        self.assertEqual(bought["status"], "awaiting_exit")
        self.assertEqual(bought["buy_open"], 11)

    def test_official_daily_can_reverse_provisional_buy_to_unfilled(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        captured = datetime(2026, 8, 27, 9, 30, 5, tzinfo=CN)
        self.tracker.update_realtime(
            [self._quote(captured, open=10, high=10.2, low=9.9, close=10.1)],
            "20260827",
        )
        self._sync(
            "20260827", open=11, high=11, low=11, close=11, pre_close=10, pct_chg=10
        )

        self.tracker.settle()
        entry = self.db.list_test_pool_entries()[0]
        self.assertEqual(entry["status"], "unfilled")
        self.assertEqual(entry["buy_confirmation_source"], "official_daily")

    def test_realtime_quote_confirms_t_plus_two_open_exit(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync("20260827", open=10, high=10.5)
        self.tracker.settle()
        captured = datetime(2026, 8, 28, 9, 30, 5, tzinfo=CN)

        result = self.tracker.update_realtime(
            [self._quote(captured, pre_close=10.2, open=11, high=11.4, low=10.9, close=11.2)],
            "20260828",
        )
        entry = self.db.list_test_pool_entries()[0]

        self.assertEqual(result["sold"], 1)
        self.assertEqual(entry["status"], "awaiting_settlement")
        self.assertEqual(entry["exit_open"], 11)
        self.assertEqual(entry["standard_return_pct"], 10.0)
        self.assertEqual(entry["maximum_return_pct"], 14.0)
        self.assertEqual(self.db.test_pool_summary()["pending_count"], 1)

        later = datetime(2026, 8, 28, 14, 0, 5, tzinfo=CN)
        update = self.tracker.update_realtime(
            [self._quote(later, pre_close=10.2, open=11, high=12, low=10.9, close=11.5)],
            "20260828",
        )
        entry = self.db.list_test_pool_entries()[0]
        self.assertEqual(update["exit_marked"], 1)
        self.assertEqual(entry["maximum_return_pct"], 20.0)

    def test_realtime_one_price_limit_down_waits_for_price_range(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync("20260827", open=10)
        self.tracker.settle()
        captured = datetime(2026, 8, 28, 9, 30, 5, tzinfo=CN)

        pending_result = self.tracker.update_realtime(
            [self._quote(captured, pre_close=10, open=9, high=9, low=9, close=9)],
            "20260828",
            lower_limits={"600000.SH": 9},
        )
        pending = self.db.list_test_pool_entries()[0]
        self.assertEqual(pending_result["exit_pending"], 1)
        self.assertEqual(pending["status"], "awaiting_exit")

        opened = datetime(2026, 8, 28, 10, 0, 5, tzinfo=CN)
        sold_result = self.tracker.update_realtime(
            [self._quote(opened, pre_close=10, open=9, high=9.2, low=9, close=9.1)],
            "20260828",
            lower_limits={"600000.SH": 9},
        )
        sold = self.db.list_test_pool_entries()[0]
        self.assertEqual(sold_result["sold"], 1)
        self.assertEqual(sold["status"], "awaiting_settlement")
        self.assertEqual(sold["standard_return_pct"], -10.0)

    def test_official_daily_finalizes_realtime_exit(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync("20260827", open=10)
        self.tracker.settle()
        captured = datetime(2026, 8, 28, 9, 30, 5, tzinfo=CN)
        self.tracker.update_realtime(
            [self._quote(captured, open=11, high=11.4, low=10.9, close=11.2)],
            "20260828",
        )
        self._sync("20260828", open=11, high=12)

        result = self.tracker.settle()
        entry = self.db.list_test_pool_entries()[0]
        self.assertEqual(result["settled"], 1)
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["maximum_return_pct"], 20.0)

    def test_official_daily_reverses_unexecutable_realtime_exit(self):
        self.tracker.add(self.theme_id, "600000.SH", self.now)
        self._sync("20260827", open=10)
        self.tracker.settle()
        captured = datetime(2026, 8, 28, 10, 0, 5, tzinfo=CN)
        self.tracker.update_realtime(
            [self._quote(captured, pre_close=10, open=9, high=9.1, low=9, close=9.05)],
            "20260828",
            lower_limits={"600000.SH": 9},
        )
        self._sync(
            "20260828", open=9, high=9, low=9, close=9, pre_close=10, pct_chg=-10
        )

        result = self.tracker.settle()
        entry = self.db.list_test_pool_entries()[0]
        self.assertEqual(result["delayed"], 1)
        self.assertEqual(entry["status"], "awaiting_exit")
        self.assertEqual(entry["exit_attempt_date"], "20260831")
        self.assertIsNone(entry["exit_open"])

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
