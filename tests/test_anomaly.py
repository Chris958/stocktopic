from datetime import datetime, timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.anomaly import AnomalyDetector
from stocktopic.domain import Direction, Quote, StockContext

CN = ZoneInfo("Asia/Shanghai")


def quote(**changes):
    values = {
        "code": "600001.SH",
        "name": "测试股份",
        "pre_close": 10.0,
        "open": 10.1,
        "high": 10.8,
        "low": 10.0,
        "close": 10.7,
        "volume": 10_000_000,
        "amount": 120_000_000.0,
        "trades": 5_000,
        "trade_time": "2026-08-26 10:00:00",
        "captured_at": datetime(2026, 8, 26, 10, 0, tzinfo=CN),
    }
    values.update(changes)
    return Quote(**values)


def history(at: datetime, close: float, amount: float, trades: int):
    return {"captured_at": at.isoformat(), "close": close, "amount": amount, "trades": trades}


class AnomalyDetectorTests(TestCase):
    def setUp(self):
        self.detector = AnomalyDetector()
        self.context = StockContext("600001.SH", "测试股份", upper_limit=11.0, lower_limit=9.0)

    def test_ordinary_stock_requires_two_conditions(self):
        current = quote(close=10.55, amount=120_000_000, trades=5_000)
        previous = history(current.captured_at - timedelta(minutes=5), 10.45, 100_000_000, 4_500)
        events = self.detector.detect(current, self.context, [previous])
        self.assertEqual(events, [])

    def test_balanced_positive_enters_with_two_conditions(self):
        current = quote(close=10.7, amount=150_000_000, trades=5_000)
        previous = history(current.captured_at - timedelta(minutes=5), 10.4, 100_000_000, 4_500)
        prior = history(current.captured_at - timedelta(minutes=10), 10.3, 90_000_000, 4_300)
        events = self.detector.detect(current, self.context, [previous, prior])
        positive = [event for event in events if event.direction == Direction.POSITIVE]
        self.assertEqual(len(positive), 1)
        self.assertIn("strong_gain", positive[0].event_types)
        self.assertIn("rapid_rise", positive[0].event_types)

    def test_two_price_signals_without_liquidity_confirmation_are_rejected(self):
        current = quote(close=10.8, amount=115_000_000, trades=4_600)
        previous = history(current.captured_at - timedelta(minutes=5), 10.4, 100_000_000, 4_500)
        prior = history(current.captured_at - timedelta(minutes=10), 10.3, 90_000_000, 4_400)
        events = self.detector.detect(current, self.context, [previous, prior])
        self.assertFalse(any(event.direction == Direction.POSITIVE for event in events))

    def test_limit_up_is_hard_event(self):
        current = quote(close=11.0, high=11.0)
        events = self.detector.detect(current, self.context, [])
        positive = [event for event in events if event.direction == Direction.POSITIVE]
        self.assertTrue(positive[0].is_hard_event)
        self.assertIn("limit_up", positive[0].event_types)

    def test_failed_limit_is_negative_hard_event(self):
        current = quote(close=10.3, high=11.0)
        events = self.detector.detect(current, self.context, [])
        negative = [event for event in events if event.direction == Direction.NEGATIVE]
        self.assertTrue(negative[0].is_hard_event)
        self.assertIn("failed_limit", negative[0].event_types)

    def test_lunch_gap_is_not_mislabeled_as_five_minute_move(self):
        current = quote(captured_at=datetime(2026, 8, 26, 13, 0, tzinfo=CN), close=10.7)
        previous = history(datetime(2026, 8, 26, 11, 30, tzinfo=CN), 10.0, 100, 10)
        events = self.detector.detect(current, self.context, [previous])
        for event in events:
            self.assertEqual(event.change_5m, 0)
