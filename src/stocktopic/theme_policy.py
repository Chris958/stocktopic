from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from . import ai as ai_module
from . import theme_graph as graph_module
from . import themes as themes_module

# Very broad taxonomies must never bridge otherwise unrelated concrete themes.
_GENERIC_BRIDGE_TAGS = {
    "农业",
    "大农业",
    "养殖",
    "畜牧",
    "畜牧养殖",
    "农林牧渔",
    "消费",
    "周期",
    "涨价",
}

# Explicit synonyms are deliberately narrow. Most family merging is still driven by
# deterministic constituent overlap across KPL/DC/TDX rather than a hand-written list.
_SEMANTIC_FAMILIES: dict[str, set[str]] = {
    "生猪养殖": {
        "猪肉",
        "猪肉概念",
        "生猪",
        "生猪概念",
        "生猪养殖",
        "养猪",
        "猪周期",
        "猪价",
    },
}

_installed = False
_original_structured_event_clusters = graph_module.structured_event_clusters
_original_assess_for_admission = ai_module.OpenAIThemeExplainer.assess_for_admission


def install_theme_policy() -> None:
    """Install conservative theme-family merging and new-cycle admission semantics."""
    global _installed
    if _installed:
        return
    _installed = True

    graph_module.structured_event_clusters = policy_structured_event_clusters
    # themes.py imports the function directly, so replace that bound reference too.
    themes_module.structured_event_clusters = policy_structured_event_clusters
    ai_module.OpenAIThemeExplainer.assess_for_admission = policy_assess_for_admission


def policy_structured_event_clusters(
    events: Iterable[dict[str, Any]],
    minimum_members: int = 2,
    *,
    include_broad_parents: bool = False,
) -> list[dict[str, Any]]:
    """Add concrete cross-source theme families on top of exact graph nodes.

    Exact KPL/DC/TDX tags are retained, but nearby concrete tags can be merged when
    their constituent sets strongly overlap. This repairs cases such as 猪肉 / 生猪 /
    猪周期 / 生猪养殖 being split into several sub-threshold nodes.
    """
    event_list = [dict(item) for item in events]
    base = _original_structured_event_clusters(
        event_list,
        minimum_members=minimum_members,
        include_broad_parents=include_broad_parents,
    )
    families = _build_family_clusters(event_list, minimum_members)
    if not families:
        return base

    # A family candidate supersedes its own proper child nodes. Unrelated exact nodes
    # remain untouched, and an exact node with identical membership stays preferred.
    exact_member_sets = {
        frozenset(str(code) for code in item.get("member_codes", [])): item for item in base
    }
    accepted_families = [
        item
        for item in families
        if frozenset(item["member_codes"]) not in exact_member_sets
    ]
    if not accepted_families:
        return base

    filtered_base: list[dict[str, Any]] = []
    for item in base:
        item_codes = set(str(code) for code in item.get("member_codes", []))
        item_tag = str(item.get("tag") or "")
        superseded = any(
            item_tag in set(family.get("aliases") or [])
            and item_codes
            and item_codes < set(family["member_codes"])
            for family in accepted_families
        )
        if not superseded:
            filtered_base.append(item)

    combined = [*accepted_families, *filtered_base]
    combined.sort(
        key=lambda item: (
            int(item.get("touch_count") or len(item.get("member_codes", []))),
            float(item.get("cluster_confidence") or 0),
            int(item.get("sealed_count") or 0),
            -int(item.get("failed_count") or 0),
        ),
        reverse=True,
    )
    return combined[:30]


def _build_family_clusters(
    events: list[dict[str, Any]], minimum_members: int
) -> list[dict[str, Any]]:
    by_code = {
        str(item.get("code") or item.get("ts_code") or "").strip(): item
        for item in events
        if str(item.get("code") or item.get("ts_code") or "").strip()
    }
    memberships: dict[str, set[str]] = defaultdict(set)
    weights: dict[str, float] = defaultdict(float)
    reasons: dict[str, dict[str, str]] = defaultdict(dict)

    for event in events:
        code = str(event.get("code") or event.get("ts_code") or "").strip()
        if not code:
            continue
        for tag, weight, reason in _event_tags(event):
            if not tag:
                continue
            memberships[tag].add(code)
            weights[tag] = max(weights[tag], weight)
            if reason:
                reasons[tag][code] = reason[:300]

    tags = [
        tag
        for tag in memberships
        if not graph_module.is_broad_parent_tag(tag)
        and _compact_tag(tag) not in {_compact_tag(value) for value in _GENERIC_BRIDGE_TAGS}
    ]
    if len(tags) < 2:
        return []

    parent = {tag: tag for tag in tags}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for index, left in enumerate(tags):
        for right in tags[index + 1 :]:
            if _same_explicit_family(left, right) or _strong_membership_link(
                left,
                right,
                memberships[left],
                memberships[right],
            ):
                union(left, right)

    components: dict[str, list[str]] = defaultdict(list)
    for tag in tags:
        components[find(tag)].append(tag)

    broad_memberships: dict[str, set[str]] = defaultdict(set)
    for event in events:
        code = str(event.get("code") or event.get("ts_code") or "").strip()
        if not code:
            continue
        for tag, _, _ in _event_tags(event):
            if graph_module.is_broad_parent_tag(tag):
                broad_memberships[tag].add(code)

    result: list[dict[str, Any]] = []
    for component in components.values():
        if len(component) < 2:
            continue
        member_codes = sorted(set().union(*(memberships[tag] for tag in component)))
        if len(member_codes) < minimum_members:
            continue

        canonical = _family_canonical_name(component, memberships, weights)
        members = [by_code[code] for code in member_codes if code in by_code]
        merged_reasons: dict[str, str] = {}
        for tag in component:
            for code, reason in reasons.get(tag, {}).items():
                merged_reasons.setdefault(code, reason)

        parent_tags = sorted(
            tag
            for tag, codes in broad_memberships.items()
            if codes and len(codes & set(member_codes)) / len(member_codes) >= 0.6
        )
        sealed = sum(item.get("board_tag") == "涨停" for item in members)
        growth = sum(item.get("board_tag") == "创业板涨幅超10%" for item in members)
        failed = sum(item.get("board_tag") == "炸板" for item in members)
        confidence = min(
            97.0,
            max(weights.get(tag, 0.0) for tag in component)
            + min(3.0, max(0, len(component) - 1)),
        )
        result.append(
            {
                "tag": canonical,
                "members": members,
                "member_codes": member_codes,
                "member_reasons": merged_reasons,
                "touch_count": len(member_codes),
                "sealed_count": sealed,
                "growth_count": growth,
                "failed_count": failed,
                "common_logic": (
                    "多个结构化题材节点通过成分股高重合/明确同义关系归并为同一具体题材族："
                    + "、".join(sorted(component))
                    + "。正式阶段仍需外部催化验证。"
                ),
                "aliases": list(dict.fromkeys([canonical, *sorted(component), *parent_tags])),
                "parent_tags": parent_tags,
                "parent_only": False,
                "cluster_confidence": confidence,
                "cluster_method": "knowledge_graph_family",
                "catalysts": [],
                "model": "local-knowledge-graph-family-v1",
                "raw": {},
            }
        )
    return result


def _event_tags(event: dict[str, Any]) -> list[tuple[str, float, str]]:
    result: list[tuple[str, float, str]] = []
    limit_reason = str(event.get("limit_reason") or "").strip()
    for raw_tag in event.get("themes") or []:
        tag = graph_module._normalize_tag(raw_tag)
        if tag:
            result.append((tag, 94.0, limit_reason))

    for raw in event.get("concept_tags") or []:
        if not isinstance(raw, dict):
            continue
        tag = graph_module._normalize_tag(raw.get("tag"))
        if not tag:
            continue
        confidence = graph_module._number(raw.get("confidence"))
        weight = max(82.0, min(96.0, confidence * 100 if confidence <= 1 else confidence))
        result.append((tag, weight, ""))

    for raw in event.get("knowledge_graph_tags") or []:
        if not isinstance(raw, dict):
            continue
        tag = graph_module._normalize_tag(raw.get("tag"))
        if not tag:
            continue
        result.append(
            (
                tag,
                graph_module._number(raw.get("weight")) or 82.0,
                str(raw.get("reason") or "").strip(),
            )
        )
    return result


def _strong_membership_link(
    left: str,
    right: str,
    left_codes: set[str],
    right_codes: set[str],
) -> bool:
    intersection = len(left_codes & right_codes)
    if not intersection:
        return False
    smaller = max(1, min(len(left_codes), len(right_codes)))
    overlap = intersection / smaller
    left_key, right_key = _compact_tag(left), _compact_tag(right)
    lexical = (
        len(left_key) >= 2
        and len(right_key) >= 2
        and (left_key in right_key or right_key in left_key)
    )
    return (intersection >= 2 and overlap >= 0.5) or (lexical and overlap >= 0.34)


def _same_explicit_family(left: str, right: str) -> bool:
    left_key, right_key = _compact_tag(left), _compact_tag(right)
    for aliases in _SEMANTIC_FAMILIES.values():
        normalized = {_compact_tag(value) for value in aliases}
        if left_key in normalized and right_key in normalized:
            return True
    return False


def _family_canonical_name(
    component: list[str],
    memberships: dict[str, set[str]],
    weights: dict[str, float],
) -> str:
    compact_component = {_compact_tag(tag) for tag in component}
    for canonical, aliases in _SEMANTIC_FAMILIES.items():
        normalized = {_compact_tag(value) for value in aliases}
        if len(compact_component & normalized) >= 2:
            return canonical
    return max(
        component,
        key=lambda tag: (
            len(_compact_tag(tag)),
            len(memberships.get(tag, set())),
            weights.get(tag, 0.0),
        ),
    )


def _compact_tag(value: Any) -> str:
    text = graph_module._normalize_tag(value)
    for suffix in ("概念", "板块"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
    return text.replace(" ", "").casefold()


def policy_assess_for_admission(
    self: Any,
    theme: dict[str, Any],
    historical_matches: list[dict[str, Any]],
    eligible_stock_pool: list[dict[str, Any]],
) -> dict[str, Any]:
    """Treat a genuinely new cycle/catalyst in an old sector as admission-eligible."""
    if not self.enabled:
        raise RuntimeError("OpenAI API is not configured")

    trigger_members = [
        {
            "code": member["code"],
            "name": member["name"],
            "evidence": ai_module._compact_evidence(member.get("evidence", {})),
        }
        for member in theme.get("members", [])
        if member.get("active", 1)
    ]
    compact_history = [
        ai_module._compact_history(item) for item in historical_matches[:20]
    ]
    compact_stock_pool = [
        {
            "code": item.get("code"),
            "name": item.get("name"),
            "matched_tags": list(item.get("matched_tags") or [])[:8],
        }
        for item in eligible_stock_pool[:80]
    ]

    prompt = f"""
你是A股重点题材准入审查器。目标是识别真正形成资金共识的新炒作逻辑，同时过滤普通轮动。
必须使用web_search核查最近催化及系统提供的60交易日历史。

关键定义：行业/概念长期存在，并不等于本轮没有新题材资格。请先分类 novelty_mode：
- new_theme：新的最小共同产业/事件逻辑首次形成广泛共识；
- new_cycle：旧行业出现新的供需、价格、库存、产能、盈利周期拐点并首次形成当期广泛共识；
- new_catalyst：旧概念出现新的政策、事件、订单、产品或技术变化，实质改变本轮定价逻辑；
- old_rotation：仅旧概念反弹/轮动/换名，没有新的实质驱动。

只有 new_theme/new_cycle/new_catalyst 可令 is_new_theme=true。new_cycle/new_catalyst 不是放宽标准：
必须存在可验证的新变化、至少4只当日强势股票共同响应，并具备可解释的持续路径。
“多了一条新闻”“行业本来就涨价”“旧题材再次活跃”本身不能通过。
同一行业过去60日出现过不能单独否决；只有“同一具体催化/同一轮周期逻辑”在窗口内已广泛发酵，
才应判 old_rotation，并把对应系统历史ID写入 within_window_match_ids。
若判 new_cycle/new_catalyst，suggested_name 必须写出具体新变化，不能只写猪肉、航运、有色等老行业名。

证据优先级：政府/监管/交易所/公司公告 > 产业原始信息/权威媒体 > 研报/供应链 > 社交传闻。
持续性和空间只做条件情景，不得伪装成确定预测。必须给出反证条件。

只返回JSON对象，不要Markdown：
{{
  "suggested_name": "最小共同逻辑名称",
  "novelty_mode": "new_theme|new_cycle|new_catalyst|old_rotation",
  "is_new_theme": true,
  "novelty_confidence": 0,
  "novelty_reason": "说明本轮新变化，或为何只是旧轮动",
  "within_window_match_ids": ["仅同一具体驱动已在60交易日内发酵时填写系统历史题材ID"],
  "catalyst_summary": "当前催化链条",
  "catalyst_confidence": 0,
  "expected_duration_days": 0,
  "duration_reason": "为何可能持续或为何不可持续",
  "leader_candidate_code": "必须来自触发股票或白名单",
  "leader_upside_scenario_pct": 0,
  "upside_scenario_reason": "达到该空间需要满足的条件，不是目标价",
  "counter_evidence": ["反证或证伪条件"],
  "proposed_members": [
    {{"code":"只能来自白名单", "role":"龙头/核心/弹性/跟随", "reason":"同逻辑依据"}}
  ],
  "catalysts": [
    {{
      "title":"标题", "summary":"与题材关系", "source":"来源", "url":"原始URL",
      "published_at":"带时区ISO时间或空", "catalyst_type":"首次催化/强化催化/反证",
      "evidence_level":"官方确认/多源交叉验证/供应链未确认/合理推断",
      "source_kind":"official/company_disclosure/industry_primary/authoritative_media/brokerage_research/supply_chain_report/social"
    }}
  ]
}}

确定性触发证据：{json.dumps(trigger_members, ensure_ascii=False)}
候选最小共同逻辑：{json.dumps(theme.get("shared_tag"), ensure_ascii=False)}
图谱别名：{json.dumps(theme.get("cluster_aliases") or [], ensure_ascii=False)}
系统保存的60交易日历史相似题材：{json.dumps(compact_history, ensure_ascii=False)}
允许提议加入的股票白名单：{json.dumps(compact_stock_pool, ensure_ascii=False)}
""".strip()

    raw, parsed, sources = self._call_prompt(
        prompt,
        reasoning_effort="medium",
        task_type="admission_analysis",
        subject_id=str(theme.get("id") or ""),
        search_context_size="medium",
        max_output_tokens=6000,
        max_tool_calls=5,
    )
    required = {
        "suggested_name",
        "novelty_mode",
        "is_new_theme",
        "novelty_confidence",
        "catalyst_confidence",
        "expected_duration_days",
        "leader_candidate_code",
        "leader_upside_scenario_pct",
    }
    missing = sorted(required - set(parsed))
    if missing:
        raise RuntimeError(
            "AI admission response missing required fields: " + ", ".join(missing)
        )

    novelty_mode = _normalize_novelty_mode(parsed.get("novelty_mode"))
    novelty_confidence = ai_module._bounded_number(parsed.get("novelty_confidence"))
    qualifies = ai_module._boolean(parsed.get("is_new_theme"))
    if novelty_mode in {"new_cycle", "new_catalyst"} and novelty_confidence >= 70:
        qualifies = True
    if novelty_mode == "old_rotation":
        qualifies = False

    novelty_reason = str(parsed.get("novelty_reason") or "未提供新颖性依据")
    novelty_labels = {
        "new_theme": "新题材",
        "new_cycle": "旧赛道新周期",
        "new_catalyst": "旧赛道新催化",
        "old_rotation": "旧题材轮动",
    }
    novelty_reason = f"[{novelty_labels[novelty_mode]}] {novelty_reason}"

    return {
        "model": self.model_for_task("admission_analysis"),
        "suggested_name": ai_module._concrete_suggested_name(parsed, theme),
        "novelty_mode": novelty_mode,
        "is_new_theme": qualifies,
        "novelty_confidence": novelty_confidence,
        "novelty_reason": novelty_reason,
        "within_window_match_ids": [
            ai_module._integer(value)
            for value in (parsed.get("within_window_match_ids") or [])[:20]
            if ai_module._integer(value) > 0
        ],
        "catalyst_summary": str(parsed.get("catalyst_summary") or "未找到明确催化"),
        "catalyst_confidence": ai_module._bounded_number(parsed.get("catalyst_confidence")),
        "expected_duration_days": max(
            0, ai_module._integer(parsed.get("expected_duration_days"))
        ),
        "duration_reason": str(parsed.get("duration_reason") or "未提供持续性依据"),
        "leader_candidate_code": str(parsed.get("leader_candidate_code") or "").strip(),
        "leader_upside_scenario_pct": max(
            0.0, ai_module._number(parsed.get("leader_upside_scenario_pct"))
        ),
        "upside_scenario_reason": str(
            parsed.get("upside_scenario_reason") or "未提供空间推演依据"
        ),
        "counter_evidence": [
            str(value) for value in (parsed.get("counter_evidence") or [])[:8]
        ],
        "proposed_members": ai_module._normalize_proposed_members(
            parsed.get("proposed_members"), eligible_stock_pool
        ),
        "sources": sources,
        "catalysts": ai_module._normalize_catalysts(parsed.get("catalysts"), sources),
        "raw": raw,
    }


def _normalize_novelty_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    aliases = {
        "new_theme": "new_theme",
        "new_cycle": "new_cycle",
        "new_catalyst": "new_catalyst",
        "old_rotation": "old_rotation",
        "new theme": "new_theme",
        "new cycle": "new_cycle",
        "new catalyst": "new_catalyst",
        "old rotation": "old_rotation",
    }
    return aliases.get(mode, "old_rotation")
