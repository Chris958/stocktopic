from datetime import datetime
from unittest.mock import Mock

import pytest

from stocktopic.config import Settings
from stocktopic.db import Database
from stocktopic.fund_flow import EastmoneyFlowClient, aggregate, consensus, normalize, report
from stocktopic.market_clock import SHANGHAI
from stocktopic.service import StockTopicService

DATE = "20260904"
NOW = datetime(2026, 9, 4, 17, 10, tzinfo=SHANGHAI)


def row(code="600001.SH"):
    return dict(
        ts_code=code,
        trade_date=DATE,
        buy_lg_amount=100,
        sell_lg_amount=60,
        buy_elg_amount=200,
        sell_elg_amount=50,
        net_mf_amount=-999,
        net_amount=190,
    )


def test_normalization_and_non_equivalent_fields():
    value = normalize("tushare", row(), "600001.SH", DATE)
    assert value["main_net"] == 1900000
    assert value["large_net"] == 400000
    assert value["extra_large_net"] == 1500000
    ths = normalize("ths", row(), "600001.SH", DATE)
    assert ths["main_net"] == 1000000
    assert ths["extra_large_net"] is None
    assert ths["large_net"] is None
    assert normalize("eastmoney", row(), "600001.SH", DATE)["large_net"] == 1000000


@pytest.mark.parametrize("value", [None, "-", float("nan"), float("inf")])
def test_invalid_is_not_zero(value):
    data = row()
    data["buy_lg_amount"] = value
    with pytest.raises((ValueError, TypeError)):
        normalize("tushare", data, "600001.SH", DATE)


def test_date_mismatch_rejected():
    with pytest.raises(ValueError):
        normalize("tushare", row(), "600001.SH", "20260903")


def test_direction_consensus_amounts_need_not_match():
    sources = dict(tushare={"main_net": 1}, eastmoney={"main_net": 1e8}, ths={"main_net": 99})
    assert consensus(sources)["status"] == "confirmed"
    sources["ths"]["main_net"] = -1
    assert consensus(sources)["status"] == "disputed"
    assert consensus(sources)["score"] == 66.67
    sources.pop("ths")
    assert consensus(sources)["status"] == "insufficient"
    assert not consensus(sources)["direction_confirmed"]
    assert consensus({})["direction"] == "unknown"


def test_aggregate_dedup_and_missing_coverage():
    sources = {
        s: {"main_net": 10, "large_net": 3, "extra_large_net": 7}
        for s in ("tushare", "eastmoney", "ths")
    }
    reports = {"a": report(sources, DATE, NOW.isoformat())}
    members = [{"code": "a", "leader_rank": 1}, {"code": "a", "leader_rank": 2}]
    result = aggregate(members, reports, DATE, NOW.isoformat())
    assert result["main_net"] == 10
    assert result["target_count"] == 1
    members.append({"code": "b"})
    result = aggregate(members, reports, DATE, NOW.isoformat())
    assert result["main_net"] is None
    assert result["breadth"] == 50
    assert not result["breadth_complete"]
    assert result["consensus"]["status"] == "insufficient"


@pytest.mark.parametrize(
    "hour,minute,open_day,expected",
    [
        (9, 15, True, []),
        (9, 30, True, ["09:30"]),
        (10, 14, True, ["10:00"]),
        (10, 15, True, ["10:15"]),
        (11, 31, True, []),
        (12, 0, True, []),
        (13, 0, True, ["13:00"]),
        (15, 1, True, []),
        (17, 10, True, ["close"]),
        (10, 0, False, []),
        (10, 0, None, []),
    ],
)
def test_schedule(hour, minute, open_day, expected):
    assert (
        StockTopicService._due_fund_flow_slots(NOW.replace(hour=hour, minute=minute), open_day)
        == expected
    )


def service(tmp_path):
    svc = StockTopicService(
        Settings(tushare_token="test", db_path=tmp_path / "db", archive_dir=tmp_path / "archive")
    )
    svc.database.initialize()
    svc.database.replace_calendar([dict(cal_date=DATE, is_open=1)])
    return svc


def test_full_formal_scope_retry_history_and_ui(tmp_path):
    svc = service(tmp_path)
    members = [
        dict(code=f"60000{i}.SH", name=str(i), active=1, leader_rank=i + 1) for i in range(7)
    ]
    svc.database.list_themes = Mock(
        return_value=[
            dict(id=1, status="confirmed", members=members),
            dict(id=2, status="confirmed", members=members[:2]),
            dict(id=3, status="watching", members=[dict(code="000001.SZ")]),
            dict(id=4, status="archived", members=[dict(code="000002.SZ")]),
        ]
    )
    calls = []
    missing = True

    def call(api, params):
        calls.append((api, params["ts_code"]))
        if api == "moneyflow_ths" and missing:
            return []
        return [row(params["ts_code"])]

    svc.provider.call = call
    first = svc.refresh_fund_flows("close", NOW)
    assert first["stock_count"] == 7
    assert len(calls) == 21
    hist = svc.database.flow_history("theme", "1", DATE)
    assert hist[0]["consensus"]["status"] == "insufficient"
    assert hist[0]["main_net"] == 7 * 1900000
    missing = False
    second = svc.refresh_fund_flows("close", NOW.replace(minute=25))
    assert second["status"] == "success"
    assert len(calls) == 28  # only the missing source is fetched again
    hist = svc.database.flow_history("theme", "1", DATE)
    assert len(hist) == 2
    assert hist[0]["consensus"]["status"] == "confirmed"
    earlier = svc.database.flow_history("theme", "1", DATE, hist[-1]["captured_at"])
    assert len(earlier) == 1
    themes = svc.database.list_themes()
    svc.database.attach_theme_fund_flows(themes, DATE, "close")
    assert all(m["fund_flow"]["daily"] for m in themes[0]["members"])
    assert svc.refresh_fund_flows("intraday", NOW)["status"] == "idle"


def test_migration_retains_legacy_without_dependency(tmp_path):
    db = Database(tmp_path / "old.db", tmp_path / "archive")
    with db.connect() as con:
        con.execute("CREATE TABLE level2_reports (report_json TEXT)")
        con.execute("INSERT INTO level2_reports VALUES (?)", ("old evidence",))
        con.execute("CREATE TABLE fund_flow_updates (report_json TEXT)")
    db.initialize()
    db.initialize()
    with db.connect() as con:
        assert (
            con.execute("SELECT report_json FROM legacy_order_flow_reports").fetchone()[0]
            == "old evidence"
        )
        assert not con.execute(
            "SELECT name FROM sqlite_master WHERE name='level2_reports'"
        ).fetchone()
    assert db.flow_history("stock", "600001.SH") == []


def test_intraday_rejects_empty_and_stale(monkeypatch):
    import json
    from io import BytesIO

    import stocktopic.fund_flow as module

    now = NOW.replace(hour=10, minute=15)

    def response(lines):
        return BytesIO(json.dumps(dict(rc=0, data=dict(code="600001", klines=lines))).encode())

    monkeypatch.setattr(module, "open_url", lambda *a, **k: response([]))
    with pytest.raises(ValueError):
        EastmoneyFlowClient().snapshot("600001.SH", now)
    monkeypatch.setattr(
        module, "open_url", lambda *a, **k: response(["2026-09-03 10:15,10,-2,-8,3,7"])
    )
    with pytest.raises(ValueError):
        EastmoneyFlowClient().snapshot("600001.SH", now)
    monkeypatch.setattr(
        module, "open_url", lambda *a, **k: response(["2026-09-04 10:15,10,-2,-8,3,7"])
    )
    assert EastmoneyFlowClient().snapshot("600001.SH", now)["main_net"] == 10


def test_intraday_batch_stops_after_failed_probe(tmp_path):
    svc = service(tmp_path)
    svc.database.list_themes = Mock(
        return_value=[
            dict(id=1, status="confirmed", members=[dict(code=f"60000{i}.SH") for i in range(7)])
        ]
    )
    svc.flow_provider.snapshot = Mock(side_effect=ValueError("empty data"))
    result = svc.refresh_fund_flows("intraday", NOW.replace(hour=10, minute=0))
    assert result["status"] == "degraded"
    assert svc.flow_provider.snapshot.call_count == 1
    assert svc.database.flow_history("theme", "1", DATE)[0]["main_net"] is None


def test_close_retry_freezes_original_members(tmp_path):
    svc = service(tmp_path)
    themes = [dict(id=1, status="confirmed", members=[dict(code="600001.SH", leader_rank=1)])]
    svc.database.list_themes = Mock(return_value=themes)
    svc.provider.call = lambda api, params: [row(params["ts_code"])]
    svc.refresh_fund_flows("close", NOW)
    themes[0]["members"].append(dict(code="600002.SH"))
    svc.refresh_fund_flows("close", NOW.replace(minute=25))
    latest = svc.database.flow_history("theme", "1", DATE)[0]
    assert latest["member_codes"] == ["600001.SH"]
    assert not svc.database.flow_history("stock", "600002.SH", DATE)
