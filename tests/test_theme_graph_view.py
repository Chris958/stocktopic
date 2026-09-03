import tempfile
from pathlib import Path

from stocktopic.db import Database
from stocktopic.theme_graph_view import build_theme_graph


def _database():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    db = Database(root / "test.sqlite3", root / "archive")
    db.initialize()
    return temp, db


def test_theme_graph_merges_same_theme_across_sources_and_keeps_reasons():
    temp, db = _database()
    try:
        db.upsert_stocks(
            [
                {"ts_code": "600001.SH", "name": "甲公司", "market": "主板"},
                {"ts_code": "600002.SH", "name": "乙公司", "market": "主板"},
            ]
        )
        db.upsert_kpl_concept_members(
            [
                {
                    "ts_code": "900001.KP",
                    "name": "算力租赁概念",
                    "con_code": "600001.SH",
                    "con_name": "甲公司",
                    "trade_date": "20260902",
                    "desc": "开盘啦归类",
                    "hot_num": 5,
                },
                {
                    "ts_code": "DC:BK001",
                    "name": "算力租赁",
                    "con_code": "600001.SH",
                    "con_name": "甲公司",
                    "trade_date": "20260902",
                    "desc": "公司提供智算中心租赁服务",
                    "hot_num": 2,
                },
                {
                    "ts_code": "DC:BK001",
                    "name": "算力租赁",
                    "con_code": "600002.SH",
                    "con_name": "乙公司",
                    "trade_date": "20260902",
                    "desc": "运营算力基础设施",
                    "hot_num": 2,
                },
            ]
        )
        result = build_theme_graph(db, "20260902")
        assert result["stats"]["nodes"] == 1
        node = result["items"][0]
        assert node["member_count"] == 2
        assert node["cross_source"] is True
        assert {item["id"] for item in node["sources"]} == {"kpl", "dc"}
        member = next(item for item in node["members"] if item["code"] == "600001.SH")
        assert member["source_count"] == 2
        assert any("智算中心" in item["text"] for item in member["reasons"])
    finally:
        temp.cleanup()


def test_theme_graph_supports_source_search_and_min_member_filters():
    temp, db = _database()
    try:
        db.upsert_stocks(
            [
                {"ts_code": "600010.SH", "name": "机器人甲", "market": "主板"},
                {"ts_code": "600011.SH", "name": "机器人乙", "market": "主板"},
            ]
        )
        db.upsert_kpl_concept_members(
            [
                {
                    "ts_code": "TDX:880001.TDX",
                    "name": "人形机器人",
                    "con_code": "600010.SH",
                    "con_name": "机器人甲",
                    "trade_date": "20260902",
                    "desc": "通达信概念板块结构化成分",
                },
                {
                    "ts_code": "TDX:880001.TDX",
                    "name": "人形机器人",
                    "con_code": "600011.SH",
                    "con_name": "机器人乙",
                    "trade_date": "20260902",
                    "desc": "通达信概念板块结构化成分",
                },
            ]
        )
        assert build_theme_graph(db, source="dc")["items"] == []
        result = build_theme_graph(db, source="tdx", query="机器人甲", min_members=2)
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "人形机器人"
        assert build_theme_graph(db, source="tdx", min_members=3)["items"] == []
    finally:
        temp.cleanup()
