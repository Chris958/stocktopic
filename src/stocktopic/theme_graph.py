from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable


SOURCE_PRIORITY = {
    "dc_concept": 100,
    "kpl_concept": 92,
    "tdx": 82,
    "sw_industry": 58,
    "citic_industry": 58,
    "external_search": 35,
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
                    label=str(theme_code),
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
            code = row.get("ts_code")
            concept = row.get("con_code") or row.get("con_name")
            if not code or not concept:
                continue
            self.add_edge(
                str(code),
                key=f"kpl:{concept}",
                label=str(row.get("con_name") or concept),
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

    def common_nodes(self, stock_codes: Iterable[str], minimum_members: int = 2) -> list[dict[str, Any]]:
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
