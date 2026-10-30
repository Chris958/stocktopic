"""Read-only, no-network evidence export for discovery omissions.

This reports persisted decisions separately from reconstruction using today's
stored graph. It never re-runs AI, discovery, admission, ingestion or migrations.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .event_bridge import EventBridgeDetector
from .market_cycles import STATE_KEY
from .theme_policy import policy_structured_event_clusters
from .theme_taxonomy import install_theme_taxonomy


class AuditDatabase(Database):
    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        try:
            yield con
        finally:
            con.close()


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _cluster(cluster: dict) -> dict:
    return {
        key: cluster.get(key)
        for key in (
            "tag",
            "canonical_name",
            "member_codes",
            "member_reasons",
            "touch_count",
            "cluster_method",
            "parent_only",
            "parent_tags",
            "cluster_confidence",
        )
    }


def _relevant(value: Any, codes: set[str], keywords: list[str]) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return any(code in text for code in codes) or any(word in text for word in keywords)


def audit_date(db: AuditDatabase, day: str, codes: set[str], keywords: list[str]) -> dict:
    events = db.limit_touch_events(day)
    clusters = policy_structured_event_clusters(events, include_broad_parents=True)
    detector = EventBridgeDetector()
    bridges = detector.detect(day, events, clusters)
    relevant_nodes = [c for c in clusters if _relevant(c, codes, keywords)]
    tags = {c["tag"] for c in relevant_nodes}
    by_code = {event["code"]: event for event in events}
    source_state = {name: _json(db.get_metadata(f"{name}:{day}"), None)
                    for name in ("daily_limit_readiness", "market_signal_coverage",
                                 "completed_sources", "kpl_event_coverage", "kpl_concept_coverage")}
    cycle_state = _json(db.get_metadata(STATE_KEY), {})
    market_cycles = []
    for cycle in cycle_state.values():
        snapshot = next((s for s in cycle.get("snapshots", []) if s["trade_date"] == day), None)
        if snapshot and _relevant([cycle["family"], snapshot], codes, keywords):
            market_cycles.append({"cycle_id": cycle["cycle_id"], "family": cycle["family"],
                                  "start_trade_date": cycle["start_trade_date"],
                                  "snapshot": snapshot})
    with db.connect() as con:
        iso_day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        limit_rows = con.execute(
            "SELECT COUNT(*) AS stored_mainboard_count, "
            "COALESCE(SUM(CASE WHEN l.upper_limit>0 THEN 1 ELSE 0 END),0) AS valid_upper_count "
            "FROM daily_limits l JOIN stocks s ON s.code=l.code "
            "WHERE l.trade_date=? AND s.active=1 AND s.market='主板'", (iso_day,),
        ).fetchone()
        expected_main = con.execute(
            "SELECT COUNT(*) FROM stocks WHERE active=1 AND market='主板'",
        ).fetchone()[0]
        stored_limit_coverage = {
            **dict(limit_rows), "current_active_mainboard_count": expected_main,
            "basis": "current_stored_rows_not_historical_intraday_snapshot",
        }
        target_rows = []
        concept_rows = []
        if codes:
            placeholders = ",".join("?" for _ in codes)
            target_rows = [
                dict(row)
                for row in con.execute(
                    "SELECT code,name,board_tag,limit_up_time,limit_reason,themes_json,synced_at "
                    f"FROM kpl_events WHERE trade_date=? AND code IN ({placeholders})",
                    [day, *sorted(codes)],
                )
            ]
            concept_rows = [
                dict(row)
                for row in con.execute(
                    "SELECT code,concept_name,description,synced_at FROM kpl_concept_memberships "
                    f"WHERE trade_date=? AND code IN ({placeholders})",
                    [day, *sorted(codes)],
                )
            ]
        metadata = con.execute(
            "SELECT key,value,updated_at FROM metadata WHERE key LIKE ? "
            "ORDER BY updated_at DESC,key LIMIT 65",
            (f"discovery_trace:{day}:%",),
        ).fetchall()
        persisted = []
        for row in metadata[:64]:
            trace = _json(row["value"], {})
            if not isinstance(trace, dict):
                continue
            screens = [
                item
                for item in trace.get("bridge_screening", [])
                if _relevant(item, codes, keywords) or set(item.get("nodes", [])) & tags
            ]
            decisions = [
                item
                for item in trace.get("bridges", [])
                if _relevant(item, codes, keywords) or set(item.get("nodes", [])) & tags
            ]
            persisted.append(
                {
                    "signature": row["key"].rsplit(":", 1)[-1],
                    "saved_at": row["updated_at"],
                    "strong_stock_count": trace.get("strong_stock_count"),
                    "input_coverage": trace.get("input_coverage"),
                    "local_clusters": [
                        item
                        for item in trace.get("local_clusters", [])
                        if item.get("tag") in tags or _relevant(item, codes, keywords)
                    ],
                    "bridge_candidate_count": trace.get("bridge_candidate_count"),
                    "bridge_screening": screens,
                    "bridges": [
                        {
                            key: item.get(key)
                            for key in (
                                "nodes",
                                "anchor_tag",
                                "final_anchor_tag",
                                "member_codes",
                                "bridge_score",
                                "reasons",
                                "ai_bridge_decision",
                                "accepted_nodes",
                                "ai_confidence",
                                "final",
                                "final_reject_reason",
                                "canonical_name",
                                "error_type",
                                "verification_route",
                            )
                        }
                        for item in decisions
                    ],
                    "single_node_formal": trace.get("single_node_formal"),
                    "single_node_final": trace.get("single_node_final"),
                    "single_node_verifications": trace.get("single_node_verifications"),
                    "negative_cache_recheck": trace.get("negative_cache_recheck"),
                    "review_scope": trace.get("review_scope"),
                    "retained_verified_clusters": trace.get("retained_verified_clusters"),
                    "verification_failed": trace.get("verification_failed"),
                    "final_reject_reason": trace.get("final_reject_reason"),
                }
            )
        semantic = []
        for row in con.execute(
            "SELECT input_signature,created_at,status,clusters_json FROM semantic_cluster_runs "
            "WHERE trade_date=? ORDER BY created_at DESC LIMIT 65",
            (day,),
        ):
            matched = [c for c in _json(row["clusters_json"], []) if _relevant(c, codes, keywords)]
            semantic.append(
                {
                    "signature": row["input_signature"],
                    "saved_at": row["created_at"],
                    "status": row["status"],
                    "clusters": [_cluster(c) for c in matched],
                }
            )
        themes = []
        for row in con.execute(
            "SELECT id,shared_tag,provisional_name,suggested_name,final_name,status,"
            "admission_status,admission_reason,discovered_at,day1_date,cluster_method,"
            "discovery_reason,theme_level,confirmed_at,observation_at,archived_at,updated_at "
            "FROM candidate_themes WHERE day1_date BETWEEN ? AND ?",
            ((datetime.strptime(day, "%Y%m%d") - timedelta(days=120)).date().isoformat(), iso_day),
        ):
            item = dict(row)
            members = [
                dict(m)
                for m in con.execute(
                    "SELECT code,name,first_seen_at,active FROM theme_members WHERE theme_id=?",
                    (item["id"],),
                )
            ]
            if not _relevant([item, members], codes, keywords):
                continue
            item["members"] = members
            for prefix in ("direction_state", "direction_event", "direction_archive"):
                saved_direction = con.execute("SELECT value FROM metadata WHERE key=?",
                                              (f"{prefix}:{item['id']}",)).fetchone()
                item[prefix] = _json(saved_direction[0], {}) if saved_direction else {}
            daily_tracking = con.execute("SELECT value FROM metadata WHERE key=?",
                                         (f"direction_tracking:{day}:{item['id']}",)).fetchone()
            item["direction_tracking"] = _json(daily_tracking[0], {}) if daily_tracking else {}
            item["visible_in_current_default_watch_tab"] = (
                item["status"] == "watching" or (item["status"] == "pending"
                and item["admission_status"] == "early_observation")
            )
            item["is_local_early_observation"] = item["admission_status"] == "early_observation"
            item["reviews"] = [
                dict(review)
                for review in con.execute(
                    "SELECT created_at,is_new_theme,novelty_confidence,catalyst_confidence,"
                    "admitted,decision_level,decision_reason FROM theme_admission_reviews "
                    "WHERE theme_id=? ORDER BY created_at DESC LIMIT 3",
                    (item["id"],),
                )
            ]
            saved = con.execute(
                "SELECT value FROM metadata WHERE key=?", (f"admission_trace:{item['id']}",)
            ).fetchone()
            trace = _json(saved["value"], {}) if saved else {}
            item["admission_trace"] = {
                key: trace.get(key)
                for key in (
                    "novelty_mode",
                    "admission_route",
                    "novelty_required",
                    "direction_context",
                    "event_candidate_id",
                    "admission_result",
                    "checks",
                    "final_reject_reason",
                    "discovery_trace_signature",
                )
            }
            item["alerts"] = [
                dict(alert)
                for alert in con.execute(
                    "SELECT created_at,category,title,pushed_wecom FROM alerts "
                    "WHERE theme_id=? ORDER BY created_at DESC LIMIT 5",
                    (item["id"],),
                )
            ]
            themes.append(item)
    return {
        "trade_date": day,
        "strong_stock_count": len(events),
        "strong_input_counts": db.strong_input_counts(day),
        "saved_source_readiness": source_state,
        "stored_daily_limit_coverage": stored_limit_coverage,
        "market_cycles": market_cycles,
        "targets": [
            {"code": code, "in_strong_input": code in by_code, "event": by_code.get(code)}
            for code in sorted(codes)
        ],
        "stored_kpl_events": target_rows,
        "stored_concept_descriptions": concept_rows,
        "reconstructed_local_clusters": [_cluster(c) for c in relevant_nodes],
        "reconstructed_bridge_screening": [
            item for item in detector.trace if set(item.get("nodes", [])) & tags
        ],
        "reconstructed_bridges": [
            {
                key: b[key]
                for key in (
                    "anchor_tag",
                    "nodes",
                    "member_codes",
                    "bridge_score",
                    "reasons",
                    "verification_route",
                )
            }
            for b in bridges
            if _relevant(b, codes, keywords)
        ],
        "persisted_decisions": persisted,
        "persisted_trace_limit_reached": len(metadata) > 64,
        "semantic_runs": semantic[:64],
        "related_themes": themes,
    }


def build_report(path: Path, dates: list[str], names: list[str], keywords: list[str]) -> dict:
    install_theme_taxonomy()
    db = AuditDatabase(path, path.parent / "archive")
    with db.connect() as con:
        placeholders = ",".join("?" for _ in names)
        targets = (
            [
                dict(row)
                for row in con.execute(
                    f"SELECT code,name,market FROM stocks WHERE name IN ({placeholders})",
                    names,
                )
            ]
            if names
            else []
        )
    found = {item["name"] for item in targets}
    return {
        "mode": "read_only_no_ai_no_network",
        "targets": targets,
        "unresolved_names": [name for name in names if name not in found],
        "limitations": [
            "Past decisions are stored evidence; rebuilt nodes use current stored graph data.",
            "Updated event/tag rows cannot prove their exact historical intraday contents.",
            "Related themes show current state, including earlier themes within 120 calendar days.",
            "Later news, closes and added tags do not establish earlier actionable signals.",
            "Export is capped at 64 trace signatures/runs per day and 3 reviews per theme.",
            "Market cycles retain 10 observed dates; late replay is labelled by available_at.",
            "Archive timestamps do not identify who archived a theme or why.",
        ],
        "dates": [audit_date(db, day, {t["code"] for t in targets}, keywords) for day in dates],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--names", nargs="+", required=True)
    parser.add_argument("--keywords", nargs="+", default=["短剧", "漫剧", "AI影视", "版权", "出版"])
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    for day in args.dates:
        try:
            valid = len(day) == 8 and datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") == day
        except ValueError:
            valid = False
        if not valid:
            parser.error("dates must use YYYYMMDD")
    path = args.db or Settings.from_env(require_secrets=False).db_path
    print(
        json.dumps(
            build_report(path, args.dates, args.names, args.keywords), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
