from datetime import datetime
from unittest import TestCase
from zoneinfo import ZoneInfo

from stocktopic.providers.tushare import TushareClient


class TushareClientTests(TestCase):
    def test_realtime_query_includes_main_board_and_chinext(self):
        client = TushareClient("test")
        captured = {}

        def call(api_name, params, fields):
            captured.update(api_name=api_name, params=params, fields=fields)
            return []

        client.call = call
        client.realtime_quotes(
            datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.assertEqual(captured["api_name"], "rt_k")
        self.assertEqual(captured["params"]["ts_code"], "6*.SH,0*.SZ,3*.SZ")
