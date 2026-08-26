from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .db import Database
from .domain import CandidateStatus


class ThemeDiscovery:
    """Deterministic tag clustering. AI is intentionally absent from membership decisions."""

    def __init__(self, database: Database):
        self.database = database

    def discover(self, now: datetime, lookback_minutes: int = 20) -> list[int]:
        since = now - timedelta(minutes=lookback_minutes)
        anomalies = self.database.recent_anomalies(since, direction="positive")
        latest_by_code: dict[str, dict[str, Any]] = {}
        for anomaly in anomalies:
            latest_by_code.setdefault(str(anomaly["code"]), anomaly)
        if len(latest_by_code) < 3:
            return []
        tags_by_code = self.database.tags_for_codes(list(latest_by_code))
        clusters: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for code, anomaly in latest_by_code.items():
            for tag in tags_by_code.get(code, []):
                if float(tag.get("confidence", 0)) < 0.45:
                    continue
                clusters[(str(tag["tag_type"]), str(tag["tag"]))].append(
                    {
                        "code": code,
                        "name": anomaly["name"],
                        "evidence": {
                            "tag": tag["tag"],
                            "tag_type": tag["tag_type"],
                            "tag_source": tag["source"],
                            "anomaly_reasons": anomaly["reasons"],
                            "severity": anomaly["severity"],
                        },
                    }
                )
        created: list[int] = []
        eligible: list[tuple[str, str, list[dict[str, Any]]]] = []
        for (tag_type, tag), members in clusters.items():
            unique_members = {member["code"]: member for member in members}
            minimum = 4 if tag_type == "industry" else 3
            if len(unique_members) < minimum:
                continue
            ordered_members = [unique_members[code] for code in sorted(unique_members)]
            eligible.append((tag_type, tag, ordered_members))

        specific_sets = [
            {member["code"] for member in members}
            for tag_type, _, members in eligible
            if tag_type != "industry"
        ]
        for tag_type, tag, ordered_members in eligible:
            member_codes = {member["code"] for member in ordered_members}
            if tag_type == "industry" and any(
                len(member_codes & specific) / len(member_codes) >= 0.6
                for specific in specific_sets
            ):
                # Prefer the smaller, more specific shared logic over a broad industry label.
                continue
            fingerprint = self.database.recent_live_theme_fingerprint(
                tag, "positive", now - timedelta(days=10)
            ) or hashlib.sha256(
                f"{now.date().isoformat()}|positive|{tag_type}|{tag}".encode()
            ).hexdigest()
            reason = (
                f"最近{lookback_minutes}分钟内有{len(ordered_members)}只异动股票"
                f"共享{tag_type}标签“{tag}”；成员由标签与异动规则确定"
            )
            theme_id = self.database.upsert_candidate(
                fingerprint=fingerprint,
                provisional_name=f"{tag}异动",
                shared_tag=tag,
                direction="positive",
                discovered_at=now.isoformat(timespec="seconds"),
                day1_date=now.date().isoformat(),
                discovery_reason=reason,
                members=ordered_members,
            )
            created.append(theme_id)
        return created

    def confirm(
        self,
        theme_id: int,
        final_name: str | None = None,
        catalyst_strength: float | None = None,
        catalyst_duration: str | None = None,
    ) -> None:
        self.database.set_theme_status(
            theme_id,
            CandidateStatus.CONFIRMED.value,
            final_name=final_name,
            catalyst_strength=catalyst_strength,
            catalyst_duration=catalyst_duration,
        )

    def reject(self, theme_id: int) -> None:
        self.database.set_theme_status(theme_id, CandidateStatus.REJECTED.value)

    def merge(self, target_id: int, source_ids: list[int]) -> None:
        self.database.merge_themes(target_id, source_ids)

    def split(self, source_id: int, member_codes: list[str], new_name: str) -> int:
        fingerprint = hashlib.sha256(
            f"split|{source_id}|{datetime.now().isoformat()}|{','.join(sorted(member_codes))}".encode()
        ).hexdigest()
        return self.database.split_theme(source_id, member_codes, new_name, fingerprint)


def candidate_for_ai(theme: dict[str, Any]) -> dict[str, Any]:
    """Remove mutable/database fields and make the LLM boundary explicit."""
    return {
        "theme_id": theme["id"],
        "provisional_name": theme["provisional_name"],
        "shared_tag": theme["shared_tag"],
        "discovery_reason": theme["discovery_reason"],
        "stocks_read_only": [
            {
                "code": member["code"],
                "name": member["name"],
                "evidence": member["evidence"],
            }
            for member in theme.get("members", [])
            if member.get("active", 1)
        ],
        "constraint": "The stocks_read_only list is immutable. Do not add or remove stocks.",
    }
