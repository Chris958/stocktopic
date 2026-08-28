from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .db import Database
from .domain import CandidateStatus


class ThemeDiscovery:
    """Discover only shared themes with enough same-day limit-up touches."""

    def __init__(
        self,
        database: Database,
        minimum_limit_touches: int = 4,
        maximum_candidates: int = 4,
    ):
        self.database = database
        self.minimum_limit_touches = minimum_limit_touches
        self.maximum_candidates = maximum_candidates

    def discover(self, now: datetime) -> list[int]:
        compact = now.strftime("%Y%m%d")
        eligible = [
            item
            for item in self.database.kpl_theme_clusters(compact)
            if int(item["touch_count"]) >= self.minimum_limit_touches
        ]
        eligible.sort(
            key=lambda item: (
                int(item["touch_count"]),
                int(item["sealed_count"]),
                -int(item["failed_count"]),
            ),
            reverse=True,
        )

        selected: list[dict[str, Any]] = []
        for item in eligible:
            codes = {str(member["code"]) for member in item["members"]}
            if any(
                len(codes & previous["codes"]) / max(1, min(len(codes), len(previous["codes"])))
                >= 0.75
                for previous in selected
            ):
                continue
            item["codes"] = codes
            selected.append(item)
            if len(selected) >= self.maximum_candidates:
                break

        candidate_ids: list[int] = []
        for item in selected:
            tag = str(item["tag"])
            fingerprint = hashlib.sha256(
                f"limit-touch-v2|{now.date().isoformat()}|{tag}".encode()
            ).hexdigest()
            ordered = sorted(
                item["members"],
                key=lambda member: (
                    member.get("board_tag") != "涨停",
                    _clock_sort(member.get("limit_up_time")),
                ),
            )
            members = [
                {
                    "code": str(member["code"]),
                    "name": str(member["name"]),
                    "membership_source": "same_day_limit_touch",
                    "evidence": {
                        "shared_tag": tag,
                        "board_tag": member.get("board_tag"),
                        "board_status": member.get("status"),
                        "limit_reason": member.get("limit_reason"),
                        "first_limit_time": member.get("limit_up_time"),
                        "last_limit_time": member.get("last_limit_time"),
                        "failed_open_time": member.get("open_time"),
                        "trade_date": compact,
                    },
                }
                for member in ordered
            ]
            reason = (
                f"当日同属“{tag}”的股票中有{item['touch_count']}只曾触及涨停；"
                f"当前封板{item['sealed_count']}只、炸板{item['failed_count']}只。"
                "已达到AI新颖性与持续性分析门槛。"
            )
            theme_id = self.database.upsert_candidate(
                fingerprint=fingerprint,
                provisional_name=f"{tag}待审",
                shared_tag=tag,
                direction="positive",
                discovered_at=now.isoformat(timespec="seconds"),
                day1_date=now.date().isoformat(),
                discovery_reason=reason,
                members=members,
                admission_status="awaiting_ai",
            )
            theme = self.database.get_theme(theme_id)
            if (
                theme
                and theme.get("status") == "pending"
                and theme.get("admission_status") == "awaiting_ai"
            ):
                candidate_ids.append(theme_id)
        return candidate_ids

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
    """Expose existing members as immutable context to the news explainer."""
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
        "constraint": "The stocks_read_only list is immutable for this explanation call.",
    }


def _clock_sort(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits.zfill(6)
