import json

import pytest

from stocktopic.ai import OpenAIThemeExplainer
from stocktopic.ai_validation import ConfidenceScaleError, confidence_scores
from stocktopic.board_status import consecutive_boards, parse_board_status
from stocktopic.db import _board_height as database_height
from stocktopic.scoring import _board_height as score_height
from stocktopic.service import _parse_board_height
from stocktopic.theme_graph import _verify_formal_graph_clusters
from stocktopic.theme_intelligence import _board_height as structure_height
from stocktopic.theme_policy import install_theme_policy, policy_assess_for_admission


@pytest.mark.parametrize(
    "status,height",
    [
        ("首板", 1),
        ("5连板", 5),
        ("2天2板", 2),
        ("4天3板", 0),
        ("6天5板", 0),
        (None, 0),
        ("3", 3),
        ("5天6板", 0),
    ],
)
def test_board_height_parsers_agree_without_inventing_consecutive_boards(status, height):
    for parser in (
        consecutive_boards,
        database_height,
        score_height,
        _parse_board_height,
        structure_height,
    ):
        assert parser(status) == height
    if status == "4天3板":
        assert parse_board_status(status)["window_days"] == 4
        assert parse_board_status(status)["limit_count"] == 3


def test_confidence_units_are_explicit_and_ambiguous_fractions_are_not_business_rejections():
    assert confidence_scores(
        {"confidence_scale": "0_1", "novelty_confidence": 0.96, "catalyst_confidence": 0.93},
        ("novelty_confidence", "catalyst_confidence"),
    ) == {
        "novelty_confidence": 96,
        "catalyst_confidence": 93,
    }
    assert confidence_scores({"confidence_scale": "0_100", "score": 0.93}, ("score",)) == {
        "score": 0.93,
    }  # Explicitly low percent confidence must not be silently multiplied.
    with pytest.raises(ConfidenceScaleError):
        confidence_scores({"score": 0.93}, ("score",))
    for value in (float("nan"), float("inf"), -1, 101, True):
        with pytest.raises(ConfidenceScaleError):
            confidence_scores({"score": value}, ("score",))
    with pytest.raises(ConfidenceScaleError):
        confidence_scores({"confidence_scale": []}, ("score",))


def event(i, tags):
    return {
        "code": f"{600000 + i:06}.SH",
        "name": f"合成股票{i}",
        "themes": tags,
        "board_tag": "涨停",
        "status": "首板",
        "pct_change": 10,
        "limit_up_time": "093500",
    }


def accepted(anchor, codes):
    return {
        "anchor_tag": anchor,
        "canonical_name": "合成具体事件",
        "common_logic": "合成共同业务链",
        "confidence_scale": "0_100",
        "cluster_confidence": 90,
        "member_codes": codes,
        "member_reasons": [{"code": code, "reason": "合成关系"} for code in codes],
    }


def payload(prompt):
    return json.loads(prompt.split("图谱候选：", 1)[1].split("\n输入强势股票：", 1)[0])


def test_same_named_family_cannot_be_overwritten_by_its_smaller_child():
    install_theme_policy()
    rows = [event(i, ["机器人概念", "人形机器人"] if i < 4 else ["机器人概念"]) for i in range(6)]
    client = OpenAIThemeExplainer("test", "test")
    calls = []

    def respond(prompt, **kwargs):
        nodes = payload(prompt)
        assert len([c for c in nodes if c["anchor_tag"] == "人形机器人"]) == 1
        node = next(c for c in nodes if c["anchor_tag"] == "人形机器人")
        assert len(node["member_codes"]) == 6
        calls.append(nodes)
        return {}, {"clusters": [accepted("人形机器人", node["member_codes"])]}, []

    client._call_prompt = respond
    result = _verify_formal_graph_clusters(client, "20261016", rows)
    assert len(calls) == 1 and len(result[0]["member_codes"]) == 6


def test_node_twenty_one_is_reviewed_in_another_bounded_batch():
    rows = [event(i, [f"合成节点{j:02}" for j in range(20)]) for i in range(5)]
    rows += [event(i, ["机器人概念"]) for i in range(5, 9)]
    client = OpenAIThemeExplainer("test", "test")
    calls = []

    def respond(prompt, **kwargs):
        nodes = payload(prompt)
        calls.append(nodes)
        assert len(nodes) <= 20
        result = [
            accepted(c["anchor_tag"], c["member_codes"])
            for c in nodes
            if c["anchor_tag"] == "机器人概念"
        ]
        return {}, {"clusters": result}, []

    client._call_prompt = respond
    result = _verify_formal_graph_clusters(client, "20261016", rows)
    assert len(calls) == 2
    assert result[0]["tag"] == "机器人概念"
    assert set(result[0]["member_codes"]) == {r["code"] for r in rows[5:]}


def test_admission_confidence_scale_failure_does_not_become_a_low_confidence_score():
    client = OpenAIThemeExplainer("test", "test")
    client._call_prompt = lambda *args, **kwargs: (
        {},
        {
            "suggested_name": "具体新事件",
            "novelty_mode": "new_catalyst",
            "is_new_theme": True,
            "novelty_confidence": 0.96,
            "catalyst_confidence": 0.93,
            "expected_duration_days": 4,
            "leader_candidate_code": "600000.SH",
            "leader_upside_scenario_pct": 30,
        },
        [],
    )
    with pytest.raises(ConfidenceScaleError):
        policy_assess_for_admission(client, {"shared_tag": "人形机器人", "members": []}, [], [])
