import tempfile
from pathlib import Path
from unittest import TestCase

from stocktopic.db import Database
from stocktopic.themes import ThemeDiscovery


class ThemeDatabaseTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "test.sqlite3", root / "archive")
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def create_candidate(self):
        return self.db.upsert_candidate(
            fingerprint="abc",
            provisional_name="农业异动",
            shared_tag="农业",
            direction="positive",
            discovered_at="2026-08-26T10:00:00+08:00",
            day1_date="2026-08-26",
            discovery_reason="4只股票共享农业标签",
            members=[
                {"code": f"60000{i}.SH", "name": f"股票{i}", "evidence": {"rule": "test"}}
                for i in range(4)
            ],
        )

    def test_pending_candidate_has_no_score(self):
        theme_id = self.create_candidate()
        theme = self.db.get_theme(theme_id)
        self.assertEqual(theme["status"], "pending")
        self.assertIsNone(theme["score"])

    def test_confirmation_preserves_day_one(self):
        theme_id = self.create_candidate()
        discovery = ThemeDiscovery(self.db)
        discovery.confirm(theme_id, "粮食安全")
        theme = self.db.get_theme(theme_id)
        self.assertEqual(theme["status"], "confirmed")
        self.assertEqual(theme["day1_date"], "2026-08-26")
        self.assertEqual(theme["final_name"], "粮食安全")

    def test_split_is_human_controlled(self):
        theme_id = self.create_candidate()
        discovery = ThemeDiscovery(self.db)
        new_id = discovery.split(theme_id, ["600000.SH"], "种业")
        source = self.db.get_theme(theme_id)
        child = self.db.get_theme(new_id)
        self.assertEqual(sum(member["active"] for member in source["members"]), 3)
        self.assertEqual(child["members"][0]["membership_source"], "human_split")

    def test_universe_strictly_excludes_non_a_share_main_boards(self):
        count = self.db.upsert_stocks(
            [
                {"ts_code": "600000.SH", "name": "浦发银行", "market": "主板"},
                {"ts_code": "002001.SZ", "name": "新和成", "market": "主板"},
                {"ts_code": "688001.SH", "name": "科创公司", "market": "科创板"},
                {"ts_code": "200001.SZ", "name": "深发展B", "market": "主板"},
                {"ts_code": "900901.SH", "name": "云赛B股", "market": "主板"},
                {"ts_code": "600001.SH", "name": "*ST测试", "market": "主板"},
            ]
        )
        self.assertEqual(count, 2)
        self.assertEqual(set(self.db.active_stock_map()), {"600000.SH", "002001.SZ"})

    def test_kpl_themes_become_deterministic_stock_tags(self):
        self.db.upsert_stocks(
            [{"ts_code": "600000.SH", "name": "浦发银行", "market": "主板"}]
        )
        self.db.upsert_kpl_events(
            [
                {
                    "trade_date": "20260826",
                    "ts_code": "600000.SH",
                    "name": "浦发银行",
                    "tag": "涨停",
                    "theme": "金融科技、并购重组",
                    "status": "2连板",
                }
            ]
        )
        tags = self.db.tags_for_codes(["600000.SH"])["600000.SH"]
        self.assertEqual({item["tag"] for item in tags}, {"金融科技", "并购重组"})

