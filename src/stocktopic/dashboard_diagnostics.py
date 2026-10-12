"""Low-overhead timing of the real dashboard request, without changing its data."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)
PATH = "/api/v1/dashboard"
SCOPE_KEY = "stocktopic.dashboard_timing"


@dataclass
class DashboardTiming:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started: float = field(default_factory=time.perf_counter)
    metrics: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    endpoint_done: float | None = None

    def elapsed(self, name: str, since: float) -> None:
        self.metrics[name] = max(0.0, (time.perf_counter() - since) * 1000)

    def phase(self, name: str, function: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            return function()
        finally:
            self.elapsed(name, started)

    async def health(self, function: Callable[[], Any]) -> Any:
        submitted = time.perf_counter()
        finished: float | None = None

        def worker() -> Any:
            nonlocal finished
            self.elapsed("health_queue", submitted)
            try:
                return self.phase("health_exec", function)
            finally:
                finished = time.perf_counter()

        try:
            return await asyncio.to_thread(worker)
        finally:
            self.elapsed("health_total", submitted)
            if finished is not None:
                self.elapsed("health_resume", finished)

    def server_timing(self) -> str:
        return ", ".join(f"{name};dur={value:.3f}" for name, value in self.metrics.items())


class DashboardTimingRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        if self.path != PATH:
            return original

        async def timed(request: Request):
            timing = request.scope.get(SCOPE_KEY)
            if timing is None:
                return await original(request)
            timing.elapsed("pre_handler", timing.started)
            response = await original(request)
            if timing.endpoint_done is not None:
                timing.elapsed("framework_response", timing.endpoint_done)
            response.headers["Server-Timing"] = timing.server_timing()
            return response

        return timed


class DashboardTimingMiddleware:
    """Measure through ASGI response completion, including failures and overlap.

    The clock begins at application entry. Socket/Uvicorn waiting before entry and
    client/network download time cannot be measured by this middleware.
    """

    def __init__(self, app: ASGIApp):
        self.app = app
        self.inflight: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != PATH:
            await self.app(scope, receive, send)
            return
        timing = DashboardTiming()
        scope[SCOPE_KEY] = timing
        self.inflight[timing.request_id] = len(self.inflight) + 1
        for request_id in self.inflight:
            self.inflight[request_id] = max(self.inflight[request_id], len(self.inflight))
        response_bytes = 0
        status = 500
        error_type = None

        async def measured_send(message: Message) -> None:
            nonlocal response_bytes, status
            if message["type"] == "http.response.start":
                status = message["status"]
                timing.elapsed("headers_ready", timing.started)
                message = dict(message)
                message["headers"] = [
                    *message.get("headers", []),
                    (
                        b"x-stocktopic-request-id",
                        timing.request_id.encode("ascii"),
                    ),
                ]
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            peak = self.inflight.pop(timing.request_id)
            timing.elapsed("app_total", timing.started)
            logger.info(
                "dashboard_timing %s",
                json.dumps(
                    {
                        "request_id": timing.request_id,
                        "status": status,
                        "error_type": error_type,
                        "response_bytes": response_bytes,
                        "active_peak": peak,
                        "thread_count": threading.active_count(),
                        "durations_ms": {
                            key: round(value, 3) for key, value in timing.metrics.items()
                        },
                        "counts": timing.counts,
                    },
                    separators=(",", ":"),
                ),
            )
