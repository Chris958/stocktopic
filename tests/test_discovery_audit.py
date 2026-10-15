import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from stocktopic.db import Database
from stocktopic.discovery_audit import AuditDatabase, build_report
from stocktopic.event_bridge import EventBridgeDetector
from stocktopic.market_clock import SHANGHAI
from stocktopic.theme_graph import structured_event_clusters
from stocktopic.themes import ThemeDiscovery

DAY = "20261009"


def rows(groups, reason="AI影视内容生产新模型"):
    events = []
    for tag, count, hour, minute in groups:
        for index in range(count):
            events.append(
                {
                    "code": f"600{len(events):03d}.SH",
                    "name": "测试股票",
                    "themes": [tag],
                    "limit_reason": reason,
                    "limit_up_time": f"{hour:02d}{minute + index:02d}00",
                    "board_tag": "涨停",
                }
            )
    return events


@pytest.mark.parametrize(
    "groups,expected",
    [
        ([("影视", 2, 9, 35), ("版权", 2, 9, 38)], 1),
        ([("影视", 4, 9, 35), ("版权", 2, 9, 38)], 1),
        ([("影视", 3, 9, 35), ("版权", 1, 9, 38)], 0),
        ([("影视", 2, 9, 35), ("版权", 2, 10, 5)], 1),
    ],
)
def test_audit_reproduces_current_bridge_coverage_limits(groups, expected):
    events = rows(groups)
    detector = EventBridgeDetector()
    result = detector.detect(DAY, events, structured_event_clusters(events))
    assert len(result) == expected
    if groups[-1][2] == 10:
        pair = detector.trace[0]
        assert pair["bridge_score"] >= 60
        assert pair["time_sync"] < 45
        assert pair["verification_route"] == "bounded_relay_review"


def test_overlap_is_deduplicated_but_unlinked_different_role_words_stay_out():
    events = rows([("影视", 3, 9, 35), ("版权", 2, 9, 38)])
    events[0]["themes"].append("版权")
    candidate = EventBridgeDetector().detect(DAY, events, structured_event_clusters(events))[0]
    assert len(candidate["member_codes"]) == 5
    events = rows([("影视", 2, 9, 35), ("版权", 2, 9, 38)])
    for item in events:
        item["limit_reason"] = "AIGC影视制作平台" if "影视" in item["themes"] else "网文IP授权"
    assert EventBridgeDetector().detect(DAY, events, structured_event_clusters(events)) == []


def setup_db(tmp_path):
    db = Database(tmp_path / "test.sqlite3", tmp_path / "archive")
    db.initialize()
    db.upsert_stocks(
        [{"ts_code": f"60000{i}.SH", "name": f"目标{i}", "market": "主板"} for i in range(4)]
    )
    db.upsert_kpl_events(
        [
            {
                "trade_date": DAY,
                "ts_code": f"60000{i}.SH",
                "name": f"目标{i}",
                "tag": "涨停",
                "theme": "影视",
                "status": "首板",
                "lu_time": "093500",
            }
            for i in range(3)
        ]
    )
    db.upsert_kpl_concept_members(
        [
            {
                "trade_date": DAY,
                "ts_code": "concept1",
                "name": "版权",
                "con_code": "600000.SH",
                "con_name": "目标0",
                "desc": "AI漫剧IP改编权",
                "hot_num": 10,
            }
        ]
    )
    return db


def test_read_only_report_keeps_persisted_evidence_separate_from_reconstruction(tmp_path):
    db = setup_db(tmp_path)
    now = datetime(2026, 10, 9, 10, 0, tzinfo=SHANGHAI)
    ThemeDiscovery(db).discover_for_date(DAY, now)
    db.set_metadata(
        f"discovery_trace:{DAY}:original-signature",
        json.dumps(
            {
                "strong_stock_count": 3,
                "local_clusters": [{"tag": "影视", "touch_count": 3}],
                "bridge_candidate_count": 0,
                "bridges": [],
            }
        ),
    )
    with db.connect() as con:
        before = con.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
    report = build_report(db.path, [DAY], ["目标0", "目标3", "初版传媒"], ["影视", "版权"])
    assert report["mode"] == "read_only_no_ai_no_network"
    assert report["unresolved_names"] == ["初版传媒"]
    day = report["dates"][0]
    assert [t["in_strong_input"] for t in day["targets"]] == [True, False]
    assert day["persisted_decisions"][0]["signature"] == "original-signature"
    assert day["reconstructed_local_clusters"][0]["touch_count"] == 3
    assert day["stored_concept_descriptions"][0]["description"] == "AI漫剧IP改编权"
    assert day["targets"][0]["event"]["concept_tags"][0]["reason"] == "AI漫剧IP改编权"
    assert day["related_themes"][0]["is_local_early_observation"] is True
    assert day["related_themes"][0]["visible_in_current_default_watch_tab"] is True
    with db.connect() as con:
        assert before == con.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
    readonly = AuditDatabase(db.path, db.archive_dir)
    with readonly.connect() as con, pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM metadata")


def test_real_frontend_shows_local_observations_and_separates_ai_review():
    source = (Path(__file__).parents[1] / "src/stocktopic/static/app.js").read_text()
    function = (
        "function isLocalObservation(theme) {"
        + source.split(
            "function isLocalObservation(theme) {",
            1,
        )[1].split("\nfunction renderConfirmed()", 1)[0]
    )
    script = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
let shown;
const state = {candidateFilter:'watching', data:{themes:[
  {id:1, status:'pending', admission_status:'early_observation'},
  {id:2, status:'watching', admission_status:'early_watch'},
  {id:3, status:'pending', admission_status:'awaiting_ai'}
]}};
vm.runInNewContext(SOURCE+';renderCandidates();', {
 state, renderThemes(selector, items){shown=items;}
});
assert.equal(shown.length, 2);
assert.equal(shown[0].id, 2);
assert.equal(shown[1].id, 1);
state.candidateFilter='reviewing';
vm.runInNewContext(SOURCE+';renderCandidates();', {
 state, renderThemes(selector, items){shown=items;}
});
assert.equal(shown.length, 1);
assert.equal(shown[0].id, 3);
"""
    result = subprocess.run(
        ["node", "-e", "const SOURCE=" + json.dumps(function) + ";" + script],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
