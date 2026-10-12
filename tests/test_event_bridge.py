from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stocktopic.ai import OpenAIThemeExplainer
from stocktopic.config import Settings
from stocktopic.event_bridge import EventBridgeDetector, minutes, verify_event_bridge
from stocktopic.service import StockTopicService, _semantic_event_signature
from stocktopic.theme_graph import install_graph_first_ai_clustering, structured_event_clusters
from stocktopic.theme_policy import policy_structured_event_clusters

DATE = "20261009"


def events(*groups):
    result = []
    for tag, count, reason in groups:
        for _ in range(count):
            index = len(result)
            result.append(
                {
                    "code": f"600{index:03d}.SH",
                    "name": f"股票{index}",
                    "themes": [tag, "文化传媒"] if tag != "农业" else [tag],
                    "board_tag": "涨停",
                    "limit_up_time": f"09{35 + index:02d}00",
                    "limit_reason": reason,
                    "concept_tags": [],
                }
            )
    return result


def media(short=False):
    groups = [("出版", 3, "AI影视内容生产新模型"), ("影视院线", 3, "AI影视内容生产新模型")]
    if short:
        groups.append(("短剧", 2, "AI影视内容生产新模型"))
    return events(*groups)


def candidates(rows):
    return EventBridgeDetector().detect(DATE, rows, structured_event_clusters(rows))


def client_for(rows, decision="merge", accepted=None, codes=None):
    install_graph_first_ai_clustering()
    client = OpenAIThemeExplainer("test-key", "gpt-5.6-sol")
    calls = []
    nodes = accepted or ["出版", "影视院线"]
    selected = (
        codes if codes is not None else [row["code"] for row in rows if row["themes"][0] in nodes]
    )

    def call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return (
            {},
            {
                "decision": decision,
                "accepted_nodes": nodes,
                "member_codes": selected,
                "canonical_name": "AI影视内容生产催化",
                "common_logic": "新模型推动内容生产",
                "confidence": 85,
                "member_reasons": [
                    {"code": code, "reason": "输入业务与新模型相关"} for code in selected
                ],
                "catalysts": [
                    {
                        "title": "新模型发布",
                        "summary": "内容生产能力提升",
                        "url": "https://example.com/model",
                        "source_kind": "official",
                    }
                ],
            },
            [{"url": "https://example.com/model"}],
        )

    client._call_prompt = call
    return client, calls


def test_a_cross_node_merge_is_formal_candidate_and_keeps_early_nodes(tmp_path):
    rows = media()
    candidate = candidates(rows)[0]
    assert candidate["bridge_score"] >= 60
    assert candidate["anchor_tag"] == "出版"
    service = make_service(tmp_path, rows)
    service.explainer, calls = client_for(rows)
    ids = service.discover_trade_date(DATE, now())
    assert len(calls) == 1 and len(ids) == 1
    theme = service.database.get_theme(ids[0])
    assert theme["cluster_method"] == "event_bridge"
    assert theme["admission_status"] == "awaiting_ai"  # Admission is not bypassed.
    assert len(theme["members"]) == 6
    signature = service.database.get_metadata(f"discovery_trace_latest:{DATE}")
    trace = json.loads(service.database.get_metadata(f"discovery_trace:{DATE}:{signature}"))
    assert trace["strong_stock_count"] == 6
    assert trace["bridges"][0]["ai_bridge_decision"] == "merge"


def test_b_time_alone_never_combines_unrelated_nodes():
    rows = events(
        ("出版", 2, "教材出版改革"), ("影视院线", 2, "春节电影票房"), ("农业", 2, "粮食价格上涨")
    )
    client, calls = client_for(rows)
    assert candidates(rows) == []
    clusters = client.cluster_limit_events(DATE, rows)
    assert calls == []
    assert all(len(cluster["member_codes"]) < 4 for cluster in clusters)


@pytest.mark.parametrize("tag", ["文化传媒", "传媒"])
def test_c_broad_parent_only_never_becomes_formal(tag):
    rows = events((tag, 6, "行业走强"))
    client, calls = client_for(rows)
    assert client.cluster_limit_events(DATE, rows) == []
    assert calls == []


def test_d_three_stocks_no_ai_early_watch(tmp_path):
    rows = events(("出版", 3, "出版改革"))
    service = make_service(tmp_path, rows)
    service.explainer, calls = client_for(rows)
    assert service.discover_trade_date(DATE, now()) == []
    assert calls == []
    assert service.database.list_themes()[0]["admission_status"] == "early_observation"


def test_e_ai_reject_preserves_both_early_clusters():
    rows = media()
    client, calls = client_for(rows, "reject")
    result = client.cluster_limit_events(DATE, rows)
    assert len(calls) == 1
    assert {cluster["tag"] for cluster in result} == {"出版", "影视院线"}
    assert not any(cluster["cluster_method"] == "event_bridge" for cluster in result)
    assert client.last_discovery_trace["bridges"][0]["final_reject_reason"]


def test_f_partial_merge_accepted_nodes_only():
    rows = media(short=True)
    client, _ = client_for(rows, "partial_merge")
    result, trace = verify_event_bridge(client, candidates(rows)[0], rows)
    assert len(result["member_codes"]) == 6
    assert not set(result["member_codes"]) & {row["code"] for row in rows[6:]}
    assert trace["accepted_nodes"] == ["出版", "影视院线"]


def test_g_partial_under_four_rejected():
    rows = media()
    client, _ = client_for(rows, "partial_merge", codes=[row["code"] for row in rows[:3]])
    result, trace = verify_event_bridge(client, candidates(rows)[0], rows)
    assert result is None
    assert trace["final_reject_reason"] == "partial_below_four_or_outside_accepted_nodes"


def test_h_pork_family_still_merges():
    rows = events(("猪肉", 2, "猪价上涨"), ("生猪", 2, "猪价上涨"), ("猪周期", 1, "猪价上涨"))
    family = policy_structured_event_clusters(rows, minimum_members=4)
    assert family[0]["tag"] == "生猪养殖"
    assert len(family[0]["member_codes"]) == 5


def test_rejects_expanded_members():
    rows = media()
    client, _ = client_for(rows, codes=[row["code"] for row in rows] + ["999999.SH"])
    assert verify_event_bridge(client, candidates(rows)[0], rows)[0] is None


def test_verifier_failure_retains_early_observation():
    rows = media()
    client, _ = client_for(rows)

    def fail(*args, **kwargs):
        raise TimeoutError("synthetic")

    client._call_prompt = fail
    result = client.cluster_limit_events(DATE, rows)
    assert len(result) == 2
    assert client.last_discovery_trace["bridges"][0]["error_type"] == "TimeoutError"


def test_old_empty_cache_still_keeps_early_clusters(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    service.explainer, calls = client_for(rows)
    service.database.save_semantic_cluster_run(
        DATE,
        _semantic_event_signature(rows),
        "gpt-5.6-sol",
        "success",
        [],
    )
    assert service.discover_trade_date(DATE, now()) == []
    assert calls == []
    assert len(service.database.list_themes()) == 2


def test_anchor_and_theme_identity_ignore_canonical_name_changes(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    client, _ = client_for(rows)
    cluster, _ = verify_event_bridge(client, candidates(rows)[0], rows)
    cluster.update({"members": rows, "touch_count": 6, "sealed_count": 6, "failed_count": 0})
    first = service.discovery.discover_for_date(DATE, now(), [cluster])
    cluster["canonical_name"] = "另一个具体事件名称"
    second = service.discovery.discover_for_date(DATE, now(), [cluster])
    assert first == second
    assert len(service.database.list_themes()) == 1


def test_missing_time_and_reason_do_not_fabricate_evidence():
    rows = media()
    for row in rows:
        row.pop("limit_up_time")
        row.pop("limit_reason")
    detector = EventBridgeDetector()
    assert detector.detect(DATE, rows, structured_event_clusters(rows)) == []
    assert detector.trace[0]["time_sync"] is None
    assert "time_sync" not in detector.trace[0]["available_weights"]


def test_maximum_nodes_members_and_deterministic_input_order():
    rows = events(*[(f"具体节点{i}", 3, "AI影视内容生产新模型") for i in range(5)])
    result = candidates(rows)
    assert result and all(len(c["nodes"]) <= 4 and len(c["member_codes"]) <= 12 for c in result)
    assert [(c["anchor_tag"], c["nodes"]) for c in result] == [
        (c["anchor_tag"], c["nodes"]) for c in candidates(list(reversed(rows)))
    ]


def test_clock_parsing():
    assert minutes("09:35") == minutes("093500") == 575
    assert minutes("invalid") is None
    assert minutes("996000") is None


def make_service(tmp_path, rows):
    service = StockTopicService(
        Settings(
            tushare_token="test",
            db_path=tmp_path / "test.sqlite3",
            archive_dir=tmp_path / "archive",
            admin_password="test",
            app_api_token="test",
        )
    )
    service.database.initialize()
    service.database.upsert_stocks(
        [{"ts_code": row["code"], "name": row["name"], "market": "主板"} for row in rows]
    )
    service.database.limit_touch_events = lambda _: rows
    return service


def now():
    return datetime(2026, 10, 9, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_b_matching_media_pair_does_not_absorb_unrelated_concrete_agriculture():
    rows = media() + events(("种业", 2, "粮食价格上涨"))
    # Assign disjoint stock identifiers to the extra group.
    for index, row in enumerate(rows):
        row["code"] = f"600{index:03d}.SH"
    result = candidates(rows)
    assert len(result) == 1
    assert set(result[0]["nodes"]) == {"出版", "影视院线"}


def test_ai_timeout_is_retryable_and_early_nodes_survive_cache(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    client, _ = client_for(rows)

    def fail(*args, **kwargs):
        raise TimeoutError("synthetic")

    client._call_prompt = fail
    service.explainer = client
    assert service.discover_trade_date(DATE, now()) == []
    signature = service.database.get_metadata(f"discovery_trace_latest:{DATE}")
    assert service.database.semantic_cluster_run(DATE, signature)["status"] == "failed"
    assert len(service.database.list_themes()) == 2
    assert service.discover_trade_date(DATE, now()) == []
    assert len(service.database.list_themes()) == 2


def test_bridge_cache_tracks_sync_inputs():
    rows = media()
    signature = _semantic_event_signature(rows)
    changed_time = [{**row, "limit_up_time": "140000"} for row in rows]
    changed_board = [{**row, "board_tag": "炸板"} for row in rows]
    assert signature != _semantic_event_signature(changed_time)
    assert signature != _semantic_event_signature(changed_board)


def test_partial_excluded_anchor_uses_earliest_accepted_node():
    rows = media(short=True)
    client, _ = client_for(rows, "partial_merge", accepted=["影视院线", "短剧"])
    result, trace = verify_event_bridge(client, candidates(rows)[0], rows)
    assert result["tag"] == "影视院线"
    assert "出版" not in result["aliases"]
    assert trace["final_anchor_tag"] == "影视院线"


def test_verifier_rejects_accepted_node_with_only_one_member():
    rows = media()
    selected = [row["code"] for row in rows[:4]]
    client, _ = client_for(rows, "partial_merge", codes=selected)
    result, trace = verify_event_bridge(client, candidates(rows)[0], rows)
    assert result is None and trace["final_reject_reason"] == "accepted_node_below_two"


def test_broad_only_rejection_trace_and_ai_disabled_trace(tmp_path):
    rows = events(("文化传媒", 6, "行业走强"))
    service = make_service(tmp_path, rows)
    service.discover_trade_date(DATE, now())
    signature = service.database.get_metadata(f"discovery_trace_latest:{DATE}")
    trace = json.loads(service.database.get_metadata(f"discovery_trace:{DATE}:{signature}"))
    assert trace["local_clusters"][0]["touch_count"] == 6
    assert trace["bridge_screening"][0]["excluded_reason"] == "broad_parent_context_only"


def test_no_ai_for_overlap_or_insufficient_global_members():
    rows = events(("出版", 3, "AI影视内容生产新模型"))
    for row in rows:
        row["themes"].append("影视院线")
    client, calls = client_for(rows)
    result = client.cluster_limit_events(DATE, rows)
    assert calls == []
    assert all(len(c["member_codes"]) == 3 for c in result)


def test_verifier_never_calls_ai_below_four():
    rows = media()
    candidate = candidates(rows)[0]
    candidate["member_codes"] = candidate["member_codes"][:3]
    client, calls = client_for(rows)
    result, trace = verify_event_bridge(client, candidate, rows)
    assert result is None and calls == []
    assert trace["final_reject_reason"] == "below_four_members"


def test_unsearched_catalyst_url_cannot_validate_bridge():
    rows = media()
    client, _ = client_for(rows)
    original = client._call_prompt

    def call(*args, **kwargs):
        raw, parsed, sources = original(*args, **kwargs)
        parsed["catalysts"][0]["url"] = "https://example.com/invented"
        return raw, parsed, sources

    client._call_prompt = call
    assert verify_event_bridge(client, candidates(rows)[0], rows)[0] is None


def test_single_node_formal_rejection_does_not_erase_other_early_node():
    rows = events(("出版", 3, "教材出版改革"), ("固态电池", 4, "电池量产"))
    client, calls = client_for(rows)

    def reject(prompt, **kwargs):
        calls.append(prompt)
        return {}, {"clusters": []}, []

    client._call_prompt = reject
    result = client.cluster_limit_events(DATE, rows)
    assert len(calls) == 1
    assert len(result) == 1 and result[0]["tag"] == "出版"


def test_overlapping_hypotheses_never_overwrite_same_anchor():
    rows = events(*[(f"具体节点{i}", 3, "AI影视内容生产新模型") for i in range(5)])
    install_graph_first_ai_clustering()
    client = OpenAIThemeExplainer("test-key", "gpt-5.6-sol")

    def call(prompt, **kwargs):
        payload = json.loads(prompt.split("输入：", 1)[1])
        codes = payload["member_codes"]
        return {}, {
            "decision": "merge", "accepted_nodes": payload["nodes"], "member_codes": codes,
            "canonical_name": "AI影视生产新模型", "common_logic": "新模型共同受益",
            "confidence": 85,
            "member_reasons": [{"code": code, "reason": "共同受益"} for code in codes],
            "catalysts": [{"title": "发布", "summary": "新模型",
                           "url": "https://example.com/model"}],
        }, [{"url": "https://example.com/model"}]

    client._call_prompt = call
    formal = [c for c in client.cluster_limit_events(DATE, rows)
              if c["cluster_method"] == "event_bridge"]
    assert len(formal) == len({c["tag"] for c in formal})
    assert any(trace.get("final") == "superseded_same_anchor"
               for trace in client.last_discovery_trace["bridges"])
