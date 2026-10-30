import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.dashboard_diagnostics import DashboardTiming


def app_for(tmp_path):
    app = create_app(
        Settings(
            tushare_token="test",
            db_path=tmp_path / "test.sqlite3",
            archive_dir=tmp_path / "archive",
            admin_password="password-secret",
            app_api_token="token-secret",
        )
    )
    app.state.service.database.initialize()
    return app


def logged(caplog):
    return [
        json.loads(record.getMessage().split("dashboard_timing ", 1)[1])
        for record in caplog.records
        if "dashboard_timing " in record.getMessage()
    ]


def test_real_dashboard_logs_stages_without_changing_payload(tmp_path, caplog):
    app = app_for(tmp_path)
    caplog.set_level(logging.INFO, logger="stocktopic.dashboard_diagnostics")
    client = TestClient(app, base_url="https://testserver")
    response = client.get("/api/v1/dashboard", headers={"Authorization": "Bearer token-secret"})
    assert response.status_code == 200
    assert set(response.json()) == {"health", "themes", "alerts", "backtest", "market_cycles"}
    assert response.json()["market_cycles"] == []
    assert response.headers["cache-control"] == "no-store"
    log = logged(caplog)[0]
    assert log["request_id"] == response.headers["x-stocktopic-request-id"]
    assert log["response_bytes"] == len(response.content)
    assert log["status"] == 200
    assert log["counts"] == {"themes": 0, "members": 0}
    stages = log["durations_ms"]
    assert set(stages) >= {
        "pre_handler",
        "flow_context",
        "list_themes",
        "attach_fund_flows",
        "health_exec",
        "dashboard_queue",
        "dashboard_exec",
        "dashboard_resume",
        "dashboard_total",
        "jsonable_encoder",
        "json_response",
        "alerts",
        "backtest",
        "market_cycles",
        "endpoint_total",
        "framework_response",
        "headers_ready",
        "app_total",
    }
    assert all(value >= 0 for value in stages.values())
    assert "list_themes;dur=" in response.headers["server-timing"]
    assert "token-secret" not in caplog.text and "password-secret" not in caplog.text


def test_unauthorized_dashboard_never_executes_queries_and_other_paths_not_logged(tmp_path, caplog):
    app = app_for(tmp_path)
    app.state.service.database.list_themes = lambda: pytest.fail("Unauthorized query")
    caplog.set_level(logging.INFO, logger="stocktopic.dashboard_diagnostics")
    client = TestClient(app)
    assert client.get("/api/v1/dashboard").status_code == 401
    assert client.get("/health").status_code == 200
    logs = logged(caplog)
    assert len(logs) == 1 and logs[0]["status"] == 401
    assert "list_themes" not in logs[0]["durations_ms"]


def test_failing_phase_is_logged_without_error_message_or_credentials(tmp_path, caplog):
    app = app_for(tmp_path)

    def fail():
        raise RuntimeError("sensitive-error-token")

    app.state.service.database.list_themes = fail
    caplog.set_level(logging.INFO, logger="stocktopic.dashboard_diagnostics")
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/dashboard",
        headers={"Authorization": "Bearer token-secret"},
    )
    assert response.status_code == 500
    log = logged(caplog)[0]
    assert log["error_type"] == "RuntimeError"
    assert "list_themes" in log["durations_ms"]
    assert "sensitive-error-token" not in caplog.text


def test_health_queue_execution_and_resume_are_measured_separately(monkeypatch):
    import stocktopic.dashboard_diagnostics as diagnostics

    clock = [0.0]
    monkeypatch.setattr(diagnostics.time, "perf_counter", lambda: clock[0])

    async def delayed(function):
        clock[0] += 2  # Submitted but no worker available yet.
        result = function()
        clock[0] += 3  # Worker completed; event loop has not resumed yet.
        return result

    monkeypatch.setattr(diagnostics.asyncio, "to_thread", delayed)

    def health():
        clock[0] += 1
        return {"status": "ok"}

    timing = DashboardTiming()
    assert asyncio.run(timing.health(health)) == {"status": "ok"}
    assert timing.metrics == {
        "health_queue": 2000,
        "health_exec": 1000,
        "health_total": 6000,
        "health_resume": 3000,
    }


def test_health_failure_still_reports_execution_and_total():
    def fail():
        raise ValueError("synthetic")

    timing = DashboardTiming()
    with pytest.raises(ValueError):
        asyncio.run(timing.health(fail))
    assert set(timing.metrics) == {"health_queue", "health_exec", "health_total", "health_resume"}


def test_overlapping_requests_have_independent_ids_and_peak_counts(caplog):
    from stocktopic.dashboard_diagnostics import DashboardTimingMiddleware

    caplog.set_level(logging.INFO, logger="stocktopic.dashboard_diagnostics")

    async def run():
        entered = 0
        both_entered = asyncio.Event()

        async def app(scope, receive, send):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def send(message):
            pass

        async def receive():
            return {"type": "http.request", "body": b""}

        middleware = DashboardTimingMiddleware(app)
        await asyncio.gather(
            *[
                middleware({"type": "http", "path": "/api/v1/dashboard"}, receive, send)
                for _ in range(2)
            ]
        )
        assert middleware.inflight == {}

    asyncio.run(run())
    logs = logged(caplog)
    assert len(logs) == 2
    assert len({record["request_id"] for record in logs}) == 2
    assert all(record["active_peak"] == 2 for record in logs)
