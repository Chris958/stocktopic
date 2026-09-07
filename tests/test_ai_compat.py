from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from stocktopic import ai as ai_module
from stocktopic.ai import OpenAIThemeExplainer
from stocktopic.ai_compat import install_ai_relay_compat


def test_http_500_wrapped_unsupported_max_tool_calls_downgrades_to_bounded():
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model")
    payloads = []

    def respond(payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise RuntimeError(
                'OpenAI HTTP 500: {"error":{"message":"Unsupported parameter: '
                'max_tool_calls","type":"invalid_request_error"}}'
            )
        return {"output": []}

    client._request_payload = respond
    client._call_prompt(
        "prompt",
        reasoning_effort="medium",
        task_type="semantic_event_clustering",
        max_output_tokens=7000,
        max_tool_calls=5,
    )

    assert len(payloads) == 2
    assert payloads[0]["max_tool_calls"] == 5
    assert "max_tool_calls" not in payloads[1]
    assert "prompt_cache_key" not in payloads[1]
    assert payloads[1]["max_output_tokens"] == 7000
    assert client._request_controls_mode == "bounded"


def test_unsupported_parameter_wrapped_as_500_is_not_retried_at_http_layer(monkeypatch):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")
    calls = 0
    body = json.dumps(
        {
            "error": {
                "message": "Unsupported parameter: max_tool_calls",
                "type": "invalid_request_error",
            }
        }
    ).encode()

    def reject(_request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://relay.example/v1/responses",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(ai_module, "open_url", reject)
    request = urllib.request.Request(client.endpoint, data=b"{}", method="POST")

    with pytest.raises(RuntimeError, match="Unsupported parameter: max_tool_calls"):
        client._request_json_with_retry(request, attempts=3)

    assert calls == 1


def test_normal_http_500_still_retries(monkeypatch):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"output": []}'

    def flaky(_request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                "https://relay.example/v1/responses",
                500,
                "Internal Server Error",
                {},
                io.BytesIO(b'{"error":{"message":"temporary upstream failure"}}'),
            )
        return Response()

    monkeypatch.setattr(ai_module, "open_url", flaky)
    monkeypatch.setattr("stocktopic.ai_compat.time.sleep", lambda _seconds: None)
    request = urllib.request.Request(client.endpoint, data=b"{}", method="POST")

    assert client._request_json_with_retry(request, attempts=3) == {"output": []}
    assert calls == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_persistent_upstream_5xx_uses_all_retry_attempts(monkeypatch, status):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")
    calls = 0
    sleeps = []

    def reject(_request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://relay.example/v1/responses",
            status,
            "upstream failure",
            {},
            io.BytesIO(b'{"error":{"message":"temporary upstream failure"}}'),
        )

    monkeypatch.setattr(ai_module, "open_url", reject)
    monkeypatch.setattr("stocktopic.ai_compat.time.sleep", sleeps.append)
    request = urllib.request.Request(client.endpoint, data=b"{}", method="POST")

    with pytest.raises(RuntimeError, match=f"OpenAI HTTP {status}"):
        client._request_json_with_retry(request, attempts=3)

    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_stream_read_timeout_retries_once(monkeypatch):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")
    calls = 0

    def timeout(_request, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(ai_module, "open_url", timeout)
    request = urllib.request.Request(
        client.endpoint, data=b'{"stream":true}', method="POST"
    )

    with pytest.raises(RuntimeError, match="read timed out after 2/2 attempts"):
        client._request_json_with_retry(request, attempts=3)

    assert calls == 2


def test_non_stream_read_timeout_is_not_retried_and_double_billed(monkeypatch):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")
    calls = 0

    def timeout(_request, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(ai_module, "open_url", timeout)
    request = urllib.request.Request(client.endpoint, data=b"{}", method="POST")

    with pytest.raises(RuntimeError, match="read timed out after 1/1 attempt"):
        client._request_json_with_retry(request, attempts=3)

    assert calls == 1


def test_responses_sse_stream_returns_completed_response(monkeypatch):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")

    class Headers:
        def get(self, name, default=""):
            return "text/event-stream" if name == "Content-Type" else default

    class Response:
        headers = Headers()

        def __init__(self):
            self.lines = iter(
                [
                    b'event: response.created\n',
                    b'data: {"type":"response.created","response":{"status":"in_progress"}}\n',
                    b'\n',
                    b'event: response.completed\n',
                    (
                        b'data: {"type":"response.completed","response":'
                        b'{"status":"completed","output":[],"usage":'
                        b'{"total_tokens":42}}}\n'
                    ),
                    b'\n',
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(self.lines, b"")

    monkeypatch.setattr(ai_module, "open_url", lambda _request, timeout: Response())
    request = urllib.request.Request(
        client.endpoint, data=b'{"stream":true}', method="POST"
    )

    result = client._request_json_with_retry(request)

    assert result["status"] == "completed"
    assert result["usage"]["total_tokens"] == 42


def test_responses_sse_upstream_failure_retries(monkeypatch):
    install_ai_relay_compat()
    client = OpenAIThemeExplainer("key", "model", base_url="https://relay.example/v1")
    calls = 0

    class Headers:
        def get(self, name, default=""):
            return "text/event-stream" if name == "Content-Type" else default

    class Response:
        headers = Headers()

        def __init__(self, failed):
            event = (
                b'data: {"type":"response.failed","response":{"error":'
                b'{"message":"upstream error: do request failed"}}}\n'
                if failed
                else (
                    b'data: {"type":"response.completed","response":'
                    b'{"status":"completed","output":[]}}\n'
                )
            )
            self.lines = iter([event, b"\n"])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(self.lines, b"")

    def respond(_request, timeout):
        nonlocal calls
        calls += 1
        return Response(failed=calls == 1)

    monkeypatch.setattr(ai_module, "open_url", respond)
    monkeypatch.setattr("stocktopic.ai_compat.time.sleep", lambda _seconds: None)
    request = urllib.request.Request(
        client.endpoint, data=b'{"stream":true}', method="POST"
    )

    result = client._request_json_with_retry(request, attempts=3)

    assert result["status"] == "completed"
    assert calls == 2
