"""Market-first, bounded cross-node event detection and verification (P0 only)."""

from __future__ import annotations

import json
import re
from itertools import combinations
from typing import Any

from .theme_graph import is_broad_parent_tag
from .theme_intelligence import cluster_metrics


def minutes(value: Any) -> int | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{2}):?(\d{2})(?::?\d{2})?", text)
    if not match:
        return None
    hour, minute = map(int, match.groups())
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def anchor_node(nodes: list[dict[str, Any]], events: dict[str, dict]) -> str:
    def rank(node: dict) -> tuple:
        times = [minutes(events[code].get("limit_up_time")) for code in node["member_codes"]]
        known = [value for value in times if value is not None]
        return (
            min(known, default=1440),
            -len(node["member_codes"]),
            -float(node.get("graph_weight") or node.get("cluster_confidence") or 0),
            str(node["tag"]),
        )

    return str(min(nodes, key=rank)["tag"])


def _tokens(text: str) -> set[str]:
    # Strip boilerplate: a shared "涨停/产业催化" is not common-event evidence.
    text = re.sub(r"涨停|首板|产业催化|概念|公司|股份|有限公司|受益|异动", "", text.lower())
    result = set(re.findall(r"[a-z0-9]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        result.update(run[index : index + 2] for index in range(len(run) - 1))
    return result


class EventBridgeDetector:
    """Only disjoint concrete local nodes with observed market and textual links."""

    def __init__(self) -> None:
        self.trace: list[dict[str, Any]] = []

    def detect(self, trade_date: str, events: list[dict], clusters: list[dict]) -> list[dict]:
        self.trace = []
        by_code = {str(item["code"]): item for item in events}
        nodes = []
        for cluster in clusters:
            tag = str(cluster.get("tag") or "")
            codes = sorted(set(cluster.get("member_codes") or []) & by_code.keys())
            reason = ""
            if is_broad_parent_tag(tag) or cluster.get("parent_only"):
                reason = "broad_parent_context_only"
            elif not 2 <= len(codes) <= 3:
                reason = "not_subthreshold_concrete_node"
            if reason:
                self.trace.append({"nodes": [tag], "excluded_reason": reason})
            else:
                nodes.append({**cluster, "member_codes": codes})
        nodes.sort(key=lambda item: str(item["tag"]))
        result = []
        # Pairwise complete linkage prevents an unrelated synchronized third node
        # from riding along on a single good link. At most 30 local nodes exist.
        links = {}
        for left, right in combinations(range(len(nodes)), 2):
            pair = [nodes[left], nodes[right]]
            scored = self._score(pair, by_code)
            eligible = (
                not set(pair[0]["member_codes"]) & set(pair[1]["member_codes"])
                and scored["text_similarity"] >= 25
                and scored["time_sync"] is not None
                and scored["time_sync"] >= 45
                and scored["bridge_score"] >= 60
            )
            links[left, right] = eligible
            self.trace.append(
                {
                    "nodes": [node["tag"] for node in pair],
                    **scored,
                    "excluded_reason": "" if eligible else "insufficient_sync_or_text_or_overlap",
                }
            )
        for size in range(4, 1, -1):
            for indices in combinations(range(len(nodes)), size):
                if not all(links[a, b] for a, b in combinations(indices, 2)):
                    continue
                selected = [nodes[index] for index in indices]
                codes = sorted({code for node in selected for code in node["member_codes"]})
                if not 4 <= len(codes) <= 12:
                    continue
                if any(set(indices) <= existing for existing, _ in result):
                    continue
                score = self._score(selected, by_code)
                if score["bridge_score"] < 60:
                    continue
                candidate = {
                    "trade_date": trade_date,
                    "anchor_tag": anchor_node(selected, by_code),
                    "nodes": [node["tag"] for node in selected],
                    "member_codes": codes,
                    "node_members": {node["tag"]: node["member_codes"] for node in selected},
                    "touch_count": len(codes),
                    "bridge_score": score["bridge_score"],
                    "synchronization_score": score["time_sync"],
                    "reasons": score,
                    "local_nodes": selected,
                }
                result.append((set(indices), candidate))
                if len(result) >= 8:
                    return [candidate for _, candidate in result]
        return [candidate for _, candidate in result]

    def _score(self, nodes: list[dict], events: dict[str, dict]) -> dict:
        members = [events[code] for node in nodes for code in node["member_codes"]]
        times = [minutes(member.get("limit_up_time")) for member in members]
        known = [value for value in times if value is not None]
        time_sync = (
            max(0, 100 - (max(known) - min(known)) / 45 * 100)
            if (
                len(known) >= 2
                and all(
                    any(
                        minutes(events[code].get("limit_up_time")) is not None
                        for code in node["member_codes"]
                    )
                    for node in nodes
                )
            )
            else None
        )
        qualities = []
        texts = []
        for node in nodes:
            group = [events[code] for code in node["member_codes"]]
            if all(member.get("board_tag") for member in group):
                qualities.append(
                    cluster_metrics(
                        {
                            "members": group,
                            "touch_count": len(group),
                            "sealed_count": sum(m.get("board_tag") == "涨停" for m in group),
                            "growth_count": sum(
                                m.get("board_tag") == "创业板涨幅超10%" for m in group
                            ),
                            "failed_count": sum(m.get("board_tag") == "炸板" for m in group),
                        }
                    )["board_quality_score"]
                )
            texts.append(
                _tokens(
                    " ".join(
                        str(events[code].get("limit_reason") or "")
                        + " "
                        + str((node.get("member_reasons") or {}).get(code) or "")
                        for code in node["member_codes"]
                    )
                )
            )
        similarity = min(
            (100 * len(a & b) / max(1, min(len(a), len(b))) for a, b in combinations(texts, 2)),
            default=0,
        )
        # Use only explicit graph links/context; no invented historical success rates.
        contexts = [set(node.get("parent_tags") or []) for node in nodes]
        graph = 100.0 if contexts and set.intersection(*contexts) else None
        strength = (
            min(qualities) - (max(qualities) - min(qualities))
            if (len(qualities) == len(nodes))
            else None
        )
        components = {
            "time_sync": (time_sync, 0.35),
            "market_strength": (strength, 0.25),
            "text_similarity": (similarity, 0.25),
            "graph_relation": (graph, 0.15),
        }
        available = [(value, weight) for value, weight in components.values() if value is not None]
        score = sum(value * weight for value, weight in available) / sum(w for _, w in available)
        return {
            **{key: value for key, (value, _) in components.items()},
            "bridge_score": round(score, 2),
            "available_weights": {
                key: weight for key, (value, weight) in components.items() if value is not None
            },
        }


def verify_event_bridge(
    client: Any, candidate: dict, events: list[dict]
) -> tuple[dict | None, dict]:
    """Fail closed on expanded stocks/nodes, insufficient partials or missing evidence."""
    codes = candidate["member_codes"]
    trace = {
        key: candidate[key]
        for key in ("anchor_tag", "nodes", "member_codes", "bridge_score", "reasons")
    }
    nodes = candidate["nodes"]
    if len(codes) < 4:
        return None, {**trace, "final_reject_reason": "below_four_members"}
    if (
        not 2 <= len(nodes) <= 4
        or len(codes) > 12
        or len(set(codes)) != len(codes)
        or any(is_broad_parent_tag(node) for node in nodes)
        or any(len(candidate["node_members"].get(node, [])) < 2 for node in nodes)
    ):
        return None, {**trace, "final_reject_reason": "invalid_local_bridge_bounds"}
    payload = {
        key: candidate[key]
        for key in ("trade_date", "nodes", "member_codes", "node_members", "anchor_tag")
    }
    payload["members"] = [
        {
            "code": event["code"],
            "limit_reason": event.get("limit_reason"),
            "graph_reasons": {
                node["tag"]: (node.get("member_reasons") or {}).get(event["code"])
                for node in candidate["local_nodes"]
            },
        }
        for event in events
        if event["code"] in codes
    ]
    prompt = """你是受限的事件桥接验证器，Market First, News Second。
只判断 merge、partial_merge 或 reject。节点和成员已由本地确定，禁止增加输入外股票/节点，
禁止全市场选股或为凑4只扩池。web_search仅验证交易日共同新增催化，逐股核查理由。
partial_merge只保留明确接受节点及其成员；至少两个节点、每节点至少2只、总数至少4只。
无可靠共同催化证据则reject。anchor_tag不得改变。只返回JSON：
{"decision":"merge|partial_merge|reject","canonical_name":"具体事件名",
"common_logic":"共同因果链","accepted_nodes":[],"member_codes":[],
"member_reasons":[{"code":"","reason":""}],
"catalysts":[{"title":"标题","summary":"与共同事件关系","url":"实际搜索URL",
"source":"来源","published_at":"ISO时间","source_kind":"official/company_disclosure/industry_primary/authoritative_media",
"evidence_level":"官方确认/多源交叉验证/合理推断","catalyst_type":"首次催化/强化催化"}],
"confidence":0}
输入：""" + json.dumps(payload, ensure_ascii=False)
    _, parsed, sources = client._call_prompt(
        prompt,
        task_type="semantic_event_clustering",
        subject_id=candidate["trade_date"],
        reasoning_effort="low",
        search_context_size="low",
        max_output_tokens=4000,
        max_tool_calls=4,
    )
    decision = parsed.get("decision")
    accepted = parsed.get("accepted_nodes") or []
    returned = parsed.get("member_codes") or []
    trace.update(
        {
            "ai_bridge_decision": decision,
            "accepted_nodes": accepted,
            "ai_confidence": parsed.get("confidence"),
        }
    )

    def reject(reason: str) -> tuple[None, dict]:
        return None, {**trace, "final_reject_reason": reason}

    if decision not in {"merge", "partial_merge"}:
        return reject("common_catalyst_evidence_insufficient")
    if (
        not isinstance(accepted, list)
        or not isinstance(returned, list)
        or not all(isinstance(value, str) for value in [*accepted, *returned])
    ):
        return reject("invalid_ai_schema")
    if (
        len(set(accepted)) < 2
        or not set(accepted) <= set(candidate["nodes"])
        or not set(returned) <= set(codes)
    ):
        return reject("invalid_nodes_or_expanded_members")
    if decision == "merge" and set(accepted) != set(candidate["nodes"]):
        return reject("merge_missing_nodes")
    allowed = {code for node in accepted for code in candidate["node_members"][node]}
    if not set(returned) <= allowed or len(set(returned)) < 4:
        return reject("partial_below_four_or_outside_accepted_nodes")
    if any(len(set(returned) & set(candidate["node_members"][node])) < 2 for node in accepted):
        return reject("accepted_node_below_two")
    name = str(parsed.get("canonical_name") or "").strip()
    logic = str(parsed.get("common_logic") or "").strip()
    from .ai import _bounded_number, _normalize_catalysts

    catalysts = _normalize_catalysts(parsed.get("catalysts"), sources)
    reasons = {
        item["code"]: str(item.get("reason") or "").strip()
        for item in parsed.get("member_reasons", [])
        if isinstance(item, dict) and item.get("code") in returned
    }
    searched_urls = {source.get("url") for source in sources if source.get("url")}
    if (
        not name
        or is_broad_parent_tag(name)
        or not logic
        or not any(catalyst.get("url") in searched_urls for catalyst in catalysts)
        or not all(reasons.get(code) for code in returned)
    ):
        return reject("missing_specific_logic_catalyst_or_member_evidence")
    by_code = {event["code"]: event for event in events}
    accepted_local = [node for node in candidate["local_nodes"] if node["tag"] in accepted]
    anchor = anchor_node(accepted_local, by_code)
    trace["final_anchor_tag"] = anchor
    return {
        "tag": anchor,
        "canonical_name": name,
        "common_logic": logic,
        "member_codes": sorted(set(returned)),
        "member_reasons": reasons,
        "cluster_method": "event_bridge",
        "cluster_confidence": _bounded_number(parsed.get("confidence")),
        "catalysts": catalysts,
        "sources": sources,
        "aliases": list(dict.fromkeys([anchor, name, *accepted])),
        "model": client.model_for_task("semantic_event_clustering"),
        "decision_trace": trace,
    }, {**trace, "final": "formal_candidate", "canonical_name": name}
