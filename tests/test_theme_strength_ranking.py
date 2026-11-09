"""The formal list ranks daily market evidence, without full detail hydration."""
import json
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.db import Database
from stocktopic.market_clock import SHANGHAI

DAY = "20261106"


def database(tmp_path):
    db = Database(tmp_path / "rank.db", tmp_path / "archive")
    db.initialize()
    db.upsert_stocks([{"ts_code": f"600{i:03d}.SH", "name": f"股票{i}", "market": "主板"}
                      for i in range(15)])
    return db


def pool(db, name, indices):
    identifier = db.upsert_candidate(
        fingerprint=name, provisional_name=name, shared_tag=name, direction="positive",
        discovered_at="2026-11-06T10:00:00+08:00", day1_date="2026-11-06",
        discovery_reason="fixture",
        members=[{"code": f"600{i:03d}.SH", "name": f"股票{i}", "evidence": {}}
                 for i in indices],
    )
    db.set_theme_status(identifier, "confirmed")
    return identifier


def event(db, i, status="首板", day=DAY, tag="涨停"):
    db.upsert_kpl_events([{"trade_date": day, "ts_code": f"600{i:03d}.SH",
                          "name": f"股票{i}", "tag": tag, "status": status}])


def test_daily_breadth_height_and_nonconsecutive_status(tmp_path):
    db = database(tmp_path)
    wide = pool(db, "广度", range(6))
    high = pool(db, "高度", range(6, 10))
    old = pool(db, "旧高标", [10, 11])
    for i in range(10):
        event(db, i, "5板" if i == 6 else "3板" if i == 0 else "首板")
    event(db, 10, "9板", day="20261105")
    event(db, 11, "5天3板")
    rank = db.theme_strength_ranking(DAY)
    assert (rank[wide]["limit_up_count"], rank[wide]["max_board_height"]) == (6, 3)
    assert (rank[high]["limit_up_count"], rank[high]["max_board_height"]) == (4, 5)
    assert rank[high]["leaders"] == [{"code": "600006.SH", "name": "股票6"}]
    assert rank[old]["limit_up_count"] == 1
    assert rank[old]["max_board_height"] == 0 and rank[old]["unknown_height_count"] == 1
    # No per-theme fallback to a historic day when today's data is empty.
    future = db.theme_strength_ranking("20261109")
    assert all(r["limit_up_count"] == 0 and not r["data_available"] for r in future.values())


def test_inactive_members_and_live_broken_board_do_not_inflate_ranking(tmp_path):
    db = database(tmp_path)
    identifier = pool(db, "机器人", range(3))
    for i in range(3):
        event(db, i, "5板")
    with db.connect() as con:
        con.execute("UPDATE theme_members SET active=0 WHERE code='600002.SH'")
        con.execute("INSERT INTO daily_limits VALUES ('2026-11-06','600000.SH',10,11,9)")
        con.execute("""INSERT INTO quote_snapshots VALUES (
          '2026-11-06','10:00','2026-11-06T10:00:00+08:00','600000.SH','股票0',
          10,10,11,10,10.8,1,1,1,'10:00',8)""")
    rank = db.theme_strength_ranking(DAY)[identifier]
    assert rank["limit_up_count"] == 1 and rank["max_board_height"] == 5
    assert rank["leaders"] == [{"code": "600001.SH", "name": "股票1"}]


def test_live_board_height_continues_only_from_previous_open_day(tmp_path):
    db = database(tmp_path)
    identifier = pool(db, "机器人", [0])
    db.replace_calendar([{"cal_date": d, "is_open": 1} for d in ["20261105", DAY]])
    event(db, 0, "4板", day="20261105")
    event(db, 0, "")
    rank = db.theme_strength_ranking(DAY)[identifier]
    assert rank["max_board_height"] == 5 and rank["inferred_height_count"] == 1
    # A missing intervening trade day cannot be treated as consecutive.
    db.replace_calendar([{"cal_date": d, "is_open": 1}
                         for d in ["20261104", "20261105", DAY]])
    with db.connect() as con:
        con.execute("UPDATE kpl_events SET trade_date='20261104' WHERE trade_date='20261105'")
    rank = db.theme_strength_ranking(DAY)[identifier]
    assert rank["max_board_height"] == 0 and rank["unknown_height_count"] == 1


def test_index_exposes_strength_without_loading_theme_details(tmp_path):
    app = create_app(Settings(tushare_token="test", app_api_token="test",
                              db_path=tmp_path / "api.db", archive_dir=tmp_path / "archive"))
    db = app.state.service.database
    db.initialize()
    db.upsert_stocks([{"ts_code": "600000.SH", "name": "股票0", "market": "主板"}])
    identifier = pool(db, "机器人", [0])
    event(db, 0, "5板")
    db.replace_calendar([{"cal_date": DAY, "is_open": 1}])
    app.state.service.clock.china_now = lambda: datetime(2026, 11, 7, 10, tzinfo=SHANGHAI)

    def forbidden(*args, **kwargs):
        raise AssertionError("The index must not hydrate detail/history")
    db.list_themes = forbidden
    client = TestClient(app)
    assert client.get("/api/v1/dashboard?view=index").status_code == 401
    r = client.get("/api/v1/dashboard?view=index", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200 and "theme_ranking;dur=" in r.headers["server-timing"]
    [item] = r.json()["themes"]
    assert item["id"] == identifier and "members" not in item
    assert item["market_rank"]["trade_date"] == DAY  # Weekend uses last open day.
    assert item["market_rank"]["max_board_height"] == 5


def test_frontend_sorts_by_breadth_then_height_with_stable_ties():
    source = Path("src/stocktopic/static/app.js").read_text()
    comparator = source.split("function compareThemeStrength", 1)[1].split(
        "\nfunction renderConfirmed", 1)[0]
    rows = [
        {"id": 4, "market_rank": {"limit_up_count": 4, "max_board_height": 5}},
        {"id": 3, "market_rank": {"limit_up_count": 6, "max_board_height": 3}},
        {"id": 2, "market_rank": {"limit_up_count": 6, "max_board_height": 5}},
        {"id": 1, "pinned": 1, "market_rank": {"limit_up_count": 1, "max_board_height": 1}},
        {"id": 5, "market_rank": {"limit_up_count": 6, "max_board_height": 3}},
    ]
    script = ("const assert=require('node:assert/strict'); function compareThemeStrength"
              + comparator + "\nconst rows=" + json.dumps(rows) +
              ";assert.deepEqual(rows.sort(compareThemeStrength).map(r=>r.id),[2,3,5,4,1]);")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
