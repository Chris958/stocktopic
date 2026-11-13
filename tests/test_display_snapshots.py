"""Persisted projections: GET paths never recompute business/display data."""
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.db import Database
from stocktopic.display_invalidation import PREFIX, get_value
from stocktopic.display_snapshots import DisplaySnapshots, build_projection
from stocktopic.domain import Quote
from stocktopic.market_clock import SHANGHAI

NOW = datetime(2026, 11, 6, 10, tzinfo=SHANGHAI)
DAY = "20261106"
AUTH = {"Authorization": "Bearer snapshot-test"}


@pytest.fixture
def env(tmp_path):
    app = create_app(Settings(tushare_token="test", app_api_token="snapshot-test",
                              db_path=tmp_path / "db.sqlite3", archive_dir=tmp_path / "archive"))
    svc = app.state.service
    svc.clock.china_now = lambda: NOW
    svc.database.initialize()
    svc.database.replace_calendar([{"cal_date": DAY, "is_open": 1}])
    svc.database.upsert_stocks([
        {"ts_code": f"60000{i}.SH", "name": f"成员{i}", "market": "主板"} for i in range(3)
    ])
    ids = []
    for i in range(2):
        identifier = svc.database.upsert_candidate(
            fingerprint=str(i), provisional_name=f"题材{i}", shared_tag=f"题材{i}",
            direction="positive", discovered_at=NOW.isoformat(), day1_date="2026-11-06",
            discovery_reason="test", members=[{"code": f"60000{i}.SH", "name": f"成员{i}",
                                             "evidence": {}}],
        )
        ids.append(identifier)
    svc.database.set_theme_status(ids[0], "confirmed")
    svc.display.start = lambda: None  # Most tests build deterministically.
    yield app, svc, ids
    svc.display.stop()


def test_read_paths_only_use_persisted_data_and_keep_auth(env, monkeypatch):
    app, svc, (identifier, _) = env
    assert svc.display.refresh_once()
    client = TestClient(app)
    def forbidden(*args, **kwargs):
        pytest.fail("A display GET attempted synchronous computation")
    for name in ("list_themes", "_load_board_context", "attach_theme_fund_flows",
                 "theme_research", "theme_index", "theme_strength_ranking"):
        monkeypatch.setattr(svc.database, name, forbidden)
    paths = ["/api/v1/theme-display", f"/api/v1/theme-display/{identifier}",
             f"/api/v1/theme-display/{identifier}?section=funds",
             f"/api/v1/theme-display/{identifier}?section=research"]
    for path in paths:
        assert client.get(path).status_code == 401
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["snapshot"]["state"] == "ready"
        assert "snapshot_read;dur=" in response.headers["server-timing"]
    assert client.get("/api/v1/theme-display/9999", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/theme-display/{identifier}?section=wrong", headers=AUTH
                      ).status_code == 422


def test_restart_reads_last_persisted_result_without_a_rebuild(env):
    _, svc, (identifier, _) = env
    svc.display.refresh_once()
    previous = svc.display.read_theme(identifier)
    other = DisplaySnapshots(svc.database, svc.clock)
    assert other.read_theme(identifier) == previous
    assert other.prepare() is None


def test_miss_queues_build_and_does_not_bypass_no_ai_or_admission(env):
    app, svc, (_, candidate) = env
    client = TestClient(app)
    response = client.get(f"/api/v1/theme-display/{candidate}", headers=AUTH)
    assert response.status_code == 202 and response.headers["retry-after"] == "1"
    assert response.json()["snapshot"]["state"] == "building"
    # An explicit request survives an already queued wildcard invalidation.
    svc.display.refresh_once()
    result = client.get(f"/api/v1/theme-display/{candidate}", headers=AUTH)
    assert result.status_code == 200 and result.json()["status"] == "pending"
    assert svc.database.get_theme(candidate)["status"] == "pending"


def test_market_update_marks_core_stale_but_not_funds_then_rebuilds(env):
    _, svc, (identifier, _) = env
    svc.display.refresh_once()
    prior = svc.display.read_theme(identifier)
    svc.database.save_quotes([Quote(
        code="600000.SH", name="成员0", pre_close=10, open=10, high=11, low=10, close=11,
        volume=1, amount=1, trades=1, trade_time="10:00", captured_at=NOW,
    )], "2026-11-06", "10:00")
    stale = svc.display.read_theme(identifier)
    assert stale["snapshot"]["state"] == "stale"
    assert stale["snapshot"]["revision"] == prior["snapshot"]["revision"]
    assert svc.display.read_theme(identifier, "funds")["snapshot"]["state"] == "ready"
    svc.display.refresh_once()
    fresh = svc.display.read_theme(identifier)
    assert fresh["snapshot"]["state"] == "ready"
    assert fresh["members"][0]["current_price"] == 11


@pytest.mark.parametrize("operation", ["archive", "member"])
def test_old_build_cannot_overwrite_new_archive_or_members(env, operation):
    _, svc, (identifier, _) = env
    svc.display.refresh_once()
    svc.display.request(identifier)
    day, rev, pending, plan = svc.display.prepare()
    result = build_projection(str(svc.database.path), str(svc.database.archive_dir), day, rev, plan)
    if operation == "archive":
        svc.database.archive_theme(identifier)
    else:
        svc.database.update_direction_members(identifier, [
            {"code": "600002.SH", "name": "新成员", "evidence": {}}
        ], NOW.isoformat())
    assert not svc.display.publish(day, rev, pending, result)
    read = svc.display.read_theme(identifier)
    assert read["snapshot"]["state"] == "stale"
    if operation == "archive":
        assert read["status"] == "archived"
        assert next(t for t in svc.display.read_index()["themes"]
                    if t["id"] == identifier)["status"] == "archived"
    svc.display.refresh_once()
    read = svc.display.read_theme(identifier)
    assert read["snapshot"]["state"] == "ready"
    if operation == "member":
        assert {m["code"] for m in read["members"]} == {"600000.SH", "600002.SH"}


def test_fund_failure_keeps_old_good_snapshot_and_does_not_block_core(env):
    _, svc, (identifier, _) = env
    svc.display.refresh_once()
    old = svc.display.read_theme(identifier, "funds")
    svc.database.save_flow("stock", "600000.SH", DAY, "close", NOW.isoformat(),
                           {"slot": "close", "main_net": 123})
    svc.display.request(identifier, ("core",))
    def partial(*args):
        result = build_projection(*args)
        result.pop(f"theme:{identifier}:funds", None)
        result["_errors"] = {f"theme:{identifier}:funds": "RuntimeError"}
        return result
    assert svc.display.refresh_once(partial)
    assert svc.display.read_theme(identifier)["snapshot"]["state"] == "ready"
    failed = svc.display.read_theme(identifier, "funds")
    assert failed["fund_flow"] == old["fund_flow"]
    assert failed["snapshot"]["state"] == "stale"
    assert failed["snapshot"]["last_error"]
    svc.display.refresh_once()
    recovered = svc.display.read_theme(identifier, "funds")
    assert recovered["members"][0]["fund_flow"]["daily"]["main_net"] == 123


def test_fund_snapshot_omits_raw_sources_and_duplicate_member_objects(env):
    _, svc, (identifier, _) = env
    svc.database.save_flow("theme", str(identifier), DAY, "close", NOW.isoformat(), {
        "slot": "close", "main_net": 100, "members": [{"evidence": "x" * 10000}],
        "sources": {"tushare": {"main_net": 100, "raw": {"huge": "x" * 10000}}},
    })
    svc.display.refresh_once()
    data = svc.display.read_theme(identifier, "funds")["fund_flow"]["daily"]
    assert data["main_net"] == 100 and data["sources"]["tushare"]["main_net"] == 100
    assert "members" not in data and "raw" not in data["sources"]["tushare"]
    assert len(json.dumps(data)) < 200


def test_failed_business_transaction_does_not_invalidate_snapshot(env):
    _, svc, _ = env
    svc.display.refresh_once()
    with svc.database.connect() as con:
        revision = get_value(con, "revision")
    with pytest.raises(ValueError):
        svc.database.merge_themes(1, [1])
    with svc.database.connect() as con:
        assert get_value(con, "revision") == revision


def test_builder_uses_a_separate_process_and_read_only_database(env):
    _, svc, _ = env
    day, rev, pending, plan = svc.display.prepare()
    with ProcessPoolExecutor(max_workers=1,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        result = pool.submit(build_projection, str(svc.database.path),
                             str(svc.database.archive_dir), day, rev, plan).result(timeout=20)
    assert result["index"]["snapshot"]["builder_pid"] != os.getpid()
    assert svc.display.publish(day, rev, pending, result)
    readonly = Database(svc.database.path, svc.database.archive_dir, read_only=True)
    with pytest.raises(Exception, match="readonly"):
        readonly.set_metadata("must_not_write", "value")


def test_snapshot_writes_do_not_create_an_invalidation_loop(env):
    _, svc, _ = env
    svc.display.refresh_once()
    with svc.database.connect() as con:
        revision = get_value(con, "revision")
        assert get_value(con, "pending") == {}
    assert not svc.display.refresh_once()
    with svc.database.connect() as con:
        assert get_value(con, "revision") == revision
        assert con.execute("SELECT count(*) FROM metadata WHERE key LIKE ?",
                           (PREFIX + "theme:%",)).fetchone()[0] == 3


def test_new_historical_request_during_build_survives_publish(env):
    _, svc, (_, historical) = env
    day, rev, pending, plan = svc.display.prepare()
    result = build_projection(str(svc.database.path), str(svc.database.archive_dir), day, rev, plan)
    svc.display.request(historical, ("core",))
    assert svc.display.publish(day, rev, pending, result)
    with svc.database.connect() as con:
        assert str(historical) in get_value(con, "pending")["core"]
    svc.display.refresh_once()
    assert svc.display.read_theme(historical)["snapshot"]["state"] == "ready"


def test_stock_fund_update_only_invalidates_related_display_pools(env):
    _, svc, (formal, candidate) = env
    svc.display.request(candidate)
    svc.display.refresh_once()
    svc.database.save_flow("stock", "600000.SH", DAY, "close", NOW.isoformat(),
                           {"slot": "close", "main_net": 5})
    with svc.database.connect() as con:
        pending = get_value(con, "pending")
    assert pending == {"funds": [str(formal)]}
    assert svc.display.read_theme(candidate, "funds")["snapshot"]["state"] == "ready"


def test_new_source_failure_keeps_last_good_fund_value(env):
    _, svc, (identifier, _) = env
    svc.database.save_flow("stock", "600000.SH", DAY, "close", NOW.isoformat(),
                           {"slot": "close", "main_net": 5})
    svc.display.refresh_once()
    svc.database.save_flow("stock", "600000.SH", DAY, "close", "2026-11-06T11:00:00",
                           {"slot": "close", "main_net": None,
                            "sources": {"tushare": {"status": "unavailable"}}})
    svc.display.refresh_once()
    result = svc.display.read_theme(identifier, "funds")
    assert result["members"][0]["fund_flow"]["daily"]["main_net"] == 5
    assert result["snapshot"]["state"] == "stale"
    assert result["snapshot"]["last_error"]


def test_invalidation_and_business_write_rollback_together(env):
    from stocktopic.display_invalidation import display_mutation

    _, svc, (identifier, _) = env
    svc.display.refresh_once()
    before = svc.display.read_theme(identifier)

    @display_mutation("core")
    def failed_update(db):
        with db.connect() as con:
            con.execute("UPDATE candidate_themes SET final_name='must rollback' WHERE id=?",
                        (identifier,))
            raise RuntimeError("abort")
    with pytest.raises(RuntimeError, match="abort"):
        failed_update(svc.database)
    assert svc.display.read_theme(identifier) == before


def test_day_rollover_marks_snapshots_stale_and_is_refreshed_in_background(env):
    _, svc, (identifier, _) = env
    svc.display.refresh_once()
    svc.database.replace_calendar([{"cal_date": d, "is_open": 1} for d in [DAY, "20261109"]])
    svc.clock.china_now = lambda: datetime(2026, 11, 9, 9, tzinfo=SHANGHAI)
    assert svc.display.read_theme(identifier)["snapshot"]["state"] == "stale"
    svc.display.refresh_once()
    result = svc.display.read_theme(identifier)
    assert result["snapshot"]["trade_date"] == "20261109"
    assert result["snapshot"]["state"] == "ready"


def test_live_coordinator_builds_and_updates_without_any_get_recomputation(env):
    import time

    app, svc, (identifier, _) = env
    svc.display.start = DisplaySnapshots.start.__get__(svc.display)
    client = TestClient(app)
    try:
        first = client.get(f"/api/v1/theme-display/{identifier}", headers=AUTH)
        assert first.status_code in {200, 202}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = client.get(f"/api/v1/theme-display/{identifier}", headers=AUTH)
            if (result.status_code == 200 and result.json()["snapshot"]["state"] == "ready"
                    and svc.database.latest_run("display_snapshot")["status"] == "success"):
                break
            time.sleep(0.05)
        else:
            pytest.fail(str(svc.display.status()))
        index = client.get("/api/v1/theme-display", headers=AUTH).json()
        assert index["snapshot"]["builder_pid"] != os.getpid()
        assert svc.database.latest_run("display_snapshot")["status"] == "success"
        svc.database.archive_theme(identifier)
        # An old stored projection cannot undo the authoritative archive.
        result = client.get(f"/api/v1/theme-display/{identifier}", headers=AUTH)
        assert result.json()["status"] == "archived"
    finally:
        svc.display.stop()
        svc.display._thread.join(timeout=5)
        assert not svc.display._thread.is_alive()
