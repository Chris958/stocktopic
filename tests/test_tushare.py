import json
import urllib.error
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stocktopic.providers.tushare import TushareClient, TushareError, _decode_mcp_response


class FakeResponse:
    def __init__(self, payload=b"", content_type="application/json", session_id=""):
        self.payload = payload
        self.headers = {"Content-Type": content_type}
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def json_response(value, *, session_id=""):
    return FakeResponse(json.dumps(value).encode(), session_id=session_id)


def mcp_open_url(captured, tool_envelope):
    def open_url(request, timeout):
        payload = json.loads(request.data)
        captured.append((payload, timeout, dict(request.header_items())))
        if payload["method"] == "initialize":
            return json_response(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "test", "version": "1"},
                        "capabilities": {"tools": {}},
                    },
                },
                session_id="session-test",
            )
        if payload["method"] == "notifications/initialized":
            return FakeResponse()
        return json_response(
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(tool_envelope)}],
                    "isError": False,
                },
            }
        )

    return open_url


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

    def test_mcp_call_preserves_provider_signature_and_restores_ts_code(self):
        captured = []
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["symbol", "trade_date", "close"],
                "items": [["000001.SZ", "20260930", 11.57]],
            },
        }
        client = TushareClient(mcp_url="https://relay.example/mcp?token=secret")
        with patch(
            "stocktopic.providers.tushare.open_url",
            side_effect=mcp_open_url(captured, envelope),
        ):
            rows = client.call(
                "daily",
                {"ts_code": "000001.SZ", "trade_date": "20260930"},
                "ts_code,trade_date,close",
            )

        self.assertEqual(rows, [{"ts_code": "000001.SZ", "trade_date": "20260930", "close": 11.57}])
        tool_call = captured[-1][0]
        self.assertEqual(tool_call["method"], "tools/call")
        self.assertEqual(tool_call["params"]["name"], "daily")
        self.assertEqual(
            tool_call["params"]["arguments"],
            {
                "symbol": "000001.SZ",
                "trade_date": "20260930",
                "fields": ["ts_code", "trade_date", "close"],
            },
        )
        self.assertEqual(client.transport, "mcp")

    def test_mcp_stock_basic_avoids_alias_collision(self):
        captured = []
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["symbol", "name", "exchange", "market"],
                "items": [["000001.SZ", "平安银行", "SZSE", "主板"]],
            },
        }
        client = TushareClient(mcp_url="https://relay.example/mcp?token=secret")
        with patch(
            "stocktopic.providers.tushare.open_url",
            side_effect=mcp_open_url(captured, envelope),
        ):
            rows = client.stock_basic()

        self.assertEqual(rows[0]["ts_code"], "000001.SZ")
        self.assertEqual(rows[0]["symbol"], "000001")
        fields = captured[-1][0]["params"]["arguments"]["fields"]
        self.assertIn("ts_code", fields)
        self.assertNotIn("symbol", fields)

    def test_mcp_network_error_redacts_query_secret(self):
        url = "https://relay.example/mcp?token=top-secret"
        client = TushareClient(mcp_url=url)
        with patch(
            "stocktopic.providers.tushare.open_url",
            side_effect=urllib.error.URLError(f"failed to open {url}"),
        ):
            with self.assertRaises(TushareError) as raised:
                client.trade_calendar("20260930", "20260930")

        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "network")
        self.assertNotIn("top-secret", message)
        self.assertIn("<redacted>", message)

    def test_requires_a_direct_token_or_mcp_url(self):
        with self.assertRaisesRegex(ValueError, "MCP URL or token"):
            TushareClient()

    def test_decodes_streamable_http_event_response(self):
        payload = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()

        self.assertEqual(_decode_mcp_response(body, "text/event-stream"), payload)
