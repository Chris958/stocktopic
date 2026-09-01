import tempfile
from pathlib import Path
from unittest import TestCase

from stocktopic.db import Database
from stocktopic.level2 import analyze_level2_orders, format_level2_report


class Level2OrderFlowTests(TestCase):
    def setUp(self):
        self.trades = [
            _trade("09:42:11.021", 180_000, "B", 10086, 90001),
            _trade("09:42:11.021", 230_000, "B", 10086, 90002),
            _trade("09:42:11.022", 310_000, "B", 10086, 90003),
            _trade("10:01:00.000", 1_200_000, "B", 10087, 90004),
            _trade("10:15:00.000", 250_000, "S", 10088, 20001),
            _trade("10:15:00.500", 350_000, "S", 10089, 20001),
            _trade("10:30:00.000", 100_000, "?", 10090, 20002),
        ]

    def test_aggregates_multiple_fills_by_aggressor_order_id(self):
        report = analyze_level2_orders(
            self.trades,
            [{"side": "B", "order_type": "A"}, {"side": "S", "order_type": "D"}],
            code="603269.SH",
            name="海鸥股份",
            trade_date="20260901",
        )
        large, super_large = report["thresholds"]
        self.assertEqual(large["buy_amount"], 1_920_000)
        self.assertEqual(large["sell_amount"], 600_000)
        self.assertEqual(large["net_inflow"], 1_320_000)
        self.assertAlmostEqual(large["buy_ratio_pct"], 76.19)
        self.assertEqual(large["buy_order_count"], 2)
        self.assertEqual(large["sell_order_count"], 1)
        self.assertEqual(super_large["buy_amount"], 1_200_000)
        self.assertEqual(super_large["sell_amount"], 0)
        sweep = next(item for item in report["events"] if item["order_id"] == "10086")
        self.assertEqual(sweep["amount"], 720_000)
        self.assertEqual(sweep["fill_count"], 3)
        self.assertEqual(sweep["event_type"], "continuous_sweep")
        self.assertEqual(sweep["duration_ms"], 1)

    def test_unknown_direction_is_excluded_and_reported_in_coverage(self):
        report = analyze_level2_orders(
            self.trades,
            code="603269.SH",
            name="海鸥股份",
            trade_date="20260901",
        )
        self.assertEqual(report["raw_profile"]["bs_flag"]["?"], 1)
        self.assertLess(report["coverage"]["directional_amount_coverage_pct"], 100)
        self.assertEqual(report["coverage"]["grouped_trade_count"], 6)

    def test_formats_requested_compact_report(self):
        report = analyze_level2_orders(
            self.trades,
            code="603269.SH",
            name="海鸥股份",
            trade_date="20260901",
        )
        output = format_level2_report(report)
        self.assertIn("50W+", output)
        self.assertIn("买入 76%", output)
        self.assertIn("████", output)
        self.assertIn("大单净主动流入：+132万", output)

    def test_report_is_persisted_without_raw_tick_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "test.sqlite3", root / "archive")
            database.initialize()
            report = analyze_level2_orders(
                self.trades,
                code="603269.SH",
                name="海鸥股份",
                trade_date="20260901",
            )
            database.save_level2_report(report)
            saved = database.get_level2_report("603269.SH", "20260901")
            self.assertEqual(saved["thresholds"][0]["net_inflow"], 1_320_000)


def _trade(time: str, amount: float, flag: str, buy_order: int, sell_order: int):
    return {
        "time": time,
        "price": 10,
        "volume": amount / 10,
        "amount": amount,
        "bs_flag": flag,
        "trade_code": "成交",
        "buy_order_id": buy_order,
        "sell_order_id": sell_order,
    }
