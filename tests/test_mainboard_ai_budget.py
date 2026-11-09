"""Cost and scope regressions with fixed local events; no live AI or market API."""
import json
from datetime import datetime, timedelta

import pytest

from stocktopic.ai import OpenAIThemeExplainer
from stocktopic.config import Settings
from stocktopic.db import Database
from stocktopic.market_clock import SHANGHAI
from stocktopic.service import StockTopicService
from stocktopic.theme_directions import (
    ThemeDirectionPool,
    consolidate_verified_directions,
    label_direction,
)
from stocktopic.theme_graph import install_graph_first_ai_clustering
from stocktopic.verification_cache import VerificationCache

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=SHANGHAI)
DAY = "20261106"


def db_at(tmp_path):
    db = Database(tmp_path / "db.sqlite3", tmp_path / "archive")
    db.initialize()
    db.replace_calendar([
        {"cal_date": day, "is_open": 1}
        for day in ["20261030", "20261102", "20261103", "20261104", "20261105", DAY]
    ])
    db.upsert_stocks([
        {"ts_code": f"60000{i}.SH", "name": f"成员{i}", "market": "主板"}
        for i in range(12)
    ] + [{"ts_code": "300001.SZ", "name": "旧创业板", "market": "创业板"}])
    return db


def theme(db, tag, codes, status="confirmed"):
    identifier = db.upsert_candidate(
        fingerprint=tag, provisional_name=tag, shared_tag=tag, direction="positive",
        discovered_at=NOW.isoformat(), day1_date="2026-11-06", discovery_reason="fixture",
        members=[{"code": code, "name": code, "evidence": {}} for code in codes],
    )
    db.set_theme_status(identifier, status, tag)
    return identifier


def sealed(db, code, day=DAY, tag="机器人"):
    db.upsert_kpl_events([{"trade_date": day, "ts_code": code,
                          "name": code, "tag": "涨停", "theme": tag, "status": "首板"}])


def rows():
    return [
        {"code": f"60000{i}.SH", "name": f"成员{i}", "themes": [tag], "board_tag": "涨停",
         "limit_reason": tag + "新增订单", "limit_up_time": f"093{i}00"}
        for tag, indices in [("固态电池", range(4)), ("创新药", range(4, 8))]
        for i in indices
    ]


def graph_client(db, requests):
    install_graph_first_ai_clustering()
    client = OpenAIThemeExplainer("test", "test-model")
    client.verification_cache = VerificationCache(db, NOW)

    def verify(prompt, **kwargs):
        nodes = json.loads(prompt.split("图谱候选：", 1)[1].split("\n输入强势股票")[0])
        requests.append([n["anchor_tag"] for n in nodes])
        assert kwargs["subject_id"].startswith(DAY + ":graph:")
        return {}, {"clusters": [
            {"anchor_tag": n["anchor_tag"], "canonical_name": n["anchor_tag"] + "订单催化",
             "member_codes": n["member_codes"], "common_logic": "订单推动",
             "cluster_confidence": 90} for n in nodes
        ]}, []
    client._call_prompt = verify
    return client


def test_candidate_cache_survives_restart_and_ignores_unrelated_changes(tmp_path):
    db = db_at(tmp_path)
    requests = []
    client = graph_client(db, requests)
    events = rows()
    first = client.cluster_limit_events(DAY, events)
    assert len(first) == 2 and len(requests) == 1
    # Fresh client (process restart), changed price only: no new request.
    client = graph_client(db, requests)
    events[0]["pct_change"] = 9.5
    assert len(client.cluster_limit_events(DAY, events)) == 2
    assert len(requests) == 1
    # Change one business reason: only that node is reverified, the other is retained.
    events[0]["limit_reason"] = "固态电池新产品量产"
    assert len(client.cluster_limit_events(DAY, events)) == 2
    assert requests[-1] == ["固态电池"] and len(requests) == 2
    events.append({**events[0], "code": "600009.SH"})
    client.cluster_limit_events(DAY, events)
    assert requests[-1] == ["固态电池"] and len(requests) == 3


def test_negative_cache_has_two_rechecks_but_new_evidence_is_immediate(tmp_path):
    db = db_at(tmp_path)
    cache = VerificationCache(db, NOW.replace(hour=9, minute=29))
    node = {"tag": "机器人", "member_codes": [e["code"] for e in rows()[:4]]}
    key = cache.identity(DAY, "graph", node, rows(), "model")
    cache.save(key, "rejected", [], {"reject_reason": "evidence_insufficient"})
    assert cache.lookup(key)
    cache.now += timedelta(minutes=121)
    assert cache.lookup(key) is None
    cache.save(key, "rejected", [], {})
    cache.now = NOW.replace(hour=14, minute=3)
    assert cache.lookup(key) is None
    cache.save(key, "rejected", [], {})
    cache.now = NOW.replace(hour=14, minute=59)
    assert cache.lookup(key)
    changed = rows()
    changed[0]["limit_reason"] = "公司披露新订单"
    new_key = cache.identity(DAY, "graph", node, changed, "model")
    assert new_key != key and cache.lookup(new_key) is None
    # Old-day replay has no timer-driven recheck.
    cache.now = NOW + timedelta(days=3)
    assert cache.lookup(key)


def test_old_chinext_rows_survive_but_cannot_enter_live_scope(tmp_path):
    db = db_at(tmp_path)
    identifier = theme(db, "机器人", ["300001.SZ", "600000.SH"])
    # Simulate the database produced by the preceding release.
    with db.connect() as con:
        con.execute("UPDATE stocks SET active=1 WHERE code='300001.SZ'")
        con.execute(
            "INSERT INTO kpl_events(trade_date,code,name,board_tag,themes_json,raw_json,synced_at)"
            " VALUES(?, '300001.SZ','旧创业板','涨停','[\"机器人\"]','{}',?)",
            (DAY, NOW.isoformat()),
        )
    assert "300001.SZ" not in db.active_stock_map()  # Even before the startup cleanup.
    db.initialize()
    assert db.limit_touch_events(DAY) == []
    assert db.kpl_theme_clusters(DAY) == []
    old = db.get_theme(identifier)
    assert len(old["members"]) == 2
    assert next(m for m in old["members"] if m["code"] == "300001.SZ")["active"] == 0


def test_recent_strength_window_expires_and_can_reactivate_from_saved_relation(tmp_path):
    db = db_at(tmp_path)
    identifier = theme(db, "机器人", ["600000.SH", "600001.SH", "600002.SH"])
    sealed(db, "600000.SH", "20261030")  # Sixth trading day: outside the window.
    sealed(db, "600001.SH", "20261102")  # Fifth day: still live.
    # An incomplete feed must not retire members merely because evidence is missing.
    assert db.prune_direction_members(DAY)["status"] == "incomplete_coverage"
    assert all(m["active"] for m in db.get_theme(identifier)["members"])
    for day in db.open_trade_dates(DAY, 5):
        db.set_metadata(f"kpl_event_coverage:{day}", '{"state":"available"}')
    db.prune_direction_members(DAY)
    assert {m["code"] for m in db.get_theme(identifier)["members"] if m["active"]} == {"600001.SH"}
    [focused] = db.list_themes(theme_id=identifier, focus_only=True)
    assert [m["code"] for m in focused["members"]] == ["600001.SH"]
    # An inactive, previously admitted member returns after sealing again.
    sealed(db, "600000.SH", DAY, tag="")
    ThemeDirectionPool(db).observe(DAY, NOW, db.limit_touch_events(DAY))
    assert {m["code"] for m in db.get_theme(identifier)["members"] if m["active"]} == {
        "600000.SH", "600001.SH"}
    ThemeDirectionPool(db).observe("20261030", NOW, db.limit_touch_events("20261030"))
    assert {m["code"] for m in db.get_theme(identifier)["members"] if m["active"]} == {
        "600000.SH", "600001.SH"}  # Old replay cannot rewind today's focus.
    scope = json.loads(db.get_metadata("direction_member_scope"))
    assert scope["trade_dates"][0] == DAY and scope["status"] == "applied"
    assert len(db.get_theme(identifier)["members"]) == 3  # Historical relation not deleted.


def test_solar_green_power_existing_pools_merge_with_history_and_stable_id(tmp_path):
    db = db_at(tmp_path)
    a = theme(db, "光伏", ["600000.SH", "600001.SH"])
    b = theme(db, "绿色电力", ["600001.SH", "600002.SH"])
    db.set_metadata(f"direction_state:{a}", '{"name":"光伏"}')
    db.set_metadata(f"direction_state:{b}", '{"name":"绿色电力"}')
    db.save_theme_catalysts(b, [{"title": "绿电交易", "summary": "新的绿电交易规则", "url": "https://example.com/green"}])
    pool = ThemeDirectionPool(db)
    pool.normalize_existing(NOW)
    first, second = db.get_theme(a), db.get_theme(b)
    assert first["final_name"] == "光伏/绿色电力" and second["status"] == "merged"
    assert second["merged_into_id"] == a and len(first["members"]) == 3
    assert first["catalysts"][0]["title"] == "绿电交易"
    pool.normalize_existing(NOW)
    assert len(db.list_themes("confirmed")) == 1
    assert label_direction("煤电") is None


def test_direction_consolidation_never_combines_early_nodes_to_bypass_four_gate():
    nodes = [{"tag": tag, "member_codes": [f"{i}{j}" for j in range(2)]}
             for i, tag in enumerate(["光伏", "绿色电力"])]
    assert len(consolidate_verified_directions(nodes)) == 2
    for n in nodes:
        n["member_codes"] += [c + "x" for c in n["member_codes"]]
        n["canonical_name"] = n["tag"] + "新政策"
    combined = consolidate_verified_directions(nodes)
    assert len(combined) == 1 and len(combined[0]["member_codes"]) == 8


def test_admission_budget_is_bounded_and_new_catalysts_reset_it(tmp_path):
    svc = StockTopicService(Settings(tushare_token="test", db_path=tmp_path / "budget.db",
                                    archive_dir=tmp_path / "archive"))
    svc.database.initialize()
    clock = [NOW]
    svc.clock.china_now = lambda: clock[0]
    candidate = {"id": 1, "members": [{"code": "600000.SH"}]}
    assert svc._admission_budget_available(candidate)
    assert not svc._admission_budget_available(candidate)
    clock[0] += timedelta(minutes=31)
    assert svc._admission_budget_available(candidate)
    clock[0] += timedelta(minutes=61)
    assert svc._admission_budget_available(candidate)
    clock[0] += timedelta(hours=3)
    assert not svc._admission_budget_available(candidate)
    candidate["catalysts"] = [{"url": "https://example.com/new", "summary": "新订单"}]
    assert svc._admission_budget_available(candidate)


def test_optional_search_reuses_evidence_and_never_sends_unbounded_output():
    client = OpenAIThemeExplainer("test", "model", "https://relay.example/v1")
    requests = []

    def response(payload):
        requests.append(payload)
        return {"output": []}
    client._request_payload = response
    client._call_prompt("existing verified evidence", reasoning_effort="medium",
                        task_type="admission_analysis", search_required=False)
    assert requests[0]["tool_choice"] == "auto"
    assert requests[0]["max_tool_calls"] == 5

    def reject(payload):
        requests.append(payload)
        raise RuntimeError("OpenAI HTTP 400: Unsupported parameter: max_output_tokens")
    client._request_controls_mode = None
    client._request_payload = reject
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        client._call_prompt("test", reasoning_effort="low", task_type="admission_analysis")
    assert all("max_output_tokens" in r for r in requests)
