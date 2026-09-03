from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

SOURCE_LABELS = {
    "kpl": "开盘啦",
    "dc": "东方财富",
    "tdx": "通达信",
}


def build_theme_graph(
    database: Any,
    trade_date: str | None = None,
    *,
    source: str = "all",
    query: str = "",
    min_members: int = 1,
    limit: int = 800,
) -> dict[str, Any]:
    """Build a read-only, cross-source concept graph for the frontend browser."""
    normalized_date = (trade_date or "").replace("-", "").strip()
    if normalized_date and not re.fullmatch(r"20\d{6}", normalized_date):
        raise ValueError("trade_date must be YYYYMMDD or YYYY-MM-DD")
    if source not in {"all", *SOURCE_LABELS}:
        raise ValueError("source must be all/kpl/dc/tdx")
    min_members = max(1, min(int(min_members), 500))
    limit = max(1, min(int(limit), 2000))
    keyword = query.strip().casefold()

    with database.connect() as connection:
        if not normalized_date:
            row = connection.execute(
                "SELECT MAX(trade_date) AS trade_date FROM kpl_concept_memberships"
            ).fetchone()
            normalized_date = str(row["trade_date"] or "") if row else ""
        if not normalized_date:
            return _empty_graph()
        rows = connection.execute(
            """
            SELECT
                m.trade_date, m.concept_id, m.concept_name, m.code, m.name,
                m.description, m.hot_num, m.synced_at,
                s.market, s.industry
            FROM kpl_concept_memberships m
            LEFT JOIN stocks s ON s.code=m.code
            WHERE m.trade_date=?
            ORDER BY m.concept_name, m.code
            """,
            (normalized_date,),
        ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    source_stats = {
        key: {"id": key, "label": label, "edges": 0, "nodes": set(), "members": set()}
        for key, label in SOURCE_LABELS.items()
    }
    max_synced_at = ""

    for row in rows:
        concept_id = str(row["concept_id"] or "").strip()
        concept_name = str(row["concept_name"] or "").strip()
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "").strip()
        if not concept_name or not code:
            continue
        edge_source = _concept_source(concept_id)
        stat = source_stats[edge_source]
        stat["edges"] += 1
        stat["nodes"].add(_canonical_key(concept_name))
        stat["members"].add(code)
        max_synced_at = max(max_synced_at, str(row["synced_at"] or ""))
        if source != "all" and edge_source != source:
            continue

        key = _canonical_key(concept_name)
        node = groups.setdefault(
            key,
            {
                "id": key,
                "name": concept_name,
                "concept_ids": defaultdict(list),
                "sources": set(),
                "members": {},
                "hot_rank": None,
            },
        )
        if len(concept_name) < len(str(node["name"])):
            node["name"] = concept_name
        node["sources"].add(edge_source)
        if concept_id not in node["concept_ids"][edge_source]:
            node["concept_ids"][edge_source].append(concept_id)

        hot_num = _positive_int(row["hot_num"])
        if hot_num and (node["hot_rank"] is None or hot_num < node["hot_rank"]):
            node["hot_rank"] = hot_num

        member = node["members"].setdefault(
            code,
            {
                "code": code,
                "name": name or code,
                "market": str(row["market"] or ""),
                "industry": str(row["industry"] or ""),
                "sources": set(),
                "reasons": [],
                "hot_rank": None,
            },
        )
        member["sources"].add(edge_source)
        description = str(row["description"] or "").strip()
        if description:
            reason = {
                "source": edge_source,
                "source_label": SOURCE_LABELS[edge_source],
                "text": description[:800],
            }
            if reason not in member["reasons"]:
                member["reasons"].append(reason)
        if hot_num and (member["hot_rank"] is None or hot_num < member["hot_rank"]):
            member["hot_rank"] = hot_num

    items: list[dict[str, Any]] = []
    visible_edges = 0
    visible_members: set[str] = set()
    for node in groups.values():
        members = list(node["members"].values())
        if len(members) < min_members:
            continue
        if keyword and not _node_matches(node, members, keyword):
            continue
        rendered_members = []
        for member in members:
            member_sources = sorted(member["sources"], key=_source_order)
            visible_edges += len(member_sources)
            visible_members.add(member["code"])
            rendered_members.append(
                {
                    **{key: value for key, value in member.items() if key != "sources"},
                    "sources": [
                        {"id": value, "label": SOURCE_LABELS[value]}
                        for value in member_sources
                    ],
                    "source_count": len(member_sources),
                }
            )
        rendered_members.sort(
            key=lambda item: (
                -int(item["source_count"]),
                item["hot_rank"] if item["hot_rank"] is not None else 999999,
                item["code"],
            )
        )
        node_sources = sorted(node["sources"], key=_source_order)
        items.append(
            {
                "id": node["id"],
                "name": node["name"],
                "sources": [
                    {"id": value, "label": SOURCE_LABELS[value]} for value in node_sources
                ],
                "source_count": len(node_sources),
                "cross_source": len(node_sources) >= 2,
                "concept_ids": {
                    value: list(node["concept_ids"].get(value, [])) for value in node_sources
                },
                "member_count": len(rendered_members),
                "hot_rank": node["hot_rank"],
                "members": rendered_members,
            }
        )

    items.sort(
        key=lambda item: (
            -int(item["source_count"]),
            -int(item["member_count"]),
            item["hot_rank"] if item["hot_rank"] is not None else 999999,
            str(item["name"]),
        )
    )
    items = items[:limit]

    return {
        "trade_date": normalized_date,
        "synced_at": max_synced_at or None,
        "stats": {
            "nodes": len(items),
            "members": len(visible_members),
            "edges": visible_edges,
            "cross_source_nodes": sum(1 for item in items if item["cross_source"]),
        },
        "sources": [
            {
                "id": key,
                "label": value["label"],
                "edges": value["edges"],
                "nodes": len(value["nodes"]),
                "members": len(value["members"]),
            }
            for key, value in source_stats.items()
        ],
        "items": items,
    }


def _empty_graph() -> dict[str, Any]:
    return {
        "trade_date": None,
        "synced_at": None,
        "stats": {"nodes": 0, "members": 0, "edges": 0, "cross_source_nodes": 0},
        "sources": [
            {"id": key, "label": label, "edges": 0, "nodes": 0, "members": 0}
            for key, label in SOURCE_LABELS.items()
        ],
        "items": [],
    }


def _concept_source(concept_id: str) -> str:
    if concept_id.startswith("DC:"):
        return "dc"
    if concept_id.startswith("TDX:"):
        return "tdx"
    return "kpl"


def _canonical_key(name: str) -> str:
    value = re.sub(r"\s+", "", name.strip().casefold())
    for suffix in ("概念题材", "题材概念", "概念", "题材"):
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[: -len(suffix)]
            break
    return value or name.strip().casefold()


def _node_matches(node: dict[str, Any], members: list[dict[str, Any]], keyword: str) -> bool:
    if keyword in str(node["name"]).casefold():
        return True
    for member in members:
        if keyword in str(member["code"]).casefold() or keyword in str(member["name"]).casefold():
            return True
        if keyword in str(member.get("industry") or "").casefold():
            return True
        if any(keyword in str(item.get("text") or "").casefold() for item in member["reasons"]):
            return True
    return False


def _source_order(source: str) -> int:
    return {"kpl": 0, "dc": 1, "tdx": 2}.get(source, 9)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
