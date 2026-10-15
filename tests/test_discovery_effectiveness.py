import json
import threading
from datetime import timedelta

from test_event_bridge import (
    DATE,
    candidates,
    client_for,
    events,
    make_service,
    media,
    now,
)

from stocktopic.discovery_ai_policy import MAX_NEGATIVE_REVIEWS_PER_INPUT
from stocktopic.event_bridge import EventBridgeDetector, verify_event_bridge
from stocktopic.theme_graph import structured_event_clusters


def age_cache(service, minutes=31):
    signature = service.database.get_metadata(f"discovery_trace_latest:{DATE}")
    with service.database.connect() as con:
        con.execute("UPDATE semantic_cluster_runs SET created_at=? WHERE input_signature=?",
                    ((now() - timedelta(minutes=minutes)).isoformat(), signature))
    return signature


def test_complementary_content_roles_can_reach_ai_without_shared_words():
    rows = events(("出版", 2, "网络文学IP改编授权"), ("影视院线", 2, "AIGC影视制作平台"))
    candidate = candidates(rows)[0]
    assert candidate["verification_route"] == "semantic_review"
    assert candidate["reasons"]["text_similarity"] == 0
    assert candidate["bridge_score"] >= 60
    assert candidate["review_routes"] == ["complementary_business_roles"]
    client, calls = client_for(rows)
    result, _ = verify_event_bridge(client, candidate, rows)
    assert len(result["member_codes"]) == 4
    assert len(calls) == 1
    assert calls[0][1]["reasoning_effort"] == "medium"
    for row in rows[:2]:
        row["limit_reason"] = "网文IP授权"  # Specific roles can be concise.
    assert candidates(rows)[0]["review_routes"] == ["complementary_business_roles"]


def test_semantic_research_can_reject_and_cannot_expand_stock_pool():
    rows = events(("出版", 2, "网络文学IP改编授权"), ("影视院线", 2, "AIGC影视制作平台"))
    candidate = candidates(rows)[0]
    reject, _ = client_for(rows, "reject")
    assert verify_event_bridge(reject, candidate, rows)[0] is None
    expanded, _ = client_for(rows, codes=[r["code"] for r in rows] + ["999999.SH"])
    assert verify_event_bridge(expanded, candidate, rows)[0] is None


def test_four_plus_two_can_bridge_and_overlap_counts_unique_stocks():
    rows = events(("出版", 4, "AI影视内容生产新模型"), ("影视院线", 2, "AI影视内容生产新模型"))
    assert len(candidates(rows)[0]["member_codes"]) == 6
    rows[0]["themes"].append("影视院线")
    assert len(candidates(rows)[0]["member_codes"]) == 6
    assert len(candidates(rows)[0]["node_members"]["影视院线"]) == 3


def test_one_node_member_and_fewer_than_four_unique_still_never_call_ai():
    rows = events(("出版", 3, "AI影视内容生产新模型"), ("影视院线", 1, "AI影视内容生产新模型"))
    client, calls = client_for(rows)
    client.cluster_limit_events(DATE, rows)
    assert calls == []
    rows = events(("出版", 3, "AI影视内容生产新模型"))
    for row in rows:
        row["themes"].append("影视院线")
    assert candidates(rows) == []


def test_maximum_fifteen_members_and_sixty_minute_relay_are_bounded():
    assert candidates(events(("出版", 8, "AI影视内容生产新模型"),
                             ("影视院线", 7, "AI影视内容生产新模型")))
    assert not candidates(events(("出版", 8, "AI影视内容生产新模型"),
                                 ("影视院线", 8, "AI影视内容生产新模型")))
    rows = events(("出版", 2, "AI影视内容生产新模型"), ("影视院线", 2, "AI影视内容生产新模型"))
    for row in rows[:2]:
        row["limit_up_time"] = "093500"
    for row in rows[2:]:
        row["limit_up_time"] = "103400"
    assert candidates(rows)[0]["review_routes"] == ["bounded_relay_review"]
    for row in rows[2:]:
        row["limit_up_time"] = "103600"
    assert not candidates(rows)


def test_unknown_time_requires_strong_business_evidence_and_is_never_fabricated():
    rows = media()
    for row in rows:
        row["limit_time_basis"] = "first_observed_5m"
    candidate = candidates(rows)[0]
    assert candidate["synchronization_score"] is None
    assert "time_sync" not in candidate["reasons"]["available_weights"]
    assert candidate["review_routes"] == ["business_evidence_unknown_time"]
    for row in rows:
        row["limit_reason"] = "行业走强"
    assert candidates(rows) == []


def test_aged_negative_cache_rechecks_same_fixed_candidates_and_can_recover(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    service.explainer, first_calls = client_for(rows, "reject")
    assert service.discover_trade_date(DATE, now()) == []
    assert len(first_calls) == 1
    signature = age_cache(service)
    service.explainer, recovered_calls = client_for(rows)
    ids = service.discover_trade_date(DATE, now())
    assert len(ids) == 1 and len(recovered_calls) == 1
    candidate = service.database.get_theme(ids[0])
    assert candidate["admission_status"] == "awaiting_ai"
    assert {m["code"] for m in candidate["members"]} == {r["code"] for r in rows}
    trace = json.loads(service.database.get_metadata(f"discovery_trace:{DATE}:{signature}"))
    assert trace["negative_cache_recheck"]["count"] == 1
    history = service.database.get_metadata(f"discovery_review_history:{DATE}:{signature}:1")
    assert json.loads(history)["bridges"][0]["ai_bridge_decision"] == "reject"
    assert service.database.latest_run("discovery_ai_recheck")["status"] == "success"


def test_fresh_negative_cache_and_historical_replay_do_not_add_calls(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    service.explainer, calls = client_for(rows, "reject")
    service.discover_trade_date(DATE, now())
    age_cache(service, minutes=29)
    service.discover_trade_date(DATE, now())
    assert len(calls) == 1
    age_cache(service)
    service.discover_trade_date(DATE, now() + timedelta(days=1))
    assert len(calls) == 1


def test_negative_recheck_budget_and_below_four_gate(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    service.explainer, calls = client_for(rows, "reject")
    service.discover_trade_date(DATE, now())
    signature = age_cache(service)
    key = f"discovery_negative_reviews:{DATE}:{signature}"
    service.database.set_metadata(key, str(MAX_NEGATIVE_REVIEWS_PER_INPUT))
    service.discover_trade_date(DATE, now())
    assert len(calls) == 1
    rows[:] = rows[:3]
    service.discover_trade_date(DATE, now())
    age_cache(service)
    service.discover_trade_date(DATE, now())
    assert len(calls) == 1


def test_successful_formal_cache_is_not_researched_on_a_timer(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    service.explainer, calls = client_for(rows)
    first_ids = service.discover_trade_date(DATE, now())
    age_cache(service)
    assert service.discover_trade_date(DATE, now()) == first_ids
    assert len(calls) == 1


def test_local_observations_are_saved_before_any_bridge_ai(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    client, _ = client_for(rows)
    original = client._call_prompt
    checks = []

    def verify(*args, **kwargs):
        themes = service.database.list_themes()
        assert len(themes) == 2
        assert all(t["admission_status"] == "early_observation" for t in themes)
        assert len(service.database.recent_alerts(10)) == 2
        checks.append(True)
        return original(*args, **kwargs)

    client._call_prompt = verify
    service.explainer = client
    assert len(service.discover_trade_date(DATE, now())) == 1
    assert checks == [True]


def test_early_publish_does_not_downgrade_an_existing_formal_candidate(tmp_path):
    rows = media()
    service = make_service(tmp_path, rows)
    service.explainer, _ = client_for(rows)
    ids = service.discover_trade_date(DATE, now())
    fingerprint = service.database.get_theme(ids[0])["fingerprint"]
    original = service.database.upsert_candidate

    def check(**kwargs):
        if kwargs["fingerprint"] == fingerprint:
            assert kwargs["admission_status"] == "awaiting_ai"
        return original(**kwargs)

    service.database.upsert_candidate = check
    assert service.discover_trade_date(DATE, now()) == ids


def test_partial_success_rechecks_only_rejected_anchors_and_retains_accepted_pool(tmp_path):
    rows = events(("固态电池", 4, "固态电池量产进展"), ("AI影视", 4, "AI影视内容生产新模型"))
    service = make_service(tmp_path, rows)
    client, _ = client_for(rows)
    requests = []

    def verify(prompt, **_kwargs):
        payload = json.loads(prompt.split("图谱候选：", 1)[1].split("\n输入强势股票", 1)[0])
        anchors = {c["anchor_tag"] for c in payload}
        requests.append(anchors)
        tag = "固态电池" if len(requests) == 1 else "AI影视"
        codes = [row["code"] for row in rows if row["themes"][0] == tag]
        return {}, {"clusters": [{"anchor_tag": tag, "canonical_name": tag + "新催化",
                                 "common_logic": "具体事件受益", "member_codes": codes}],
                    "rejected_nodes": [{"anchor_tag": "AI影视", "reason": "证据待补充"}]
                    if len(requests) == 1 else []}, []

    client._call_prompt = verify
    service.explainer = client
    first_ids = service.discover_trade_date(DATE, now())
    assert len(first_ids) == 1
    signature = age_cache(service)
    ids = service.discover_trade_date(DATE, now())
    assert len(ids) == 2
    assert requests == [{"固态电池", "AI影视"}, {"AI影视"}]
    trace = json.loads(service.database.get_metadata(f"discovery_trace:{DATE}:{signature}"))
    assert trace["retained_verified_clusters"][0]["tag"] == "固态电池"
    age_cache(service)
    service.discover_trade_date(DATE, now())
    assert len(requests) == 2


def test_bridge_research_concurrency_is_bounded_and_trace_order_stays_deterministic():
    rows = events(*[(f"节点{i}", 2, "AI影视内容生产新模型") for i in range(5)])
    client, _ = client_for(rows)
    lock, barrier = threading.Lock(), threading.Barrier(3)
    active = peak = count = 0

    def verify(prompt, **_kwargs):
        nonlocal active, peak, count
        with lock:
            index = count
            count += 1
            active += 1
            peak = max(peak, active)
        try:
            if index < 3:
                barrier.wait(timeout=5)
            payload = json.loads(prompt.split("输入：", 1)[1])
            assert 4 <= len(payload["member_codes"]) <= 15
            return {}, {"decision": "reject"}, []
        finally:
            with lock:
                active -= 1

    client._call_prompt = verify
    client.cluster_limit_events(DATE, rows)
    assert peak == 3 and count == 5
    expected = sorted(candidates(rows), key=lambda c: (
        -c["bridge_score"], -c["touch_count"], tuple(c["nodes"])))
    assert [d["nodes"] for d in client.last_discovery_trace["bridges"]] == [
        c["nodes"] for c in expected]


def test_unknown_times_do_not_inflate_known_time_count_through_overlap():
    rows = media()
    rows[0]["themes"].append("影视院线")
    for row in rows[1:]:
        row["limit_time_basis"] = "first_observed_5m"
    detector = EventBridgeDetector()
    candidate = detector.detect(DATE, rows, structured_event_clusters(rows))[0]
    assert candidate["reasons"]["time_sync"] is None
