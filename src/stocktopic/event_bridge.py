"""Market-first, bounded cross-node event detection and verification (P0 only)."""

from __future__ import annotations

import json
import re
from itertools import combinations
from typing import Any

from .ai_validation import confidence_scores
from .discovery_ai_policy import DISCOVERY_AI_OPTIONS, MAX_BRIDGE_MEMBERS
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
        times = [_launch_minutes(events[code]) for code in node["member_codes"]]
        known = [value for value in times if value is not None]
        return (
            min(known, default=1440),
            -len(node["member_codes"]),
            -float(node.get("graph_weight") or node.get("cluster_confidence") or 0),
            str(node["tag"]),
        )

    return str(min(nodes, key=rank)["tag"])


def _launch_minutes(event: dict) -> float | None:
    if event.get("limit_time_basis") in {"first_observed_5m", "daily_high_time_unknown"}:
        return None  # A cumulative-high snapshot cannot establish its launch time.
    return minutes(event.get("limit_up_time"))


def _tokens(text: str) -> set[str]:
    # Generated price-qualification prose is not a business/catalyst reason.
    text = re.sub(r"创业板(?:盘中|当日)最高涨幅[^；]*[；;]?", "", text)
    text = text.replace("具体上涨原因结合题材标签、公司关联和当日新闻核查", "")
    # Strip boilerplate: a shared "涨停/产业催化" is not common-event evidence.
    text = re.sub(r"涨停|首板|产业催化|概念|公司|股份|有限公司|受益|异动", "", text.lower())
    result = set(re.findall(r"[a-z0-9]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        result.update(run[index : index + 2] for index in range(len(run) - 1))
    return result


class EventBridgeDetector:
    """Bound concrete local nodes; weaker evidence can request restricted AI review."""

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
            elif len(codes) < 2:
                reason = "node_below_two_members"
            elif len(codes) > MAX_BRIDGE_MEMBERS:
                reason = "node_exceeds_member_budget"
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
            unique = set(pair[0]["member_codes"]) | set(pair[1]["member_codes"])
            route = self._review_route(pair, by_code, scored)
            gates = {
                "below_four_unique_members": len(unique) < 4,
                "member_budget_exceeded": len(unique) > MAX_BRIDGE_MEMBERS,
                "insufficient_business_text": scored["text_similarity"] < 25 and not route,
                "missing_launch_times": scored["time_sync"] is None and not route,
                "insufficient_time_synchronization": (scored["time_sync"] is not None
                    and scored["time_sync"] < 45 and not route),
                "bridge_score_below_60": scored["bridge_score"] < 60,
            }
            excluded = [reason for reason, failed in gates.items() if failed]
            eligible = not excluded
            links[left, right] = eligible
            self.trace.append(
                {
                    "nodes": [node["tag"] for node in pair],
                    **scored,
                    "unique_member_count": len(unique),
                    "verification_route": route,
                    "excluded_reasons": excluded,
                    "excluded_reason": excluded[0] if excluded else "",
                }
            )
        for size in range(4, 1, -1):
            for indices in combinations(range(len(nodes)), size):
                if not all(links[a, b] for a, b in combinations(indices, 2)):
                    continue
                selected = [nodes[index] for index in indices]
                codes = sorted({code for node in selected for code in node["member_codes"]})
                if not 4 <= len(codes) <= MAX_BRIDGE_MEMBERS:
                    continue
                if any(set(indices) <= existing for existing, _ in result):
                    continue
                score = self._score(selected, by_code)
                if score["bridge_score"] < 60:
                    continue
                pair_routes = [self._review_route([nodes[a], nodes[b]], by_code,
                               self._score([nodes[a], nodes[b]], by_code))
                               for a, b in combinations(indices, 2)]
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
                    "verification_route": "semantic_review" if any(
                        route != "synchronized_text" for route in pair_routes
                    ) else "synchronized_text",
                    "review_routes": sorted(set(pair_routes)),
                    "local_nodes": selected,
                }
                result.append((set(indices), candidate))
                if len(result) >= 8:
                    return [candidate for _, candidate in result]
        return [candidate for _, candidate in result]

    def _review_route(self, nodes: list[dict], events: dict[str, dict], score: dict) -> str:
        sync, text = score["time_sync"], score["text_similarity"]
        if sync is not None and sync >= 45 and text >= 25:
            return "synchronized_text"
        detailed = all(sum(
            len(_tokens(str(events[code].get("limit_reason") or "") + " "
                        + str((node.get("member_reasons") or {}).get(code) or ""))) >= 4
            for code in node["member_codes"]
        ) >= 2 for node in nodes)
        roles = []
        for node in nodes:
            counts = {"production": 0, "rights": 0}
            for code in node["member_codes"]:
                reason = str(events[code].get("limit_reason") or "") + " " + str(
                    (node.get("member_reasons") or {}).get(code) or "")
                if re.search(
                    r"(?:AI|AIGC|人工智能|生成式).{0,20}(?:影视|视频|漫剧|短剧|剧本|内容生产)"
                    r"|(?:影视|漫剧|短剧|视频).{0,12}(?:AI|AIGC)", reason, re.I,
                ):
                    counts["production"] += 1
                if re.search(r"(?:IP|版权|著作权|网文|网络文学|剧本).{0,16}"
                             r"(?:改编|授权|开发|运营|资源)", reason, re.I):
                    counts["rights"] += 1
            roles.append({role for role, count in counts.items() if count >= 2})
        complementary = all(roles) and {"production", "rights"} <= set.union(*roles)
        if (sync is not None and sync >= 45 and score["graph_relation"] == 100
                and complementary):
            return "complementary_business_roles"
        if not detailed:
            return ""
        if (sync is not None and 0 < (score["launch_span_minutes"] or 0) <= 60
                and text >= 60):
            return "bounded_relay_review"
        if sync is None and text >= 60 and score["graph_relation"] == 100:
            return "business_evidence_unknown_time"
        return ""

    def _score(self, nodes: list[dict], events: dict[str, dict]) -> dict:
        members = [events[code] for code in sorted(
            {code for node in nodes for code in node["member_codes"]})]
        times = [_launch_minutes(member) for member in members]
        known = [value for value in times if value is not None]
        time_sync = (
            max(0, 100 - (max(known) - min(known)) / 45 * 100)
            if (
                len(known) >= 2
                and all(
                    any(
                        _launch_minutes(events[code]) is not None
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
            "launch_span_minutes": max(known) - min(known) if time_sync is not None else None,
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
    trace["verification_route"] = candidate.get("verification_route", "synchronized_text")
    nodes = candidate["nodes"]
    if len(codes) < 4:
        return None, {**trace, "final_reject_reason": "below_four_members"}
    if (
        not 2 <= len(nodes) <= 4
        or len(codes) > MAX_BRIDGE_MEMBERS
        or len(set(codes)) != len(codes)
        or any(is_broad_parent_tag(node) for node in nodes)
        or any(len(set(candidate["node_members"].get(node, []))) < 2 for node in nodes)
        or not set(codes) <= {event["code"] for event in events}
        or any(not set(candidate["node_members"][node]) <= set(codes) for node in nodes)
    ):
        return None, {**trace, "final_reject_reason": "invalid_local_bridge_bounds"}
    payload = {
        key: candidate[key]
        for key in ("trade_date", "nodes", "member_codes", "node_members", "anchor_tag")
    }
    payload["members"] = [
        {
            "code": event["code"],
            "name": event.get("name"),
            "limit_reason": event.get("limit_reason"),
            "limit_up_time": event.get("limit_up_time"),
            "limit_time_basis": event.get("limit_time_basis"),
            "graph_reasons": {
                node["tag"]: (node.get("member_reasons") or {}).get(event["code"])
                for node in candidate["local_nodes"]
            },
        }
        for event in events
        if event["code"] in codes
    ]
    payload["verification_route"] = candidate.get("verification_route", "synchronized_text")
    payload["evidence_limits"] = candidate.get("reasons", {})
    prompt = """你是受限的事件桥接验证器，Market First, News Second。
只判断 merge、partial_merge 或 reject。节点和成员已由本地确定，禁止增加输入外股票/节点，
禁止全市场选股或为凑4只扩池。web_search仅验证交易日共同新增催化，逐股核查理由。
partial_merge只保留明确接受节点及其成员；至少两个节点、每节点至少2只、总数至少4只。
成员重叠只能按唯一股票计数。semantic_review表示本地证据需要语义复核：
不同业务角色、接力或未知启动时间均不能自行证明共同催化；禁止补造时间或夸大同步。
无可靠共同催化证据则reject。anchor_tag不得改变。置信度使用0_100量纲，例如93。只返回JSON：
{"decision":"merge|partial_merge|reject","canonical_name":"具体事件名",
"common_logic":"共同因果链","accepted_nodes":[],"member_codes":[],
"member_reasons":[{"code":"","reason":""}],
"catalysts":[{"title":"标题","summary":"与共同事件关系","url":"实际搜索URL",
"source":"来源","published_at":"ISO时间","source_kind":"official/company_disclosure/industry_primary/authoritative_media",
"evidence_level":"官方确认/多源交叉验证/合理推断","catalyst_type":"首次催化/强化催化"}],
"confidence_scale":"0_100","confidence":0}
输入：""" + json.dumps(payload, ensure_ascii=False)
    _, parsed, sources = client._call_prompt(
        prompt,
        task_type="semantic_event_clustering",
        subject_id=candidate["trade_date"],
        **DISCOVERY_AI_OPTIONS,
    )
    decision = parsed.get("decision")
    accepted = parsed.get("accepted_nodes") or []
    returned = parsed.get("member_codes") or []
    trace.update(
        {
            "ai_bridge_decision": decision,
            "accepted_nodes": accepted,
            "ai_confidence": confidence_scores(parsed, ("confidence",))["confidence"],
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
    from .ai import _normalize_catalysts

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
        "cluster_confidence": trace["ai_confidence"],
        "catalysts": catalysts,
        "sources": sources,
        "aliases": list(dict.fromkeys([anchor, name, *accepted])),
        "model": client.model_for_task("semantic_event_clustering"),
        "decision_trace": trace,
    }, {**trace, "final": "formal_candidate", "canonical_name": name}
