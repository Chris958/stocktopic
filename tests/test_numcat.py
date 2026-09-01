import json
from unittest import TestCase
from unittest.mock import patch

from stocktopic.providers.numcat import NumcatClient, NumcatError


class JsonResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class NumcatClientTests(TestCase):
    def test_trade_history_uses_cursor_pagination_and_matrix_fields(self):
        first = {
            "code": 200,
            "message": "success",
            "data": {
                "fields": ["symbol", "time", "amount", "bs_flag", "buy_order_id"],
                "items": [["603269", "09:30:00.001", 600000, "B", 1001]],
                "has_more": True,
                "next_cursor": "next-page",
            },
        }
        second = {
            "code": 200,
            "message": "success",
            "data": {
                "fields": ["symbol", "time", "amount", "bs_flag", "buy_order_id"],
                "items": [["603269", "09:30:00.002", 400000, "B", 1001]],
                "has_more": False,
                "next_cursor": None,
            },
        }
        client = NumcatClient("secret-key")
        with patch(
            "stocktopic.providers.numcat.open_url",
            side_effect=[JsonResponse(first), JsonResponse(second)],
        ) as opener:
            rows = client.trade_history("603269", "20260901")
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["amount"] for row in rows), 1_000_000)
        second_request = json.loads(opener.call_args_list[1].args[0].data.decode())
        self.assertEqual(second_request["params"]["cursor"], "next-page")
        self.assertEqual(second_request["params"]["page_size"], 50000)
        self.assertEqual(second_request["apiname"], "level2_trade_history")

    def test_missing_key_fails_before_network_request(self):
        client = NumcatClient("")
        with self.assertRaises(NumcatError) as captured:
            client.trade_history("603269", "20260901")
        self.assertIn("NUMCAT_API_KEY", str(captured.exception))

    def test_api_error_never_echoes_api_key(self):
        client = NumcatClient("secret-key")
        response = JsonResponse({"code": 401, "message": "invalid api key"})
        with patch("stocktopic.providers.numcat.open_url", return_value=response):
            with self.assertRaises(NumcatError) as captured:
                client.trade_history("603269", "20260901")
        self.assertNotIn("secret-key", str(captured.exception))
