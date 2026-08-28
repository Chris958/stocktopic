import base64
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.wecom import WeComDeliveryError


class FailingWeComNotifier:
    enabled = True

    def send_text(self, title, body):
        raise RuntimeError(
            "request failed: access_token=sensitive-token&errcode=60020 not allowed from ip"
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
        client = TestClient(app)

        page = client.get("/")
        assert page.status_code == 200
        assert "app.js?v=0.4.0" in page.text
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
        bearer = {"Authorization": "Bearer app-token"}
        dashboard = client.get("/api/v1/dashboard", headers=bearer)
        assert dashboard.status_code == 200
        assert dashboard.headers["cache-control"] == "no-store"
        assert dashboard.headers["x-frame-options"] == "DENY"
        assert "anomalies" not in dashboard.json()

        basic_value = base64.b64encode(b"admin:password").decode()
        basic = {"Authorization": f"Basic {basic_value}"}
        assert client.post("/api/v1/admin/run-once", headers=basic).status_code == 403
        basic["X-StockTopic-Request"] = "1"
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

        app.state.service.notifier = FailingWeComNotifier()
        response = client.post("/api/v1/admin/wecom-test", headers=basic)
        assert response.status_code == 502
        assert "errcode=60020" in response.json()["detail"]
        assert "sensitive-token" not in response.text


def test_wecom_trusted_ip_error_has_actionable_guidance():
    error = WeComDeliveryError("获取Token", 60020, "not allow to access from your ip")
    assert "errcode=60020" in str(error)
    assert "企业可信IP" in str(error)
