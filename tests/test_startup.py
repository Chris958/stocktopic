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
                reference_release.set()
        finally:
            reference_release.set()
