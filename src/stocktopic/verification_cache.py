"""Persistent, per-candidate AI research reuse; stored in existing metadata."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from .market_clock import MarketClock


class VerificationCache:
    def __init__(self, database, now: datetime):
        self.db = database
        self.now = MarketClock.normalize(now)

    def identity(self, day: str, kind: str, node: dict, events: list[dict], model: str) -> str:
        codes = sorted(set(node.get("member_codes") or []))
        payload = {
            "policy": "mainboard-research-v1", "model": model, "day": day, "kind": kind,
            "node": {k: node.get(k) for k in (
                "tag", "anchor_tag", "verification_route", "nodes", "node_members",
                "member_reasons", "known_direction_id",
                "known_formal", "known_status", "archive_epoch",
            )},
            "codes": codes,
            "graph_evidence": [{k: n.get(k) for k in ("tag", "member_reasons")}
                               for n in node.get("local_nodes", [])],
            # Price, live board status, ingestion timestamps and unrelated graph tags
            # are not a new business relationship. The detector still gates every run.
            "events": sorted([
                {k: e.get(k) for k in (("code", "limit_reason", "limit_up_time", "limit_time_basis")
                                        if kind == "bridge" else ("code", "limit_reason"))}
                for e in events if e.get("code") in codes
            ], key=lambda e: e["code"]),
        }
        digest = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()[:32]
        return f"verification_v1:{day}:{kind}:{digest}"

    def lookup(self, key: str) -> dict | None:
        raw = self.db.get_metadata(key)
        if not raw:
            return None
        item = json.loads(raw)
        if item["status"] == "accepted":
            return item
        # Old-day replays never periodically repeat research; changed evidence
        # produces a different key and can still be processed.
        current_day = self.now.strftime("%Y%m%d") == key.split(":")[1]
        attempts = int(item.get("attempts", 1))
        last = MarketClock.normalize(datetime.fromisoformat(item["at"]))
        if item["status"] == "rejected":
            due = attempts < 3 and self.now - last >= timedelta(minutes=120)
        else:
            due = attempts < 3 and self.now - last >= timedelta(minutes=30 * 2 ** (attempts-1))
        if not current_day or not due:
            return item
        if MarketClock.session_name(self.now) not in {"morning", "afternoon"}:
            return item
        return None

    def save(self, key: str, status: str, result: Any, decision: dict) -> None:
        old = json.loads(self.db.get_metadata(key) or "{}")
        self.db.set_metadata(key, json.dumps({
            "at": self.now.isoformat(), "status": status, "result": result,
            "decision": decision, "attempts": int(old.get("attempts", 0)) + 1,
        }, ensure_ascii=False))

    def audit(self, day: str, kind: str, nodes: list[dict], keys: list[str]) -> str:
        digest = hashlib.sha256("|".join(keys).encode()).hexdigest()[:20]
        subject = f"{day}:{kind}:{digest}"
        self.db.set_metadata(f"ai_request_context:{subject}", json.dumps({
            "trade_date": day, "kind": kind, "cache_keys": keys,
            "nodes": [n.get("tag") or n.get("nodes") for n in nodes],
            "member_codes": sorted({c for n in nodes for c in n.get("member_codes", [])}),
            "requested_at": self.now.isoformat(),
            "trigger": "new_or_changed_candidate_or_due_review",
        }, ensure_ascii=False))
        return subject
