from __future__ import annotations

from stocktopic.ai import OpenAIThemeExplainer
from stocktopic.config import Settings
from stocktopic.providers.tushare import TushareClient
from stocktopic.theme_graph import (
    install_graph_first_ai_clustering,
    structured_event_clusters,
)


def _events(count: int, tag: str = "固态电池") -> list[dict]:
    return [
        {
            "code": f"60000{index}.SH",
            "name": f"股票{index}",
            "market": "主板",
            "board_tag": "涨停",
            "status": "首板",
            "limit_up_time": f"09{35 + index:02d}00",
            "limit_reason": f"{tag}产业催化",
            "themes": [tag],
            "concept_tags": [
                {
                    "tag": tag,
                    "source": "tushare_kpl_concept_cons",
                    "confidence": 0.9,
                }
            ],
        }
        for index in range(count)
    ]


def test_two_stock_structured_graph_cluster_is_available_without_ai():
    clusters = structured_event_clusters(_events(2), minimum_members=2)
    assert len(clusters) == 1
    assert clusters[0]["tag"] == "固态电池"
    assert clusters[0]["cluster_method"] == "knowledge_graph"
    assert clusters[0]["touch_count"] == 2


def test_broad_parent_labels_never_create_local_early_or_formal_nodes():
    broad_labels = (
        "农业",
        "医药",
        "零售",
        "食品饮料",
        "消费电子",
        "地方国资",
        "AI应用",
        "金融概念",
    )
    for label in broad_labels:
        for count in (2, 3, 4):
            assert structured_event_clusters(_events(count, label), minimum_members=2) == []


def test_broad_parent_is_kept_as_context_for_a_specific_cluster():
    events = _events(3, "种业审定")
    for event in events:
        event["themes"] = ["农业", "种业审定"]
    clusters = structured_event_clusters(events, minimum_members=2)
    assert len(clusters) == 1
    assert clusters[0]["tag"] == "种业审定"
    assert clusters[0]["parent_tags"] == ["农业"]
    assert "农业" in clusters[0]["aliases"]


def test_three_stock_early_layer_never_calls_web_ai():
    install_graph_first_ai_clustering()
    client = OpenAIThemeExplainer("test-key", "test-model")
    called = False

    def should_not_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("2-3 stock early layer must not invoke web/AI")

    client._call_prompt = should_not_call  # type: ignore[method-assign]
    clusters = client.cluster_limit_events("20260902", _events(3), 2)
    assert called is False
    assert clusters[0]["tag"] == "固态电池"
    assert clusters[0]["touch_count"] == 3


def test_four_stock_layer_calls_ai_but_preserves_graph_anchor():
    install_graph_first_ai_clustering()
    client = OpenAIThemeExplainer("test-key", "test-model")
    calls = 0

    def fake_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        member_codes = [item["code"] for item in _events(4)]
        parsed = {
            "clusters": [
                {
                    "anchor_tag": "固态电池",
                    "canonical_name": "固态电池量产提速",
                    "common_logic": "量产进展推动设备与材料链共同受益",
                    "member_codes": member_codes,
                    "member_reasons": [
                        {"code": code, "reason": "结构化图谱成员且当日共同异动"}
                        for code in member_codes
                    ],
                    "aliases": ["全固态电池"],
                    "cluster_confidence": 91,
                    "catalysts": [],
                }
            ]
        }
        return {}, parsed, []

    client._call_prompt = fake_call  # type: ignore[method-assign]
    clusters = client.cluster_limit_events("20260902", _events(4), 2)
    assert calls == 1
    assert len(clusters) == 1
    assert clusters[0]["tag"] == "固态电池"
    assert clusters[0]["canonical_name"] == "固态电池量产提速"
    assert len(clusters[0]["member_codes"]) == 4


def test_four_stock_broad_parent_requires_specific_ai_logic():
    install_graph_first_ai_clustering()
    client = OpenAIThemeExplainer("test-key", "test-model")
    member_codes = [item["code"] for item in _events(4, "农业")]
    canonical_name = "农业"

    def fake_call(*args, **kwargs):
        return (
            {},
            {
                "clusters": [
                    {
                        "anchor_tag": "农业",
                        "canonical_name": canonical_name,
                        "common_logic": "新品种审定提速带动种业公司共同受益",
                        "member_codes": member_codes,
                        "member_reasons": [],
                        "aliases": [],
                        "cluster_confidence": 90,
                        "catalysts": [],
                    }
                ]
            },
            [],
        )

    client._call_prompt = fake_call  # type: ignore[method-assign]
    assert client.cluster_limit_events("20260902", _events(4, "农业"), 2) == []

    canonical_name = "转基因种业审定提速"
    clusters = client.cluster_limit_events("20260902", _events(4, "农业"), 2)
    assert len(clusters) == 1
    assert clusters[0]["tag"] == "转基因种业审定提速"
    assert clusters[0]["parent_tags"] == ["农业"]
    assert "农业" in clusters[0]["aliases"]


def test_existing_minimum_limit_env_is_formal_only(monkeypatch):
    monkeypatch.delenv("EARLY_LIMIT_TOUCHES", raising=False)
    monkeypatch.delenv("FORMAL_LIMIT_TOUCHES", raising=False)
    monkeypatch.setenv("MINIMUM_LIMIT_TOUCHES", "4")
    settings = Settings.from_env(require_secrets=False)
    assert settings.minimum_limit_touches == 2
    assert settings.formal_limit_touches == 4


class FakeGraphClient(TushareClient):
    def __init__(self):
        pass

    def call(self, api_name, params=None, fields=""):
        params = dict(params or {})
        offset = int(params.get("offset") or 0)
        if offset:
            return []
        if api_name == "kpl_concept_cons":
            return [
                {
                    "ts_code": "KPL001",
                    "name": "商业航天",
                    "con_name": "股票A",
                    "con_code": "600001.SH",
                    "trade_date": "20260902",
                    "desc": "商业航天",
                    "hot_num": 1,
                }
            ]
        if api_name == "dc_concept":
            return [
                {"theme_code": "DC001", "trade_date": "20260902", "name": "固态电池"}
            ]
        if api_name == "dc_concept_cons":
            return [
                {
                    "ts_code": "600002.SH",
                    "trade_date": "20260902",
                    "name": "股票B",
                    "theme_code": "DC001",
                    "industry_code": "I1",
                    "industry": "电池",
                    "reason": "固态电解质",
                    "hot_num": 2,
                }
            ]
        if api_name == "tdx_index":
            return [
                {
                    "ts_code": "TDX001",
                    "trade_date": "20260902",
                    "name": "机器人",
                    "idx_type": "概念板块",
                    "idx_count": 100,
                }
            ]
        if api_name == "tdx_member":
            return [
                {
                    "ts_code": "TDX001",
                    "trade_date": "20260902",
                    "con_code": "600003.SH",
                    "con_name": "股票C",
                }
            ]
        return []


def test_existing_concept_sync_returns_kpl_dc_and_tdx_graph_rows():
    client = FakeGraphClient()
    rows = client.kpl_concept_members("20260902")
    names = {row["name"] for row in rows}
    codes = {row["con_code"] for row in rows}
    assert {"商业航天", "固态电池", "机器人"} <= names
    assert {"600001.SH", "600002.SH", "600003.SH"} <= codes
