import tempfile
from pathlib import Path

from stocktopic.db import Database


def test_interrupted_ai_analysis_is_recovered_for_immediate_retry():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        database = Database(root / "test.sqlite3", root / "archive")
        database.initialize()
        theme_id = database.upsert_candidate(
            fingerprint="interrupted-ai",
            provisional_name="中断题材待审",
            shared_tag="中断题材",
            direction="positive",
            discovered_at="2026-09-03T09:30:00+08:00",
            day1_date="2026-09-03",
            discovery_reason="测试中断恢复",
            members=[],
        )
        database.set_admission_status(theme_id, "analyzing", "正在分析")

        recovered = database.recover_interrupted_ai_analyses()
        theme = database.get_theme(theme_id)

        assert recovered == 1
        assert theme["admission_status"] == "awaiting_ai"
        assert theme["admission_reviewed_at"] is None
        assert "服务重启" in theme["admission_reason"]
