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
    ):
        self.database = database
        self.minimum_limit_touches = minimum_limit_touches

    def discover(self, now: datetime) -> list[int]:
        return self.discover_for_date(now.strftime("%Y%m%d"), now)

    def discover_for_date(
        self,
        trade_date: str,
        now: datetime,
        semantic_clusters: list[dict[str, Any]] | None = None,
    ) -> list[int]:
        eligible = semantic_clusters
        if eligible is None:
            eligible = self.database.kpl_theme_clusters(trade_date)
        eligible = [
            item
            for item in eligible
            if int(item.get("touch_count") or 0) >= self.minimum_limit_touches
        ]
        eligible.sort(
            key=lambda item: (
                float(item.get("cluster_confidence") or 0),
                int(item.get("touch_count") or 0),
                int(item.get("sealed_count") or 0),
                -int(item.get("failed_count") or 0),
            ),
            reverse=True,
        )

        candidate_ids: list[int] = []
        for item in eligible:
            item["codes"] = {str(member["code"]) for member in item["members"]}
            tag = str(item["tag"])
            fingerprint = hashlib.sha256(
                (
                    f"event-cluster-v3|{trade_date}|{tag}|"
                    + ",".join(sorted(item["codes"]))
                ).encode()
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
                        "source_themes": member.get("themes", []),
                        "concept_tags": member.get("concept_tags", []),
                        "board_tag": member.get("board_tag"),
                        "board_status": member.get("status"),
                        "limit_reason": member.get("limit_reason"),
                        "first_limit_time": member.get("limit_up_time"),
                        "last_limit_time": member.get("last_limit_time"),
                        "failed_open_time": member.get("open_time"),
                        "trade_date": trade_date,
                        "cluster_method": item.get("cluster_method", "exact_tag"),
                        "common_logic": item.get("common_logic"),
                    },
                }
                for member in ordered
            ]
            common_logic = str(item.get("common_logic") or "").strip()
            method = str(item.get("cluster_method") or "exact_tag")
            grouping_reason = (
                "由涨停原因、题材标签、题材成分与新闻催化语义归并。"
                if method == "semantic_event"
                else "由开盘啦共同标签归并。"
            )
            reason = (
                f"{trade_date}共同事件“{tag}”有{item['touch_count']}只股票曾触及涨停；"
                f"封板{item['sealed_count']}只、炸板{item['failed_count']}只。"
                + (f"共同逻辑：{common_logic}。" if common_logic else "")
                + grouping_reason
            )
            day1 = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            discovered = now
            if trade_date != now.strftime("%Y%m%d"):
                discovered = now.replace(
                    year=int(trade_date[:4]),
                    month=int(trade_date[4:6]),
                    day=int(trade_date[6:8]),
                    hour=15,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            theme_id = self.database.upsert_candidate(
                fingerprint=fingerprint,
                provisional_name=f"{tag}待审",
                shared_tag=tag,
                direction="positive",
                discovered_at=discovered.isoformat(timespec="seconds"),
                day1_date=day1,
                discovery_reason=reason,
                members=members,
                admission_status="awaiting_ai",
                cluster_method=method,
                cluster_confidence=float(item.get("cluster_confidence") or 0),
                cluster_aliases=item.get("aliases") or [tag],
            )
            self.database.save_theme_catalysts(theme_id, item.get("catalysts") or [])
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
