from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from .db import Database
from .theme_intelligence import (
    core_stock_structure,
    counter_evidence,
    lifecycle_stage,
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

        recent = self.database.recent_anomalies(now - timedelta(minutes=45))
        member_events = [event for event in recent if event["code"] in codes]
        kpl_events = self.database.kpl_events_for_codes(now.strftime("%Y%m%d"), codes)
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
                str(theme["day1_date"]).replace("-", ""), now.strftime("%Y%m%d")
            ),
        )
        catalyst_strength = float(theme.get("catalyst_strength") or 0.0)
        cohort_stats = self.database.cohort_stats(int(theme["id"]))
        avg_next_day_return = float(cohort_stats.get("avg_next_day_return") or 0.0)
        cohort_loss_ratio = float(cohort_stats.get("loss_ratio") or 0.0)

        # Prefer the discovery-layer synchronization metric persisted in member evidence.
        sync_values = [
            float(member.get("evidence", {}).get("synchronization_score") or 0)
            for member in members
            if member.get("evidence", {}).get("synchronization_score") is not None
        ]
        synchronization_score = max(sync_values, default=50.0)

        # Breadth now uses the median return as the central statistic; the mean is
        # intentionally excluded so one or two limit-up stocks cannot inflate the theme.
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
            + min(15.0, catalyst_strength * 0.15)
            + max(-10.0, min(10.0, avg_next_day_return * 2.0))
            + max(0.0, 5.0 - negative_ratio * 20.0)
            - failed_count * 3.0
        )

        market_environment = theme.get("market_environment") or {}
        market_regime = str(market_environment.get("label") or "震荡")
        market_penalty = {"主升": -6, "修复": -2, "震荡": 0, "退潮": 10, "冰点": 18}.get(
            market_regime, 0
        )
        age_risk = min(20.0, max(0, day_number - 1) * 4.0)
        climax_risk = max(0.0, heat - 70.0) * 0.5
        negative = counter_evidence(
            median_pct=median_pct,
            negative_ratio=negative_ratio,
            failed_count=failed_count,
            concentration=concentration,
            divergence=divergence,
            market_regime=market_regime,
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
            + (10.0 if catalyst_strength > 0 else 0.0)
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
            "counter_evidence": negative,
            "market_environment": {
                "label": market_regime,
                "score": market_environment.get("score"),
                "risk_adjustment": market_penalty,
            },
            "cohort_observations": int(cohort_stats.get("observations") or 0),
            "cohort_avg_next_day_return": round(avg_next_day_return, 3),
            "cohort_loss_ratio": round(cohort_loss_ratio, 3),
            "score_limitations": [
                "共振度优先使用题材发现时的触板时间密度；缺失时回退为中性值",
                "市场环境在全市场情绪快照接入前回退为震荡，不人为放大机会",
                "催化质量分需要结构化source_kind/新颖度/产业影响字段后才能充分发挥",
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


def _board_height(status: Any) -> int:
    value = str(status or "")
    if "首板" in value:
        return 1
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0
