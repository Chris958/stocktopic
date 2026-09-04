from __future__ import annotations

from stocktopic.theme_policy import (
    _normalize_novelty_mode,
    policy_assess_for_admission,
    policy_structured_event_clusters,
)


def _event(code: str, *tags: str) -> dict:
    return {
        "code": code,
        "name": code,
        "board_tag": "涨停",
        "status": "首板",
        "themes": list(tags),
        "concept_tags": [],
        "pct_change": 10.0,
        "realtime_pct_change": 10.0,
        "limit_up_time": "100000",
    }


def test_pork_aliases_merge_into_one_formal_family_even_when_each_tag_is_subthreshold():
    events = [
        _event("000001.SZ", "猪肉"),
        _event("000002.SZ", "猪肉"),
        _event("000003.SZ", "生猪养殖"),
        _event("000004.SZ", "生猪养殖"),
        _event("000005.SZ", "猪周期"),
    ]

    clusters = policy_structured_event_clusters(events, minimum_members=4)
    pork = next(item for item in clusters if item["tag"] == "生猪养殖")

    assert pork["touch_count"] == 5
    assert set(pork["member_codes"]) == {
        "000001.SZ",
        "000002.SZ",
        "000003.SZ",
        "000004.SZ",
        "000005.SZ",
    }
    assert pork["cluster_method"] == "knowledge_graph_family"
    assert {"猪肉", "生猪养殖", "猪周期"}.issubset(set(pork["aliases"]))


def test_broad_agriculture_tag_still_cannot_create_a_theme():
    events = [_event(f"00000{index}.SZ", "农业") for index in range(1, 6)]
    assert policy_structured_event_clusters(events, minimum_members=4) == []


def test_generic_raising_tag_does_not_bridge_unrelated_livestock_themes():
    events = [
        _event("000001.SZ", "养殖", "猪肉"),
        _event("000002.SZ", "养殖", "猪肉"),
        _event("000003.SZ", "养殖", "白羽鸡"),
        _event("000004.SZ", "养殖", "白羽鸡"),
    ]
    clusters = policy_structured_event_clusters(events, minimum_members=4)
    assert all(item["touch_count"] < 4 for item in clusters) or clusters == []


def test_high_constituent_overlap_merges_non_hardcoded_nearby_nodes():
    events = [
        _event("000001.SZ", "低空经济", "低空经济设备"),
        _event("000002.SZ", "低空经济", "低空经济设备"),
        _event("000003.SZ", "低空经济"),
        _event("000004.SZ", "低空经济设备"),
    ]
    clusters = policy_structured_event_clusters(events, minimum_members=4)
    assert any(
        set(item["member_codes"])
        == {"000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"}
        and item["cluster_method"] == "knowledge_graph_family"
        for item in clusters
    )


class _FakeAdmissionClient:
    enabled = True

    def model_for_task(self, _task: str) -> str:
        return "test-model"

    def _call_prompt(self, prompt: str, **_kwargs):
        assert "旧赛道新周期" in prompt
        parsed = {
            "suggested_name": "猪价反弹与产能去化周期拐点",
            "novelty_mode": "new_cycle",
            "is_new_theme": False,
            "novelty_confidence": 82,
            "novelty_reason": "猪价从低位反弹且产能去化进入新阶段",
            "within_window_match_ids": [],
            "catalyst_summary": "猪价回升与供给收缩共振",
            "catalyst_confidence": 84,
            "expected_duration_days": 5,
            "duration_reason": "供需变化需要多个交易日定价",
            "leader_candidate_code": "000001.SZ",
            "leader_upside_scenario_pct": 32,
            "upside_scenario_reason": "价格和板块共振延续",
            "counter_evidence": ["猪价重新转弱"],
            "proposed_members": [],
            "catalysts": [],
        }
        return {"output": []}, parsed, []


def test_old_sector_new_cycle_is_admission_eligible_without_pretending_sector_is_new():
    theme = {
        "id": 1,
        "provisional_name": "生猪养殖待审",
        "shared_tag": "生猪养殖",
        "cluster_aliases": ["猪肉", "猪周期"],
        "members": [
            {
                "code": f"00000{index}.SZ",
                "name": f"股票{index}",
                "active": 1,
                "evidence": {"board_tag": "涨停"},
            }
            for index in range(1, 5)
        ],
    }

    result = policy_assess_for_admission(_FakeAdmissionClient(), theme, [], [])

    assert result["novelty_mode"] == "new_cycle"
    assert result["is_new_theme"] is True
    assert result["novelty_confidence"] == 82
    assert result["novelty_reason"].startswith("[旧赛道新周期]")


def test_unknown_novelty_mode_fails_closed_to_old_rotation():
    assert _normalize_novelty_mode("something-else") == "old_rotation"
