import logging
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings

HEADERS = {"Authorization": "Bearer token-secret"}


def fixture(tmp_path):
    app = create_app(Settings(tushare_token="test", app_api_token="token-secret",
                              db_path=tmp_path / "db.sqlite3", archive_dir=tmp_path / "archive"))
    db = app.state.service.database
    db.initialize()
    ids = []
    for i in range(2):
        ids.append(db.upsert_candidate(
            fingerprint=str(i), provisional_name="机器人", shared_tag="机器人",
            direction="positive", discovered_at="2026-11-06T10:00:00+08:00",
            day1_date="2026-11-06", discovery_reason="fixture",
            members=[{"code": f"60000{i}.SH", "name": "股票", "evidence": {}}],
        ))
    return app, db, ids


def test_split_detail_matches_legacy_and_emits_real_request_timings(tmp_path, caplog):
    app, db, (identifier, _) = fixture(tmp_path)
    client = TestClient(app)
    caplog.set_level(logging.INFO, logger="stocktopic.dashboard_diagnostics")
    full = client.get(f"/api/v1/themes/{identifier}?view=focus", headers=HEADERS).json()
    research_original = db._attach_theme_research

    def forbidden(*args):
        raise AssertionError("Core must not load research or funds")
    db._attach_theme_research = forbidden
    flows_original = db.attach_theme_fund_flows
    db.attach_theme_fund_flows = forbidden
    core = client.get(f"/api/v1/themes/{identifier}?view=core", headers=HEADERS)
    assert core.status_code == 200
    assert "board_history;dur=" in core.headers["server-timing"]
    assert "member_quotes;dur=" in core.headers["server-timing"]
    assert "detail_queue;dur=" in core.headers["server-timing"]
    assert core.json()["members"] and "fund_flow" not in core.json()
    db._attach_theme_research = research_original
    db.attach_theme_fund_flows = flows_original
    research = client.get(f"/api/v1/themes/{identifier}/research", headers=HEADERS).json()
    flows = client.get(f"/api/v1/themes/{identifier}/fund-flows", headers=HEADERS).json()
    merged = core.json()
    merged.pop("deferred_sections")
    merged.update({k: v for k, v in research.items() if k != "id"})
    merged["fund_flow"] = flows["fund_flow"]
    by_code = {m["code"]: m["fund_flow"] for m in flows["members"]}
    for m in merged["members"]:
        m["fund_flow"] = by_code[m["code"]]
    assert merged == full
    assert "theme_detail_timing" in caplog.text and "token-secret" not in caplog.text
    for suffix in ["?view=core", "/research", "/fund-flows"]:
        assert client.get(f"/api/v1/themes/{identifier}{suffix}").status_code == 401
        assert client.get(f"/api/v1/themes/999999{suffix}", headers=HEADERS).status_code == 404


def test_selected_themes_and_latest_snapshots_skip_unused_invalid_history(tmp_path):
    _, db, (identifier, other) = fixture(tmp_path)
    with db.connect() as con:
        con.execute("UPDATE theme_members SET evidence_json='invalid' WHERE theme_id=?", (other,))
    assert [t["id"] for t in db.list_themes(theme_ids=[identifier])] == [identifier]
    assert db.list_themes(theme_ids=[]) == []
    with db.connect() as con:
        for i in range(100):
            con.execute("INSERT INTO flow_history(kind,owner,trade_date,slot,captured_at,payload) "
                        "VALUES ('stock','600000.SH','20261106','10:00',?,'invalid')",
                        (f"2026-11-06T09:00:{i:03d}",))
    db.save_flow("stock", "600000.SH", "20261106", "10:00", "2026-11-06T10:00:00",
                 {"slot": "10:00", "value": 1})
    db.save_flow("stock", "600000.SH", "20261106", "close", "2026-11-06T17:00:00",
                 {"slot": "close", "value": 2})
    themes = [{"id": identifier, "members": [{"code": "600000.SH"}]}]
    db.attach_theme_fund_flows(themes, "20261106", "close")
    flow = themes[0]["members"][0]["fund_flow"]
    assert flow["daily"]["value"] == 2 and flow["intraday"]["value"] == 1


def test_detail_history_queries_use_indexes_and_stop_before_old_rows(tmp_path):
    _, db, _ = fixture(tmp_path)
    with db.connect() as con:
        con.executemany(
            "INSERT INTO kpl_events(trade_date,code,name,board_tag,themes_json,raw_json,synced_at) "
            "VALUES (?,'600000.SH','股票','涨停','[]',?,'2026-11-06')",
            [(f"{i:08d}", "{}" if i >= 22 else "invalid") for i in range(30)],
        )
        history, _ = db._load_board_context(con, ["600000.SH"])
        assert len(history["600000.SH"]) == 8
        plan = " ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM kpl_events WHERE code='600000.SH' "
            "ORDER BY trade_date DESC,synced_at DESC LIMIT 8"))
        assert "idx_kpl_code_date" in plan and "TEMP B-TREE" not in plan
        plan = " ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(trade_date) FROM anomaly_events"))
        assert "idx_anomaly_date_direction_code" in plan


def test_browser_paints_core_before_sections_and_caches_and_cancels():
    source = Path("src/stocktopic/static/app.js").read_text()
    function = "function fetchThemeDetail" + source.split("function fetchThemeDetail", 1)[1].split(
        "function detailSectionState", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
let activeDetailLoad=null;
const themeDetailCache=new Map();
const state={authenticated:true,detailRequest:0};
const requests=[];
const frames=[];
const window={dispatchEvent(){}};
function CustomEvent(){};
function renderActiveView(){frames.push(state.detail?.id);}
function api(path, options){
  return new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
}
const tick=()=>new Promise(setImmediate);
(async()=>{
  let p=fetchThemeDetail(1);
  assert.equal(fetchThemeDetail(1),p); // De-duplicate the same in-flight theme.
  requests[0].resolve({id:1,members:[{code:'600000.SH'}]}); await tick();
  assert.equal(state.detail.id,1);
  assert.equal(requests.length,3);
  assert.equal(state.detail.detail_sections.research,'loading');
  assert.ok(frames.includes(1)); // Quotes are visible while both other requests wait.
  requests[1].resolve({fund_flow:{value:1},members:[]});
  requests[2].resolve({catalysts:[],admission_review:null,latest_explanation:null});
  await p;
  await fetchThemeDetail(1);
  assert.equal(requests.length,3); // Reopen uses the short-lived completed cache.
  const a=fetchThemeDetail(2), old=requests[3];
  const b=fetchThemeDetail(3);
  assert.ok(old.options.signal.aborted);
  old.resolve({id:2,members:[]});
  requests[4].resolve({id:3,members:[]}); await tick();
  assert.equal(state.detail.id,3); // A slow old response cannot overwrite the new theme.
  requests[5].reject(new Error('fund source failed'));
  requests[6].resolve({catalysts:[]});
  await Promise.all([a,b]);
  assert.equal(state.detail.id,3);
  assert.equal(state.detail.detail_sections['fund-flows'],'error');
  assert.equal(state.detail.detail_sections.research,'ready');
  assert.equal(themeDetailCache.has(3),false); // Partial failure is retryable, not cached as fresh.
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", function + script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
