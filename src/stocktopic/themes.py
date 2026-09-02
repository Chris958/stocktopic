from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .db import Database
from .domain import CandidateStatus
from .theme_graph import (
    install_graph_first_ai_clustering,
    is_broad_parent_tag,
    structured_event_clusters,
)
from .theme_intelligence import cluster_metrics, discovery_stage


class ThemeDiscovery:
    """Discover early theme resonance while keeping four touches as formal gate."""

    def __init__(
        self,
        database: Database,
        minimum_limit_touches: int = 2,
        minimum_early_touches: int = 2,
    ):
        self.database = database
        # The service still passes this legacy setting as its discovery floor.
        # Formal confirmation remains hard-coded at four touches in discovery_stage
        # and in the graph-first AI verifier.
        self.minimum_limit_touches = max(2, minimum_limit_touches)
        self.minimum_early_touches = max(2, minimum_early_touches)
        install_graph_first_ai_clustering()

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
            # Even when AI is disabled/unavailable, use the persisted graph tags
            # attached by Database.limit_touch_events before falling back to raw KPL tags.
            graph_events = self.database.limit_touch_events(trade_date)
            eligible = structured_event_clusters(
                graph_events,
                minimum_members=self.minimum_early_touches,
            )
            if not eligible:
                eligible = self.database.kpl_theme_clusters(trade_date)

        prepared: list[dict[str, Any]] = []
        for raw in eligible:
            item = dict(raw)
            if is_broad_parent_tag(item.get("tag")):
                continue
            touch_count = int(item.get("touch_count") or 0)
            if touch_count < self.minimum_early_touches:
                continue
            metrics = cluster_metrics(item)
            stage = discovery_stage(
                touch_count,
                float(metrics["synchronization_score"]),
                float(item.get("cluster_confidence") or 0),
            )
            if stage == "noise":
                continue
            item["intelligence_metrics"] = metrics
            item["discovery_stage"] = stage
            prepared.append(item)

        prepared.sort(
            key=lambda item: (
                1 if item.get("discovery_stage") == "formal_candidate" else 0,
                float(item.get("intelligence_metrics", {}).get("synchronization_score") or 0),
                float(item.get("cluster_confidence") or 0),
                int(item.get("touch_count") or 0),
                int(item.get("sealed_count") or 0),
                -int(item.get("failed_count") or 0),
            ),
            reverse=True,
        )

        candidate_ids: list[int] = []
        for item in prepared:
            item["codes"] = {str(member["code"]) for member in item["members"]}
            tag = str(item["tag"])
            member_reasons = dict(item.get("member_reasons") or {})
            metrics = dict(item.get("intelligence_metrics") or {})
            stage = str(item.get("discovery_stage") or "early_observation")

            # Keep the fingerprint stable while a 2-3 stock early cluster grows into
            # a >=4-stock formal candidate on the same trading day. Formal AI output
            # preserves this graph anchor tag and only refines canonical_name/logic.
            fingerprint = hashlib.sha256(
                f"event-cluster-v4|{trade_date}|{tag}".encode()
            ).hexdigest()
            ordered = sorted(
                item["members"],
                key=lambda member: (
                    _signal_rank(member.get("board_tag")),
                    _clock_sort(member.get("limit_up_time")),
                ),
            )
            members = [
                {
                    "code": str(member["code"]),
                    "name": str(member["name"]),
                    "membership_source": "same_day_market_strength",
                    "evidence": {
                        "shared_tag": tag,
                        "source_themes": member.get("themes", []),
                        "concept_tags": member.get("concept_tags", []),
                        "board_tag": member.get("board_tag"),
                        "board_status": member.get("status"),
                        "limit_reason": member.get("limit_reason"),
                        "aggregated_reason": member_reasons.get(str(member["code"])),
                        "first_limit_time": member.get("limit_up_time"),
                        "last_limit_time": member.get("last_limit_time"),
                        "failed_open_time": member.get("open_time"),
                        "trade_date": trade_date,
                        "cluster_method": item.get("cluster_method", "exact_tag"),
                        "common_logic": item.get("common_logic"),
                        "discovery_stage": stage,
                        "synchronization_score": metrics.get("synchronization_score"),
                        "breadth_score": metrics.get("breadth_score"),
                        "median_pct": metrics.get("median_pct"),
                    },
                }
                for member in ordered
            ]
            common_logic = str(item.get("common_logic") or "").strip()
            method = str(item.get("cluster_method") or "exact_tag")
            grouping_reason = (
                "先由结构化题材图谱聚合，再由外部新闻验证共同事件。"
                if method == "semantic_event"
                else "由结构化题材知识图谱确定性归并，未使用外部搜索创造成员。"
            )
            stage_reason = (
                "已达到4只触板正式确认门槛，进入AI准入审查。"
                if stage == "formal_candidate"
                else "当前为2-3只异常共振的早期观察层，不调用联网AI准入。"
            )
            reason = (
                f"{trade_date}共同事件“{tag}”有{item['touch_count']}只股票形成强势共识；"
                f"封板{item['sealed_count']}只、创业板涨幅超10%"
                f"{int(item.get('growth_count') or 0)}只、炸板{item['failed_count']}只。"
                f"共振度{float(metrics.get('synchronization_score') or 0):.1f}，"
                f"广度{float(metrics.get('breadth_score') or 0):.1f}，"
                f"中位数涨幅{float(metrics.get('median_pct') or 0):.2f}%。"
                + stage_reason
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
            admission_status = (
                "awaiting_ai" if stage == "formal_candidate" else "early_observation"
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
                admission_status=admission_status,
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
    """Expose only decision-relevant immutable context to the news explainer."""
    return {
        "theme_id": theme["id"],
        "provisional_name": theme["provisional_name"],
        "shared_tag": theme["shared_tag"],
        "discovery_reason": str(theme["discovery_reason"])[:700],
        "stocks_read_only": [
            {
                "code": member["code"],
                "name": member["name"],
                "evidence": {
                    key: value
                    for key, value in {
                        "board_tag": member.get("evidence", {}).get("board_tag"),
                        "limit_reason": str(
                            member.get("evidence", {}).get("limit_reason") or ""
                        )[:240],
                        "aggregated_reason": str(
                            member.get("evidence", {}).get("aggregated_reason") or ""
                        )[:300],
                        "source_themes": list(
                            member.get("evidence", {}).get("source_themes") or []
                        )[:8],
                        "trade_date": member.get("evidence", {}).get("trade_date"),
                        "synchronization_score": member.get("evidence", {}).get(
                            "synchronization_score"
                        ),
                        "breadth_score": member.get("evidence", {}).get("breadth_score"),
                        "median_pct": member.get("evidence", {}).get("median_pct"),
                    }.items()
                    if value is not None and value != "" and value != [] and value != ()
                },
            }
            for member in theme.get("members", [])
            if member.get("active", 1)
        ],
        "constraint": "The stocks_read_only list is immutable for this explanation call.",
    }


def _clock_sort(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits.zfill(6)


def _signal_rank(value: Any) -> int:
    return {"涨停": 0, "创业板涨幅超10%": 1, "炸板": 2}.get(str(value), 9)
