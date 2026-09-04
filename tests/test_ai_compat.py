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
