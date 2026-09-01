import base64
import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings


class FailingWeComNotifier:
    enabled = True

    def send_text(self, title, body):
        raise RuntimeError(
            "request failed: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
            "key=sensitive-bot-key&errcode=93000 invalid webhook url"
        )


def test_api_auth_and_csrf_guard():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        settings = Settings(
            tushare_token="test",
            db_path=root / "test.sqlite3",
            archive_dir=root / "archive",
            admin_username="admin",
            admin_password="password",
            app_api_token="app-token",
        )
        app = create_app(settings)
        app.state.service.database.initialize()
        client = TestClient(app, base_url="https://testserver")

        page = client.get("/")
        assert page.status_code == 200
        assert "app.js?v=0.11.1" in page.text
        assert 'data-view="anomalies"' not in page.text
        assert page.headers["cache-control"] == "no-store, must-revalidate"
        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert script.headers["cache-control"] == "no-store, must-revalidate"
        assert client.get("/static/manifest.webmanifest").status_code == 200
        icon = client.get("/static/app-icon-180.png")
        assert icon.status_code == 200
        assert icon.headers["content-type"] == "image/png"
        assert client.get("/api/v1/dashboard").status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password"},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert "stocktopic_session=" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=strict" in cookie
        assert "Max-Age=2592000" in cookie
        assert "password" not in cookie
        session_dashboard = client.get("/api/v1/dashboard")
        assert session_dashboard.status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 403
        assert client.post(
            "/api/v1/auth/logout", headers={"X-StockTopic-Request": "1"}
        ).status_code == 200
        assert client.get("/api/v1/dashboard").status_code == 401
        bearer = {"Authorization": "Bearer app-token"}
        dashboard = client.get("/api/v1/dashboard", headers=bearer)
        assert dashboard.status_code == 200
        assert dashboard.headers["cache-control"] == "no-store"
        assert dashboard.headers["x-frame-options"] == "DENY"
        assert "anomalies" not in dashboard.json()
        assert "backtest" in dashboard.json()

        basic_value = base64.b64encode(b"admin:password").decode()
        basic = {"Authorization": f"Basic {basic_value}"}
        assert client.post("/api/v1/admin/run-once", headers=basic).status_code == 403
        basic["X-StockTopic-Request"] = "1"
        level2 = client.post(
            "/api/v1/level2/analyze",
            json={"code": "603269.SH"},
            headers=basic,
        )
        assert level2.status_code == 503
        assert "猫爪数据尚未配置" in level2.json()["detail"]
        response = client.post("/api/v1/admin/run-once", headers=basic)
        assert response.status_code == 200
        assert response.json()["status"] == "idle"

        theme_id = app.state.service.database.upsert_candidate(
            fingerprint="api-theme",
            provisional_name="接口题材",
            shared_tag="接口测试",
            direction="positive",
            discovered_at="2026-08-26T10:00:00+08:00",
            day1_date="2026-08-26",
            discovery_reason="测试置顶和归档",
            members=[],
        )
        app.state.service.database.set_theme_status(theme_id, "confirmed", "接口题材")
        response = client.post(
            f"/api/v1/themes/{theme_id}/pin", json={"pinned": True}, headers=basic
        )
        assert response.status_code == 200
        assert response.json()["theme"]["pinned"] == 1
        assert client.post(f"/api/v1/themes/{theme_id}/archive", headers=basic).status_code == 200
        assert app.state.service.database.get_theme(theme_id)["status"] == "archived"
        assert client.post(f"/api/v1/themes/{theme_id}/restore", headers=basic).status_code == 200
        assert app.state.service.database.get_theme(theme_id)["status"] == "confirmed"

        now = app.state.service.clock.china_now()
        calendar_rows = []
        for offset in range(3):
            day = now.date() + timedelta(days=offset)
            previous = day - timedelta(days=1)
            calendar_rows.append(
                {
                    "cal_date": day.strftime("%Y%m%d"),
                    "is_open": "1",
                    "pretrade_date": previous.strftime("%Y%m%d"),
                }
            )
        app.state.service.database.replace_calendar(calendar_rows)
        tracked_theme_id = app.state.service.database.upsert_candidate(
            fingerprint="api-tracked-theme",
            provisional_name="回测接口题材",
            shared_tag="回测接口",
            direction="positive",
            discovered_at=now.isoformat(),
            day1_date=now.date().isoformat(),
            discovery_reason="测试股票跟踪接口",
            members=[{"code": "600000.SH", "name": "浦发银行", "evidence": {}}],
        )
        response = client.post(
            "/api/v1/test-pool",
            json={"theme_id": tracked_theme_id, "code": "600000.SH"},
            headers=basic,
        )
        assert response.status_code == 200
        assert response.json()["created"] is True
        duplicate = client.post(
            "/api/v1/test-pool",
            json={"theme_id": tracked_theme_id, "code": "600000.SH"},
            headers=basic,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False

        app.state.service.notifier = FailingWeComNotifier()
        response = client.post("/api/v1/admin/wecom-test", headers=basic)
        assert response.status_code == 502
        assert "errcode=93000" in response.json()["detail"]
        assert "sensitive-bot-key" not in response.text
