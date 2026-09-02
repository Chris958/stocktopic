from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from .db import Database
from .theme_intelligence import (
    catalyst_quality,
    core_stock_structure,
    counter_evidence,
    lifecycle_stage,
    market_regime as classify_market_regime,
)


def clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


class ThemeScorer:
    def __init__(self, database: Database):
        self.database = database

    def score_confirmed(self, now: datetime) -> list[int]:
        scored: list[int] = []
        for theme in self.database.list_themes(status="confirmed"):
            score = self.calculate(theme, now)
            if score:
                self.database.save_score(int(theme["id"]), score)
                scored.append(int(theme["id"]))
        return scored

    def calculate(self, theme: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if theme.get("status") != "confirmed":
            return None
        members = [member for member in theme.get("members", []) if member.get("active", 1)]
        codes = [str(member["code"]) for member in members]
        if not codes:
            return None
        quotes = self.database.latest_quotes(codes)
        if not quotes:
            return None

        pct = [float(quote["pct_change"]) for quote in quotes]
        leader = max(quotes, key=lambda quote: float(quote["pct_change"]))
        leader_pct = float(leader["pct_change"])
        follower_pct = [float(q["pct_change"]) for q in quotes if q["code"] != leader["code"]]
        follower_average = mean(follower_pct) if follower_pct else 0.0
        median_pct = median(pct)
        strong_count = sum(value >= 5.0 for value in pct)
        limit_like_count = sum(value >= 9.5 for value in pct)
        negative_count = sum(value <= -5.0 for value in pct)
        up_count = sum(value > 0 for value in pct)
        breadth_ratio = up_count / len(pct)
        strong_ratio = strong_count / len(pct)
        negative_ratio = negative_count / len(pct)
        concentration = max(0.0, leader_pct) / max(1.0, sum(max(0.0, value) for value in pct))

        trade_date = now.strftime("%Y%m%d")
        recent = self.database.recent_anomalies(now - timedelta(minutes=45))
        member_events = [event for event in recent if event["code"] in codes]
        kpl_events = self.database.kpl_events_for_codes(trade_date, codes)
        kpl_limit_count = sum(
            event["board_tag"] in {"涨停", "创业板涨幅超10%"} for event in kpl_events
        )
        kpl_failed_count = sum(event["board_tag"] == "炸板" for event in kpl_events)
        kpl_down_count = sum(event["board_tag"] == "跌停" for event in kpl_events)
        failed_count = max(
            kpl_failed_count,
            sum("failed_limit" in event["event_types"] for event in member_events),
        )
        if kpl_events:
            limit_like_count = kpl_limit_count
            negative_count = max(negative_count, kpl_down_count)
            negative_ratio = negative_count / len(pct)
        max_board_height = max(
            (_board_height(event.get("status")) for event in kpl_events), default=0
        )
        hard_positive = sum(
            event["direction"] == "positive" and event["is_hard_event"] for event in member_events
        )
        divergence = leader_pct >= 8.0 and median_pct <= 1.5

        day_number = max(
            1,
            self.database.count_open_days(
                str(theme["day1_date"]).replace("-", ""), trade_date
            ),
        )
        legacy_catalyst_strength = float(theme.get("catalyst_strength") or 0.0)
        catalyst_score = self._catalyst_score(theme)
        effective_catalyst_strength = max(legacy_catalyst_strength, catalyst_score["score"])
        cohort_stats = self.database.cohort_stats(int(theme["id"]))
        avg_next_day_return = float(cohort_stats.get("avg_next_day_return") or 0.0)
        cohort_loss_ratio = float(cohort_stats.get("loss_ratio") or 0.0)

        sync_values = [
            float(member.get("evidence", {}).get("synchronization_score") or 0)
            for member in members
            if member.get("evidence", {}).get("synchronization_score") is not None
        ]
        synchronization_score = max(sync_values, default=50.0)

        # Median return is the central breadth statistic. This prevents one or two
        # limit-up stocks from making a weak theme look broad and healthy.
        breadth_score = clamp(
            breadth_ratio * 45
            + strong_ratio * 30
            + clamp((median_pct + 2.0) * 8.0) * 0.25
        )
        resonance_score = clamp(
            synchronization_score * 0.55
            + breadth_score * 0.30
            + min(15.0, limit_like_count / max(1, len(codes)) * 30)
        )

        heat = clamp(
            min(24.0, limit_like_count / 6 * 24)
            + min(14.0, strong_count / 10 * 14)
            + breadth_score * 0.24
            + resonance_score * 0.18
            + min(10.0, hard_positive * 2.5)
            + min(10.0, len(codes) / 12 * 10)
        )
        recurrence = min(20.0, day_number / 5 * 20)
        ladder_proxy = min(
            20.0,
            (limit_like_count * 3 + strong_count + max_board_height * 2) / 16 * 20,
        )
        persistence = clamp(
            min(16.0, len(codes) / 15 * 16)
            + breadth_score * 0.22
            + resonance_score * 0.16
            + recurrence
            + ladder_proxy
            + min(15.0, effective_catalyst_strength * 0.15)
            + max(-10.0, min(10.0, avg_next_day_return * 2.0))
            + max(0.0, 5.0 - negative_ratio * 20.0)
            - failed_count * 3.0
        )

        market_environment = self._market_environment(trade_date)
        market_label = str(market_environment.get("label") or "震荡")
        market_penalty = {"主升": -6, "修复": -2, "震荡": 0, "退潮": 10, "冰点": 18}.get(
            market_label, 0
        )
        age_risk = min(20.0, max(0, day_number - 1) * 4.0)
        climax_risk = max(0.0, heat - 70.0) * 0.5
        negative = counter_evidence(
            median_pct=median_pct,
            negative_ratio=negative_ratio,
            failed_count=failed_count,
            concentration=concentration,
            divergence=divergence,
            market_regime=market_label,
        )
        entry_risk = clamp(
            age_risk
            + climax_risk
            + negative_ratio * 22.0
            + cohort_loss_ratio * 15.0
            + min(18.0, failed_count * 4.5)
            + max(0.0, concentration - 0.45) * 25.0
            + float(negative["score"]) * 0.28
            + market_penalty
        )
        lifecycle = lifecycle_stage(
            day_number=day_number,
            heat=heat,
            entry_risk=entry_risk,
            divergence=divergence,
            negative_ratio=negative_ratio,
            synchronization_score=synchronization_score,
            median_pct=median_pct,
        )
        confidence = clamp(
            40.0
            + min(20.0, len(codes) * 2.0)
            + min(15.0, len(member_events) * 1.5)
            + resonance_score * 0.15
            + catalyst_score["truth"] * 0.08
        )

        core_structure = core_stock_structure(quotes, kpl_events)
        influence_leader = core_structure.get("influence_leader") or {}
        leader_code = influence_leader.get("code") or leader["code"]
        details = {
            "day_number": day_number,
            "member_count": len(codes),
            "median_pct": round(median_pct, 3),
            "up_count": up_count,
            "strong_count": strong_count,
            "limit_like_count": limit_like_count,
            "negative_count": negative_count,
            "failed_limit_count_45m": failed_count,
            "max_board_height": max_board_height,
            "leader_pct": round(leader_pct, 3),
            "followers_average_pct": round(follower_average, 3),
            "concentration": round(concentration, 3),
            "breadth_score": breadth_score,
            "synchronization_score": synchronization_score,
            "resonance_score": resonance_score,
            "core_stock_structure": core_structure,
            "catalyst_quality": catalyst_score,
            "counter_evidence": negative,
            "market_environment": {
                **market_environment,
                "risk_adjustment": market_penalty,
            },
            "cohort_observations": int(cohort_stats.get("observations") or 0),
            "cohort_avg_next_day_return": round(avg_next_day_return, 3),
            "cohort_loss_ratio": round(cohort_loss_ratio, 3),
            "score_limitations": [
                "市场环境V1使用当日涨停/炸板/跌停与连板结构，昨日涨停收益和晋级率待日线复盘层补齐",
                "催化新颖度目前主要依据首次催化/强化催化标签，后续由准入AI补充90日历史新颖度",
            ],
        }
        return {
            "calculated_at": now.isoformat(timespec="seconds"),
            "heat": heat,
            "persistence": persistence,
            "entry_risk": entry_risk,
            "lifecycle": lifecycle,
            "confidence": confidence,
            "leader_code": leader_code,
            "leader_influence": influence_leader.get("influence_score"),
            "leader_theme_divergence": divergence,
            "details": details,
        }

    def _catalyst_score(self, theme: dict[str, Any]) -> dict[str, Any]:
        source_map = {
            "official": "central_policy",
            "company_disclosure": "company_announcement",
            "industry_primary": "official_media",
            "authoritative_media": "official_media",
            "brokerage_research": "broker_research",
            "supply_chain_report": "industry_media",
            "social": "social",
        }
        truth_map = {
            "官方确认": 98,
            "多源交叉验证": 86,
            "供应链未确认": 62,
            "明确证据": 90,
            "合理推断": 48,
        }
        items = []
        for catalyst in theme.get("catalysts", [])[:12]:
            catalyst_type = str(catalyst.get("catalyst_type") or "")
            source_kind = str(catalyst.get("source_kind") or "")
            evidence = str(catalyst.get("evidence_level") or "")
            items.append(
                {
                    "source_tier": source_map.get(source_kind, "industry_media"),
                    "truth_score": truth_map.get(evidence, 55),
                    "novelty_score": 88 if catalyst_type == "首次催化" else 68 if catalyst_type == "强化催化" else 35,
                    "impact_score": 75 if source_kind in {"official", "company_disclosure", "industry_primary"} else 58,
                    "duration_score": _duration_score(theme.get("catalyst_duration")),
                }
            )
        return catalyst_quality(items)

    def _market_environment(self, trade_date: str) -> dict[str, Any]:
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT board_tag, status, COUNT(*) AS count
                    FROM kpl_events
                    WHERE trade_date=?
                    GROUP BY board_tag, status
                    """,
                    (trade_date,),
                ).fetchall()
        except Exception:
            rows = []
        limit_up = 0
        failed = 0
        limit_down = 0
        promoted = 0
        for row in rows:
            tag = str(row["board_tag"] or "")
            count = int(row["count"] or 0)
            if tag in {"涨停", "创业板涨幅超10%"}:
                limit_up += count
                if _board_height(row["status"]) >= 2:
                    promoted += count
            elif tag == "炸板":
                failed += count
            elif tag == "跌停":
                limit_down += count
        attempts = limit_up + failed
        seal_rate = limit_up / attempts * 100 if attempts else 0.0
        failed_rate = failed / attempts * 100 if attempts else 0.0
        promotion_rate = promoted / limit_up * 100 if limit_up else 0.0
        metrics = {
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "seal_rate": seal_rate,
            "promotion_rate": promotion_rate,
            "yesterday_limit_return": 0.0,
            "failed_rate": failed_rate,
        }
        return {**classify_market_regime(metrics), **metrics, "version": "v1-kpl-intraday"}


def _duration_score(value: Any) -> float:
    text = str(value or "")
    if "数月" in text:
        return 90
    if "数周" in text:
        return 80
    if "数日" in text:
        return 68
    if "一次" in text:
        return 35
    return 50


def _board_height(status: Any) -> int:
    value = str(status or "")
    if "首板" in value:
        return 1
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0
