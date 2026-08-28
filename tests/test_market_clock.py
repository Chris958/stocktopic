from datetime import datetime
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.market_clock import MarketClock

CN = ZoneInfo("Asia/Shanghai")


class MarketClockTests(TestCase):
    def test_valid_collection_windows(self):
        for hour, minute, expected in [
            (9, 15, "opening_auction"),
            (9, 25, "opening_auction"),
            (9, 30, "morning"),
            (11, 30, "morning"),
            (13, 0, "afternoon"),
            (15, 0, "afternoon"),
        ]:
            value = datetime(2026, 8, 26, hour, minute, tzinfo=CN)
            state = MarketClock.state(value, True)
            self.assertTrue(state.in_realtime_window)
            self.assertEqual(state.session, expected)

    def test_non_trading_windows_are_idle(self):
        for hour, minute in [(9, 10), (9, 27), (12, 0), (15, 1), (23, 0)]:
            state = MarketClock.state(datetime(2026, 8, 26, hour, minute, tzinfo=CN), True)
            self.assertFalse(state.in_realtime_window)

    def test_unknown_calendar_fails_closed_even_during_session(self):
        state = MarketClock.state(datetime(2026, 8, 26, 10, 0, tzinfo=CN), None)
        self.assertFalse(state.in_realtime_window)
        self.assertEqual(state.reason, "calendar_unknown_fail_closed")

    def test_closed_day_fails_closed(self):
        state = MarketClock.state(datetime(2026, 8, 29, 10, 0, tzinfo=CN), False)
        self.assertFalse(state.in_realtime_window)
        self.assertEqual(state.reason, "exchange_closed")
