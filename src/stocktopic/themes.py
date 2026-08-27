from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .db import Database
from .domain import CandidateStatus


class ThemeDiscovery:
    """Deterministic tag clustering. AI is intentionally absent from membership decisions."""

    def __init__(
        self,
        database: Database,
        minimum_severity: float = 65.0,
        maximum_candidates: int = 8,
    ):
        self.database = database
        self.minimum_severity = minimum_severity
        self.maximum_candidates = maximum_candidates

    def discover(self, now: datetime, lookback_minutes: int = 20) -> list[int]:
        since = now - timedelta(minutes=lookback_minutes)
        anomalies = [
            item
            for item in self.database.recent_anomalies(since, direction="positive")
            if item.get("is_hard_event")
            or float(item.get("severity") or 0) >= self.minimum_severity
        ]
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
                            "is_hard_event": bool(anomaly.get("is_hard_event")),
                        },
                    }
                )
        created: list[int] = []
        eligible: list[dict[str, Any]] = []
        for (tag_type, tag), members in clusters.items():
            unique_members = {member["code"]: member for member in members}
            minimum = 4 if tag_type == "industry" else 3
            if len(unique_members) < minimum:
                continue
            ordered_members = sorted(
                unique_members.values(),
                key=lambda item: float(item["evidence"].get("severity") or 0),
                reverse=True,
            )
            severities = [
                float(member["evidence"].get("severity") or 0)
                for member in ordered_members
            ]
            hard_count = sum(
                bool(member["evidence"].get("is_hard_event"))
                for member in ordered_members
            )
            average_severity = sum(severities) / len(severities)
            if hard_count == 0 and average_severity < 75:
                continue
            strength = (
                hard_count * 24
                + sum(severities[:6]) / 6
                + min(18, len(ordered_members) * 2)
                + (8 if tag_type == "theme" else 0)
            )
            eligible.append(
                {
                    "tag_type": tag_type,
                    "tag": tag,
                    "members": ordered_members,
                    "codes": set(unique_members),
                    "hard_count": hard_count,
                    "average_severity": average_severity,
                    "strength": strength,
                }
            )

        specific_sets = [
            item["codes"] for item in eligible if item["tag_type"] != "industry"
        ]
        eligible.sort(key=lambda item: float(item["strength"]), reverse=True)
        selected: list[dict[str, Any]] = []
        for item in eligible:
            tag_type = str(item["tag_type"])
            member_codes = item["codes"]
            if tag_type == "industry" and any(
                len(member_codes & specific) / len(member_codes) >= 0.6
                for specific in specific_sets
            ):
                # Prefer the smaller, more specific shared logic over a broad industry label.
                continue
            if any(
                len(member_codes & previous["codes"])
                / max(1, min(len(member_codes), len(previous["codes"])))
                >= 0.72
                for previous in selected
            ):
                continue
            selected.append(item)
            if len(selected) >= self.maximum_candidates:
                break

        for item in selected:
            tag_type = str(item["tag_type"])
            tag = str(item["tag"])
            ordered_members = item["members"]
            fingerprint = self.database.recent_live_theme_fingerprint(
                tag, "positive", now - timedelta(days=10)
            ) or hashlib.sha256(
                f"{now.date().isoformat()}|positive|{tag_type}|{tag}".encode()
            ).hexdigest()
            reason = (
                f"最近{lookback_minutes}分钟内有{len(ordered_members)}只异动股票"
                f"共享{tag_type}标签“{tag}”；高强度事件{item['hard_count']}只，"
                f"平均异动强度{item['average_severity']:.1f}；成员由标签与异动规则确定"
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
