"""Preserve direction rules while bounding repeated classification work."""
import re

from stocktopic import theme_directions as directions


def original_label(value):
    def compact(value):
        return re.sub(r"\s|(?:概念|板块)$", "", str(value or "")).casefold()
    text = compact(value)
    return next((name for name, aliases in directions.DIRECTION_ALIASES.items()
                 if text in {compact(a) for a in aliases}), None)


def test_alias_index_preserves_original_normalization_and_unknown_values():
    labels = [alias for aliases in directions.DIRECTION_ALIASES.values() for alias in aliases]
    inputs = [None, "", 0, 123, [], {}, ["机器人"], {"name": "机器人"},
              "轴承", "特斯拉订单", "未知方向", "机器人板块概念"]
    for label in labels:
        inputs.extend([label, label + "概念", label + "板块", label.upper(),
                       " " + label + " ", "\t" + label + "\n"])
    for value in inputs:
        assert directions.label_direction(value) == original_label(value)


def test_repeated_stock_tags_do_not_renormalize_the_alias_taxonomy(monkeypatch):
    directions._compact.cache_clear()
    original = re.sub
    calls = []

    def count(pattern, replacement, value, *args, **kwargs):
        calls.append(value)
        return original(pattern, replacement, value, *args, **kwargs)
    monkeypatch.setattr(directions.re, "sub", count)
    for _ in range(1000):
        assert directions.label_direction("人形机器人概念") == "机器人"
        assert directions.label_direction("未知细分节点") is None
    assert calls == ["人形机器人概念", "未知细分节点"]


def test_label_cache_has_bounded_memory():
    directions._compact.cache_clear()
    for i in range(8300):
        assert directions.label_direction(f"未配置方向{i}") is None
    assert directions._compact.cache_info().currsize <= 8192


def test_robot_exclusions_and_pork_family_remain_market_driven():
    relation = directions.member_relation
    assert relation({"code": "600000.SH", "themes": ["猪肉概念"]},
                    "生猪养殖", set()) == "local_graph:猪肉概念"
    assert relation({"code": "600000.SH", "themes": ["轴承"]},
                    "机器人", set()) is None
    assert relation({"code": "600000.SH", "themes": ["人形机器人"],
                     "limit_reason": "与机器人无关"}, "机器人", {"600000.SH"}) is None
    assert relation({"code": "600000.SH", "themes": ["独立标签"]},
                    "独立方向", set(), {"独立标签"}) == "local_graph:独立标签"
