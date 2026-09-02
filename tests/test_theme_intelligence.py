from stocktopic.theme_graph import ThemeKnowledgeGraph
from stocktopic.theme_intelligence import (
    catalyst_quality,
    cluster_metrics,
    counter_evidence,
    discovery_stage,
    lifecycle_stage,
    market_regime,
)


def test_two_stock_cluster_can_enter_early_observation_when_resonance_is_strong():
    assert discovery_stage(2, 72, 0.8) == "early_observation"
    assert discovery_stage(2, 35, 0.8) == "noise"
    assert discovery_stage(4, 20, 0.2) == "formal_candidate"


def test_cluster_metrics_use_median_and_limit_time_density():
    metrics = cluster_metrics(
        {
            "touch_count": 3,
            "sealed_count": 3,
            "failed_count": 0,
            "growth_count": 0,
            "members": [
                {"pct_change": 10, "limit_up_time": "093500"},
                {"pct_change": 6, "limit_up_time": "093900"},
                {"pct_change": 4, "limit_up_time": "094300"},
            ],
        }
    )
    assert metrics["median_pct"] == 6
    assert metrics["synchronization_score"] >= 70
    assert metrics["breadth_score"] > 50


def test_graph_first_grouping_prefers_structured_theme_membership():
    graph = ThemeKnowledgeGraph()
    graph.ingest_kpl_concept(
        [
            {"ts_code": "000001.SZ", "con_code": "C1", "con_name": "商业航天"},
            {"ts_code": "000002.SZ", "con_code": "C1", "con_name": "商业航天"},
            {"ts_code": "000003.SZ", "con_code": "C1", "con_name": "商业航天"},
        ]
    )
    clusters = graph.common_nodes(["000001.SZ", "000002.SZ", "000003.SZ"], 2)
    assert clusters[0]["cluster_method"] == "knowledge_graph"
    assert set(clusters[0]["member_codes"]) == {"000001.SZ", "000002.SZ", "000003.SZ"}


def test_catalyst_quality_rewards_official_new_high_impact_evidence():
    score = catalyst_quality(
        [
            {
                "source_tier": "central_policy",
                "novelty_score": 90,
                "impact_score": 85,
                "duration_score": 80,
            }
        ]
    )
    assert score["truth"] == 100
    assert score["score"] >= 85


def test_counter_evidence_catches_single_leader_false_strength():
    result = counter_evidence(
        median_pct=0.5,
        negative_ratio=0.3,
        failed_count=2,
        concentration=0.6,
        divergence=True,
        market_regime="退潮",
    )
    assert result["score"] >= 70
    assert "上涨过度集中于少数核心股" in result["items"]


def test_lifecycle_covers_start_acceleration_climax_and_ebb():
    assert lifecycle_stage(
        day_number=1,
        heat=45,
        entry_risk=20,
        divergence=False,
        negative_ratio=0.05,
        synchronization_score=75,
        median_pct=2,
    ) == "启动"
    assert lifecycle_stage(
        day_number=2,
        heat=70,
        entry_risk=25,
        divergence=False,
        negative_ratio=0.05,
        synchronization_score=75,
        median_pct=5,
    ) == "加速"
    assert lifecycle_stage(
        day_number=3,
        heat=90,
        entry_risk=35,
        divergence=False,
        negative_ratio=0.05,
        synchronization_score=80,
        median_pct=7,
    ) == "高潮"
    assert lifecycle_stage(
        day_number=4,
        heat=55,
        entry_risk=82,
        divergence=False,
        negative_ratio=0.4,
        synchronization_score=20,
        median_pct=-1,
    ) == "退潮"


def test_market_regime_model_distinguishes_bullish_and_ice_conditions():
    bullish = market_regime(
        {
            "limit_up_count": 90,
            "limit_down_count": 2,
            "seal_rate": 85,
            "promotion_rate": 65,
            "yesterday_limit_return": 3.5,
            "failed_rate": 15,
        }
    )
    ice = market_regime(
        {
            "limit_up_count": 15,
            "limit_down_count": 30,
            "seal_rate": 35,
            "promotion_rate": 15,
            "yesterday_limit_return": -4,
            "failed_rate": 60,
        }
    )
    assert bullish["label"] in {"主升", "修复"}
    assert ice["label"] in {"退潮", "冰点"}
    assert bullish["score"] > ice["score"]
