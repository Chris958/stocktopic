import base64
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings


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

        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
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
        assert dashboard.json()["data_context"] == {
            "anomaly_trade_date": None,
            "has_intraday_data": False,
        }

        basic_value = base64.b64encode(b"admin:password").decode()
        basic = {"Authorization": f"Basic {basic_value}"}
        assert client.post("/api/v1/admin/run-once", headers=basic).status_code == 403
        basic["X-StockTopic-Request"] = "1"
        response = client.post("/api/v1/admin/run-once", headers=basic)
        assert response.status_code == 200
        assert response.json()["status"] == "idle"
