from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


SOURCE_PRIORITY = {
    "dc_concept": 100,
    "kpl_concept": 94,
    "tdx": 86,
    "sw_industry": 58,
    "citic_industry": 58,
    "external_search": 35,
}

# These labels are too broad to create a two-stock early theme by themselves.
# They can still appear as aliases/context once a more specific graph node exists.
BROAD_EARLY_TAGS = {
    "AI",
    "国企",
    "央企",
    "并购",
    "并购重组",
    "化工",
    "科技",
    "消费",
    "金融",
}


@dataclass
class GraphNode:
    key: str
    label: str
    source: str
    weight: float
    reason: str = ""
    aliases: set[str] = field(default_factory=set)


class ThemeKnowledgeGraph:
    """Small in-memory graph used before web/LLM semantic expansion.

    Structured concept membership is authoritative for first-pass grouping.
    Web search is deliberately lower-priority and is used to complete the
    causal explanation, not to invent initial membership.
    """

    def __init__(self) -> None:
        self.stock_nodes: dict[str, dict[str, GraphNode]] = defaultdict(dict)

    def add_edge(
        self,
        stock_code: str,
        *,
        key: str,
        label: str,
        source: str,
        reason: str = "",
        aliases: Iterable[str] = (),
    ) -> None:
        code = str(stock_code or "").strip()
        key = str(key or label or "").strip()
        label = str(label or key).strip()
        if not code or not key:
            return
        weight = float(SOURCE_PRIORITY.get(source, 30))
        current = self.stock_nodes[code].get(key)
        if current and current.weight > weight:
            return
        self.stock_nodes[code][key] = GraphNode(
            key=key,
            label=label,
            source=source,
            weight=weight,
            reason=str(reason or ""),
            aliases={str(value).strip() for value in aliases if str(value).strip()},
        )

    def ingest_dc_concept(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            code = row.get("ts_code")
            theme_code = row.get("theme_code")
            industry_code = row.get("industry_code")
            reason = str(row.get("reason") or "")
            if theme_code:
                self.add_edge(
                    str(code),
                    key=f"dc:{theme_code}",
                    label=str(row.get("theme_name") or theme_code),
                    source="dc_concept",
                    reason=reason,
                    aliases=[row.get("industry")],
                )
            if industry_code:
                self.add_edge(
                    str(code),
                    key=f"industry:{industry_code}",
                    label=str(row.get("industry") or industry_code),
                    source="dc_concept",
                    reason=reason,
                )

    def ingest_kpl_concept(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            code = row.get("con_code") or row.get("ts_code")
            concept = row.get("name") or row.get("con_name")
            if not code or not concept:
                continue
            self.add_edge(
                str(code),
                key=f"kpl:{concept}",
                label=str(concept),
                source="kpl_concept",
                reason=str(row.get("desc") or ""),
            )

    def ingest_industry(
        self, rows: Iterable[dict[str, Any]], source: str = "sw_industry"
    ) -> None:
        for row in rows:
            code = row.get("ts_code")
            if not code:
                continue
            for level in ("l3", "l2", "l1"):
                node_code = row.get(f"{level}_code")
                node_name = row.get(f"{level}_name")
                if node_code:
                    self.add_edge(
                        str(code),
                        key=f"{source}:{node_code}",
                        label=str(node_name or node_code),
                        source=source,
                    )

    def common_nodes(
        self, stock_codes: Iterable[str], minimum_members: int = 2
    ) -> list[dict[str, Any]]:
        memberships: dict[str, list[str]] = defaultdict(list)
        labels: dict[str, str] = {}
        weights: dict[str, float] = {}
        sources: dict[str, str] = {}
        reasons: dict[str, dict[str, str]] = defaultdict(dict)
        for code in dict.fromkeys(str(value) for value in stock_codes):
            for key, node in self.stock_nodes.get(code, {}).items():
                memberships[key].append(code)
                labels[key] = node.label
                weights[key] = max(weights.get(key, 0), node.weight)
                sources[key] = node.source
                if node.reason:
                    reasons[key][code] = node.reason
        result = []
        for key, members in memberships.items():
            unique = list(dict.fromkeys(members))
            if len(unique) < minimum_members:
                continue
            result.append(
                {
                    "node_key": key,
                    "tag": labels[key],
                    "member_codes": unique,
                    "member_reasons": reasons.get(key, {}),
                    "graph_source": sources[key],
                    "graph_weight": weights[key],
                    "cluster_method": "knowledge_graph",
                    "cluster_confidence": min(100.0, weights[key] + len(unique) * 2),
                }
            )
        return sorted(
            result,
            key=lambda item: (item["graph_weight"], len(item["member_codes"])),
            reverse=True,
        )

    def enrich_events(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for event in events:
            item = dict(event)
            code = str(item.get("code") or item.get("ts_code") or "")
            graph_tags = [
                {
                    "key": node.key,
                    "tag": node.label,
                    "source": node.source,
                    "weight": node.weight,
                    "reason": node.reason,
                }
                for node in sorted(
                    self.stock_nodes.get(code, {}).values(),
                    key=lambda node: node.weight,
                    reverse=True,
                )[:12]
            ]
            item["knowledge_graph_tags"] = graph_tags
            result.append(item)
        return result


def structured_event_clusters(
    events: Iterable[dict[str, Any]], minimum_members: int = 2
) -> list[dict[str, Any]]:
    """Group strong stocks by deterministic structured graph nodes.

    Current-day KPL tags are preferred, then persisted concept tags (KPL/DC/TDX).
    No network or LLM is used here, so this function is safe for the 2-3 stock
    early-observation layer.
    """
    event_list = [dict(item) for item in events if str(item.get("code") or "").strip()]
    by_code = {str(item["code"]): item for item in event_list}
    memberships: dict[str, dict[str, float]] = defaultdict(dict)
    reasons: dict[str, dict[str, str]] = defaultdict(dict)
    aliases: dict[str, set[str]] = defaultdict(set)

    for event in event_list:
        code = str(event["code"])
        limit_reason = str(event.get("limit_reason") or "").strip()
        for raw_tag in event.get("themes") or []:
            tag = _normalize_tag(raw_tag)
            if not tag:
                continue
            memberships[tag][code] = max(memberships[tag].get(code, 0.0), 94.0)
            if limit_reason:
                reasons[tag][code] = limit_reason[:300]

        for raw in event.get("concept_tags") or []:
            if not isinstance(raw, dict):
                continue
            tag = _normalize_tag(raw.get("tag"))
            if not tag:
                continue
            confidence = _number(raw.get("confidence"))
            weight = max(82.0, min(96.0, confidence * 100 if confidence <= 1 else confidence))
            memberships[tag][code] = max(memberships[tag].get(code, 0.0), weight)
            source = str(raw.get("source") or "").strip()
            if source:
                aliases[tag].add(source)

        for raw in event.get("knowledge_graph_tags") or []:
            if not isinstance(raw, dict):
                continue
            tag = _normalize_tag(raw.get("tag"))
            if not tag:
                continue
            weight = _number(raw.get("weight")) or 82.0
            memberships[tag][code] = max(memberships[tag].get(code, 0.0), weight)
            reason = str(raw.get("reason") or "").strip()
            if reason:
                reasons[tag][code] = reason[:300]

    result: list[dict[str, Any]] = []
    for tag, member_weights in memberships.items():
        member_codes = list(member_weights)
        if len(member_codes) < minimum_members:
            continue
        strongest = max(member_weights.values(), default=0.0)
        # A broad label cannot create a two-stock alert by itself. Three or more
        # simultaneous stocks may still use it as a provisional observation node.
        if len(member_codes) == 2 and tag.upper() in {item.upper() for item in BROAD_EARLY_TAGS}:
            continue
        if len(member_codes) == 2 and strongest < 88:
            continue
        members = [by_code[code] for code in member_codes if code in by_code]
        sealed = sum(item.get("board_tag") == "涨停" for item in members)
        growth = sum(item.get("board_tag") == "创业板涨幅超10%" for item in members)
        failed = sum(item.get("board_tag") == "炸板" for item in members)
        confidence = min(98.0, strongest + max(0, len(member_codes) - 2) * 2)
        result.append(
            {
                "tag": tag,
                "members": members,
                "member_codes": member_codes,
                "member_reasons": reasons.get(tag, {}),
                "touch_count": len(member_codes),
                "sealed_count": sealed,
                "growth_count": growth,
                "failed_count": failed,
                "common_logic": f"结构化题材知识图谱共同节点“{tag}”，等待外部催化验证。",
                "aliases": [tag, *sorted(aliases.get(tag, set()))[:6]],
                "cluster_confidence": confidence,
                "cluster_method": "knowledge_graph",
                "catalysts": [],
                "model": "local-knowledge-graph-v1",
                "raw": {},
            }
        )

    return sorted(
        result,
        key=lambda item: (
            int(item["touch_count"]),
            float(item["cluster_confidence"]),
            int(item["sealed_count"]),
            -int(item["failed_count"]),
        ),
        reverse=True,
    )[:30]


def install_graph_first_ai_clustering() -> None:
    """Install the graph-first clustering method on the already-loaded AI class.

    StockTopicService imports the AI class before it constructs ThemeDiscovery.
    ThemeDiscovery calls this installer in __init__, so the class is patched before
    the explainer instance is created without changing the large service module.
    """
    from . import ai as ai_module

    cls = ai_module.OpenAIThemeExplainer
    if getattr(cls, "_graph_first_clustering_installed", False):
        return
    cls.cluster_limit_events = _graph_first_cluster_limit_events
    cls._graph_first_clustering_installed = True


def _graph_first_cluster_limit_events(
    self: Any,
    trade_date: str,
    events: list[dict[str, Any]],
    minimum_members: int = 2,
) -> list[dict[str, Any]]:
    """Use local graph for 2-3 stocks and web/AI only from four stocks onward."""
    from . import ai as ai_module

    if not self.enabled:
        raise RuntimeError("OpenAI API is not configured")

    local_clusters = structured_event_clusters(events, minimum_members=max(2, minimum_members))
    allowed_codes = {
        str(item.get("code") or "").strip()
        for item in events
        if str(item.get("code") or "").strip()
    }
    formal_minimum = 4
    if len(allowed_codes) < formal_minimum:
        # Critical cost-control rule: never invoke web_search/LLM for the 2-3 stock layer.
        return local_clusters

    graph_candidates = [item for item in local_clusters if len(item["member_codes"]) >= 2][:20]
    if not graph_candidates:
        # Fail closed: without a structured graph relationship, do not ask the model
        # to invent a four-stock theme from scratch.
        return []

    compact_events = []
    for event in events[:120]:
        code = str(event.get("code") or "").strip()
        if not code:
            continue
        compact_events.append(
            {
                "code": code,
                "name": event.get("name"),
                "market": event.get("market"),
                "board_tag": event.get("board_tag"),
                "status": event.get("status"),
                "limit_reason": str(event.get("limit_reason") or "")[:240],
                "source_themes": list(event.get("themes") or [])[:8],
                "concept_tags": [
                    item.get("tag")
                    for item in event.get("concept_tags", [])[:10]
                    if isinstance(item, dict) and item.get("tag")
                ],
            }
        )

    graph_payload = [
        {
            "anchor_tag": item["tag"],
            "member_codes": item["member_codes"],
            "cluster_confidence": item["cluster_confidence"],
            "member_reasons": item.get("member_reasons", {}),
        }
        for item in graph_candidates
    ]
    anchor_tags = {str(item["anchor_tag"]) for item in graph_payload}
    prompt = f"""
你是A股题材图谱验证器。系统已经先用结构化知识图谱完成股票聚合；你只能在这些图谱候选上
使用web_search补充当日/隔夜催化、验证共同因果链，不能从全市场自由拼凑股票。

正式题材硬门槛：至少{formal_minimum}只输入强势股票共同指向同一具体新增事件/产业逻辑。
允许把多个相邻图谱节点合并成一个具体事件，但必须有公开信息证明它们属于同一催化链。

硬约束：
1. anchor_tag必须逐字来自“图谱候选”的anchor_tag，不得自创；它用于保证2-3只观察层升级到4只时题材ID稳定。
2. member_codes只能来自输入股票，且至少{formal_minimum}只；不得为了凑数加入宽泛行业相关股。
3. canonical_name可以把anchor_tag优化成更具体的新事件名，但不能改变anchor_tag。
4. 必须逐只核查涨停原因/公司业务与共同事件关系；找不到关系的股票不得纳入。
5. 新闻只负责验证和补全图谱，不负责创造初始股票集合。
6. 没有合格正式题材就返回空数组。

只返回JSON对象：
{{
  "clusters": [
    {{
      "anchor_tag": "必须来自图谱候选",
      "canonical_name": "更具体的共同事件名称",
      "common_logic": "事件→产业变化→股票受益的共同链条",
      "member_codes": ["至少4只，只能来自输入"],
      "member_reasons": [{{"code":"股票代码", "reason":"关联依据"}}],
      "aliases": ["常用近义名"],
      "cluster_confidence": 0,
      "catalysts": [
        {{
          "title":"标题", "summary":"与共同事件关系", "source":"来源",
          "url":"web_search实际URL", "published_at":"ISO时间或空",
          "catalyst_type":"首次催化/强化催化/反证",
          "evidence_level":"官方确认/多源交叉验证/供应链未确认/合理推断",
          "source_kind":"official/company_disclosure/industry_primary/authoritative_media/brokerage_research/supply_chain_report/social"
        }}
      ]
    }}
  ]
}}

交易日：{trade_date}
图谱候选：{json.dumps(graph_payload, ensure_ascii=False)}
输入强势股票：{json.dumps(compact_events, ensure_ascii=False)}
""".strip()

    raw, parsed, sources = self._call_prompt(
        prompt,
        reasoning_effort="medium",
        task_type="semantic_event_clustering",
        subject_id=trade_date,
        search_context_size="medium",
        max_output_tokens=7000,
        max_tool_calls=5,
    )
    clusters = parsed.get("clusters")
    if not isinstance(clusters, list):
        raise RuntimeError("AI graph verification response missing clusters array")

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for cluster in clusters[:20]:
        if not isinstance(cluster, dict):
            continue
        anchor = str(cluster.get("anchor_tag") or "").strip()
        if anchor not in anchor_tags:
            continue
        codes = list(
            dict.fromkeys(
                str(code).strip()
                for code in cluster.get("member_codes", [])
                if str(code).strip() in allowed_codes
            )
        )
        if len(codes) < formal_minimum:
            continue
        signature = (anchor, tuple(sorted(codes)))
        if signature in seen:
            continue
        seen.add(signature)
        canonical_name = str(cluster.get("canonical_name") or anchor).strip()
        logic = str(cluster.get("common_logic") or "").strip()
        if not logic:
            continue
        member_reasons = {
            str(item.get("code") or "").strip(): str(item.get("reason") or "").strip()
            for item in cluster.get("member_reasons", [])
            if isinstance(item, dict)
            and str(item.get("code") or "").strip() in codes
            and str(item.get("reason") or "").strip()
        }
        aliases = [
            str(value).strip()
            for value in cluster.get("aliases", [])[:12]
            if str(value).strip()
        ]
        result.append(
            {
                "tag": anchor,
                "canonical_name": canonical_name,
                "common_logic": logic[:1000],
                "member_codes": codes,
                "member_reasons": member_reasons,
                "aliases": list(dict.fromkeys([anchor, canonical_name, *aliases])),
                "cluster_confidence": ai_module._bounded_number(
                    cluster.get("cluster_confidence")
                ),
                "cluster_method": "semantic_event",
                "catalysts": ai_module._normalize_catalysts(
                    cluster.get("catalysts"), sources
                ),
                "sources": sources,
                "model": self.model_for_task("semantic_event_clustering"),
                "raw": raw,
            }
        )
    return result


def _normalize_tag(value: Any) -> str:
    tag = " ".join(str(value or "").replace("\u3000", " ").split()).strip(" ,，、/|")
    if len(tag) < 2 or len(tag) > 50:
        return ""
    return tag


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
