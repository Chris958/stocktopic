import copy
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.db import Database
from stocktopic.domain import Anomaly, Direction
from stocktopic.market_clock import SHANGHAI

DATE = "20261013"


def database(tmp_path):
    db = Database(tmp_path / "test.sqlite3", tmp_path / "archive")
    db.initialize()
    return db


def theme(db, number, codes):
    return db.upsert_candidate(
        fingerprint=f"perf-{number}",
        provisional_name=f"具体事件{number}",
        shared_tag=f"具体事件{number}",
        direction="positive",
        discovered_at="2026-10-13T09:30:00+08:00",
        day1_date="2026-10-13",
        discovery_reason="测试",
        members=[{"code": code, "name": code, "evidence": {}} for code in codes],
    )


def trace(db):
    original = db.connect
    statements = []

    @contextmanager
    def connect():
        with original() as con:
            con.set_trace_callback(statements.append)
            yield con

    db.connect = connect
    return statements


def test_board_context_batch_matches_individual_and_followers_stay_theme_local(tmp_path):
    db = database(tmp_path)
    codes = [f"60000{i}.SH" for i in range(3)]
    db.upsert_stocks([{"ts_code": code, "name": code, "market": "主板"} for code in codes])
    theme(db, 1, codes[:2])
    theme(db, 2, codes[1:])
    db.upsert_kpl_events(
        [
            {
                "trade_date": DATE,
                "ts_code": code,
                "name": code,
                "tag": "涨停",
                "theme": "具体事件",
                "status": "首板",
                "lu_time": f"09{35 + i}00",
            }
            for i, code in enumerate(codes)
        ]
    )
    db.save_anomalies(
        [
            Anomaly(
                code=code,
                name=code,
                captured_at=datetime(2026, 10, 13, 9, 35 + i, tzinfo=SHANGHAI),
                direction=Direction.POSITIVE,
                severity=80,
                pct_change=10,
                change_5m=2,
                amount_delta=1000,
                trade_delta=10,
                is_hard_event=True,
                event_types=("limit",),
                reasons=("测试",),
            )
            for i, code in enumerate(codes)
        ]
    )
    statements = trace(db)
    themes = db.list_themes()
    assert sum("MAX(trade_date) AS trade_date FROM anomaly_events" in s for s in statements) == 1
    assert sum("WITH ranked AS" in s for s in statements) == 1
    for item in themes:
        expected = copy.deepcopy(item)
        with db.connect() as con:
            db._attach_board_context(con, expected)
        assert expected == item
        assert max(m["follow_count_30m"] for m in item["members"]) == 1


def test_bulk_fund_flows_keep_latest_close_and_intraday_and_missing_values(tmp_path):
    db = database(tmp_path)
    themes = [
        {"id": 1, "members": [{"code": "600001.SH"}, {"code": "missing"}]},
        {"id": 2, "members": [{"code": "600001.SH"}]},
    ]
    for kind, owner in [("theme", "1"), ("stock", "600001.SH")]:
        for index, slot in enumerate(["10:00", "close", "10:15", "close"]):
            db.save_flow(
                kind,
                owner,
                DATE,
                slot,
                f"2026-10-13T17:{index:02d}:00+08:00",
                {"slot": slot, "value": index},
            )
        db.save_flow(kind, owner, "20261012", "close", "2026-10-14", {"slot": "close"})
    expected = copy.deepcopy(themes)
    for item in expected:
        rows = db.flow_history("theme", str(item["id"]), DATE)
        item["fund_flow"] = {
            "daily": next((r for r in rows if r.get("slot") == "close"), None),
            "intraday": next((r for r in rows if r.get("slot") != "close"), None),
            "trade_date": DATE,
        }
        for member in item["members"]:
            rows = db.flow_history("stock", member["code"], DATE)
            member["fund_flow"] = {
                "daily": next((r for r in rows if r.get("slot") == "close"), None),
                "intraday": next((r for r in rows if r.get("slot") != "close"), None),
            }
    statements = trace(db)
    db.attach_theme_fund_flows(themes, DATE, "close")
    assert themes == expected
    assert sum("SELECT owner, payload FROM flow_history" in s for s in statements) == 2


def test_bulk_fund_flows_batch_parameters_and_deduplicate_stocks(tmp_path):
    db = database(tmp_path)
    members = [{"code": f"600{i:03d}.SH"} for i in range(801)]
    themes = [{"id": i, "members": copy.deepcopy(members)} for i in range(2)]
    statements = trace(db)
    db.attach_theme_fund_flows(themes, DATE, "close")
    assert sum("SELECT owner, payload FROM flow_history" in s for s in statements) == 4
    assert all(
        m["fund_flow"] == {"daily": None, "intraday": None} for t in themes for m in t["members"]
    )


def test_dashboard_query_does_not_block_health_and_runs_in_worker(tmp_path):
    app = create_app(
        Settings(
            tushare_token="test",
            db_path=tmp_path / "test.sqlite3",
            archive_dir=tmp_path / "archive",
            app_api_token="test-token",
        )
    )
    app.state.service.initialize_reference_data = lambda: None
    entered = threading.Event()
    release = threading.Event()
    worker_names = []

    def blocked():
        worker_names.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5)
        return []

    app.state.service.database.list_themes = blocked
    try:
        with TestClient(app) as client, ThreadPoolExecutor(2) as executor:
            dashboard = executor.submit(
                client.get, "/api/v1/dashboard", headers={"Authorization": "Bearer test-token"}
            )
            assert entered.wait(2)
            health = executor.submit(client.get, "/health").result(timeout=2)
            assert health.status_code == 200  # Dashboard is still blocked at this point.
            release.set()
            assert dashboard.result(timeout=3).status_code == 200
        assert worker_names[0].startswith("dashboard")
    finally:
        release.set()


def test_frontend_shared_refresh_and_structure_panel_never_refetch():
    root = Path(__file__).parents[1] / "src/stocktopic/static"
    app_source = (root / "app.js").read_text()
    load = app_source.split("function load(manual = false) {", 1)[1].split(
        "\nfunction render()", 1
    )[0]
    plugin = (root / "intelligence-panel.js").read_text()
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
let calls = 0, resolve, events = [];
const classes = {add(){}, remove(){}};
const context = {
  dashboardLoadInFlight: null, state: {authenticated:true},
  $: () => ({classList:classes}), toast(){}, render(){ events.push('render'); },
  api: () => { calls++; return new Promise(r => {resolve = r;}); },
  window: {dispatchEvent(e){events.push(e.type);}},
  CustomEvent: class {constructor(type, options){this.type=type; this.detail=options.detail;}}
};
vm.createContext(context);
vm.runInContext('function load(manual=false) {' + LOAD, context);
(async () => {
  const a = context.load(), b = context.load(true);
  assert.equal(a, b);
  assert.equal(calls, 1);
  resolve({themes:[]}); await a;
  assert.deepEqual(events, ['render','stocktopic:dashboard']);
  const c = context.load(); assert.equal(calls, 2);
  resolve({themes:[]}); await c;
  let handler, timers = [];
  vm.runInNewContext(PLUGIN, {
    window: {addEventListener(type, h){assert.equal(type,'stocktopic:dashboard'); handler=h;}},
    document: {readyState:'complete', getElementById(){return null;},
               querySelectorAll(){return [];}},
    MutationObserver: class {observe(){}}, clearTimeout(){},
    setTimeout(fn){timers.push(fn);},
    fetch(){throw new Error('structure panel must not fetch');}
  });
  handler({detail:{themes:[]}});
  timers.forEach(fn => fn());
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            "const LOAD=" + json.dumps(load) + ";const PLUGIN=" + json.dumps(plugin) + ";" + script,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_board_context_large_universe_batches_and_keeps_stock_history(tmp_path):
    db = database(tmp_path)
    codes = [f"600{i:03d}.SH" for i in range(801)]
    db.upsert_stocks([{"ts_code": code, "name": code, "market": "主板"} for code in codes])
    db.upsert_kpl_events([
        {"trade_date": DATE, "ts_code": code, "name": code, "tag": "涨停",
         "theme": "具体事件", "status": "首板"} for code in codes
    ])
    statements = trace(db)
    with db.connect() as con:
        histories, moves = db._load_board_context(con, codes + codes)
    assert set(histories) == set(codes)
    assert moves == {}
    assert all(len(value) == 1 for value in histories.values())
    assert sum("WITH ranked AS" in s for s in statements) == 3
    assert sum("MAX(trade_date) AS trade_date FROM anomaly_events" in s for s in statements) == 1


def test_scheduler_lightweight_states_preserve_admission_due_checks(tmp_path):
    from stocktopic.service import _admission_candidate_due

    db = database(tmp_path)
    ids = [theme(db, i, []) for i in range(6)]
    now = datetime(2026, 10, 13, 10, 30, tzinfo=SHANGHAI)
    db.set_admission_status(ids[0], "awaiting_ai", "测试")
    db.set_admission_status(ids[1], "early_observation", "测试")
    db.set_admission_status(ids[2], "analysis_failed", "测试")
    db.set_admission_status(ids[3], "analyzing", "测试")
    db.set_theme_status(ids[4], "watching")
    db.set_admission_status(ids[4], "awaiting_ai", "测试")
    db.set_theme_status(ids[5], "confirmed")
    expected = {int(t["id"]) for t in db.list_themes()
                if t["status"] in {"pending", "watching"} and _admission_candidate_due(t, now)}
    statements = trace(db)
    states = db.list_admission_states()
    assert {int(t["id"]) for t in states if _admission_candidate_due(t, now)} == expected
    assert {t["id"] for t in states} == set(ids[:5])
    assert all(set(t) == {"id", "status", "admission_status", "admission_reviewed_at"}
               for t in states)
    assert sum(s.startswith("SELECT") for s in statements) == 1
    assert not any("theme_members" in s or "quote_snapshots" in s for s in statements)
