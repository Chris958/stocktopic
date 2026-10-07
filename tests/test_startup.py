import tempfile
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings


def test_health_is_available_while_reference_data_loads():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        settings = Settings(
            tushare_token="test",
            db_path=root / "test.sqlite3",
            archive_dir=root / "archive",
        )
        app = create_app(settings)
        # The detailed health response must not scan the entire SQLite database.
        app.state.service.database.integrity_check = lambda: (_ for _ in ()).throw(
            AssertionError("health must use the lightweight database ping")
        )
        reference_started = threading.Event()
        reference_release = threading.Event()

        def slow_reference_data() -> None:
            reference_started.set()
            reference_release.wait(timeout=5)

        app.state.service.initialize_reference_data = slow_reference_data
        try:
            with TestClient(app) as client:
                assert reference_started.wait(timeout=1)
                response = client.get("/health")
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
                assert response.json()["database"] == "reachable"
                reference_release.set()
        finally:
            reference_release.set()


def test_health_reports_mcp_transport_without_exposing_url():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        mcp_url = "https://relay.example/mcp?token=test-placeholder"
        settings = Settings(
            tushare_token="",
            db_path=root / "test.sqlite3",
            archive_dir=root / "archive",
            tushare_mcp_url=mcp_url,
        )
        app = create_app(settings)
        app.state.service.initialize_storage()

        health = app.state.service.health()

        assert health["integrations"]["tushare"] is True
        assert health["integrations"]["tushare_transport"] == "mcp"
        assert mcp_url not in str(health)


def test_health_reports_disabled_rt_k_with_direct_transport():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        settings = Settings(
            tushare_token="test",
            db_path=root / "test.sqlite3",
            archive_dir=root / "archive",
            tushare_rt_k_enabled=False,
        )
        app = create_app(settings)
        app.state.service.initialize_storage()

        health = app.state.service.health()

        assert health["integrations"]["tushare_transport"] == "direct"
        assert health["integrations"]["tushare_rt_k_enabled"] is False
        assert health["market"]["realtime_collection_enabled"] is False
        assert health["market"]["reason"] == "rt_k_disabled_by_configuration"
