from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

_OPTIONAL_REQUEST_CONTROLS = (
    "max_tool_calls",
    "prompt_cache_key",
    "max_output_tokens",
)


def install_ai_relay_compat() -> None:
    """Make Responses API request controls degrade cleanly through API relays.

    Some OpenAI-compatible relays wrap an upstream invalid_request_error as HTTP 500.
    StockTopic should treat a body that explicitly says an optional request control is
    unsupported as a capability-negotiation signal, not as a transient server outage.
    """
    from . import ai as ai_module

    cls = ai_module.OpenAIThemeExplainer
    if getattr(cls, "_relay_compat_installed", False):
        return

    def request_with_compatible_controls(
        self: Any,
        base_payload: dict[str, Any],
        *,
        task_type: str,
        max_output_tokens: int,
        max_tool_calls: int,
    ) -> tuple[dict[str, Any], str]:
        modes = ("full", "bounded", "legacy")
        cached_mode = self._request_controls_mode
        start_index = modes.index(cached_mode) if cached_mode in modes else 0

        # Keep the fast path lock-free once a relay capability has been learned.
        if cached_mode in modes:
            payload = self._payload_for_mode(
                base_payload,
                cached_mode,
                task_type,
                max_output_tokens,
                max_tool_calls,
            )
            try:
                return self._request_payload(payload), cached_mode
            except RuntimeError as error:
                if not is_unsupported_optional_control_error(error) or cached_mode == "legacy":
                    raise
                logger.warning(
                    "AI relay stopped accepting %s request controls; downgrading compatibility mode",
                    cached_mode,
                )
                start_index = modes.index(cached_mode) + 1

        with self._request_controls_lock:
            # Another task may already have negotiated a lower mode while this task waited.
            learned_mode = self._request_controls_mode
            if learned_mode in modes and modes.index(learned_mode) >= start_index:
                start_index = modes.index(learned_mode)

            for candidate_mode in modes[start_index:]:
                payload = self._payload_for_mode(
                    base_payload,
                    candidate_mode,
                    task_type,
                    max_output_tokens,
                    max_tool_calls,
                )
                try:
                    raw = self._request_payload(payload)
                    self._request_controls_mode = candidate_mode
                    return raw, candidate_mode
                except RuntimeError as error:
                    if (
                        not is_unsupported_optional_control_error(error)
                        or candidate_mode == "legacy"
                    ):
                        raise
                    logger.warning(
                        "AI upstream rejected %s request controls; trying compatibility mode",
                        candidate_mode,
                    )
                    self._request_controls_mode = None

        raise RuntimeError("AI request control negotiation exhausted")

    def request_json_with_retry(
        self: Any, request: Any, attempts: int = 3
    ) -> dict[str, Any]:
        """Retry transient failures, but never retry a deterministic unsupported parameter."""
        host = urllib.parse.urlsplit(self.endpoint).hostname or "unknown"
        last_error: Exception | None = None
        last_message = "unknown upstream error"
        for attempt in range(max(1, attempts)):
            try:
                with ai_module.open_url(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                last_error = error
                body = error.read().decode("utf-8", errors="replace")
                last_message = f"OpenAI HTTP {error.code}: {body[:500]}"
                deterministic_compat_error = is_unsupported_optional_control_error(
                    last_message
                )
                retryable = (
                    (error.code == 429 or error.code >= 500)
                    and not deterministic_compat_error
                )
                if not retryable or attempt >= attempts - 1:
                    raise RuntimeError(last_message) from error
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                last_message = (
                    f"AI upstream network failed after {attempt + 1}/{attempts} attempts "
                    f"(host={host}): {error}"
                )
                if attempt >= attempts - 1:
                    raise RuntimeError(last_message) from error
            except json.JSONDecodeError as error:
                last_error = error
                last_message = f"AI upstream returned non-JSON response (host={host})"
                if attempt >= attempts - 1:
                    raise RuntimeError(last_message) from error
            time.sleep(1.0 * (2**attempt))
        raise RuntimeError(last_message) from last_error

    cls._request_with_compatible_controls = request_with_compatible_controls
    cls._request_json_with_retry = request_json_with_retry
    cls._relay_compat_installed = True


def is_unsupported_optional_control_error(error: Any) -> bool:
    text = str(error or "").casefold()
    if not any(control in text for control in _OPTIONAL_REQUEST_CONTROLS):
        return False
    markers = (
        "unsupported parameter",
        "unsupported_parameter",
        "not supported",
        "unknown parameter",
        "unrecognized request argument",
        "invalid_request_error",
    )
    return any(marker in text for marker in markers)
