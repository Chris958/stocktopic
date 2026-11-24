from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from .board_status import consecutive_boards
from .db import Database
from .market_clock import MarketClock
from .market_environment import build_market_environment
from .market_feedback import MIN_FEEDBACK_COVERAGE, fresh_trade_sample, price_board
from .market_scope import is_mainboard
from .theme_intelligence import (
    catalyst_quality,
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
        now = MarketClock.normalize(now)
        scored: list[int] = []
        themes = self.database.list_themes(status="confirmed")
        if not themes:
            return scored
        environment = build_market_environment(
            self.database, now.strftime("%Y%m%d"), as_of=now,
        )
        limits = self.database.daily_limit_map(now.date().isoformat())
        for theme in themes:
            score = self.calculate(theme, now, environment=environment, limits=limits)
            if score:
                self.database.save_score(int(theme["id"]), score)
                scored.append(int(theme["id"]))
        return scored

    def calculate(
        self, theme: dict[str, Any], now: datetime, *, environment=None, limits=None,
    ) -> dict[str, Any] | None:
        if theme.get("status") != "confirmed":
            return None
        now = MarketClock.normalize(now)
        members = [member for member in theme.get("members", [])
                   if member.get("active", 1) and is_mainboard(member["code"])]
        codes = [str(member["code"]) for member in members]
        if not codes:
            return None
        quotes = [q for q in self.database.latest_quotes(codes, trade_date=now.date().isoformat())
                  if q["close"] > 0 and q["pre_close"] > 0 and q["volume"] > 0]
        at_market = MarketClock.session_name(now) in {"morning", "afternoon"}
        if at_market:
            quotes = [q for q in quotes if fresh_trade_sample(
                now, MarketClock.normalize(datetime.fromisoformat(q["captured_at"])),
                q.get("provider_trade_time") or "",
            )]
        if not quotes:
            return None

        pct = [float(quote["pct_change"]) for quote in quotes]
        leader = max(quotes, key=lambda quote: float(quote["pct_change"]))
        leader_pct = float(leader["pct_change"])
        follower_pct = [
            float(q["pct_change"]) for q in quotes if q["code"] != leader["code"]
        ]
        follower_average = mean(follower_pct) if follower_pct else 0.0
        median_pct = median(pct)
        strong_count = sum(value >= 5.0 for value in pct)
        severe_codes = {q["code"] for q in quotes if q["pct_change"] <= -5.0}
        up_count = sum(value > 0 for value in pct)
        breadth_ratio = up_count / len(pct)
        strong_ratio = strong_count / len(pct)
        concentration = max(0.0, leader_pct) / max(
            1.0, sum(max(0.0, value) for value in pct)
        )

        trade_date = now.strftime("%Y%m%d")
        recent = self.database.recent_anomalies(now - timedelta(minutes=45))
        member_events = [event for event in recent if event["code"] in codes]
        kpl_events = self.database.kpl_events_for_codes(trade_date, codes)
        known_boards = {}
        for event in sorted(kpl_events, key=lambda e: (e["synced_at"], e["board_tag"] != "涨停")):
            known_boards[event["code"]] = event
        if limits is None:
            limits = self.database.daily_limit_map(now.date().isoformat())
        boards = {
            q["code"]: price_board(q, limits.get(q["code"], (None, None)))
            or known_boards.get(q["code"], {}).get("board_tag") for q in quotes
        }
        limit_like_count = sum(tag == "涨停" for tag in boards.values())
        failed_count = sum(tag == "炸板" for tag in boards.values())
        severe_codes.update(code for code, tag in boards.items() if tag == "跌停")
        negative_count = len(severe_codes)
        negative_ratio = negative_count / len(pct)
        max_board_height = max(
            (_board_height(known_boards.get(code, {}).get("status"))
             for code, tag in boards.items() if tag == "涨停"), default=0,
        )
        hard_positive = sum(
            event["direction"] == "positive" and event["is_hard_event"]
            for event in member_events
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
        effective_catalyst_strength = max(
            legacy_catalyst_strength, catalyst_score["score"]
        )
        # Yesterday's cohort doesn't have a realized T+1 close during the live session.
        before = self.database.previous_trade_date(trade_date) if at_market else trade_date
        cohort_stats = self.database.cohort_stats(int(theme["id"]), before or trade_date)
        observed = cohort_stats["observations"]
        expected = cohort_stats["expected_count"]
        avg_next_day_return = cohort_stats["avg_next_day_return"]
        cohort_loss_ratio = cohort_stats["loss_ratio"]
        cohort_ready = bool(expected) and observed / expected >= MIN_FEEDBACK_COVERAGE

        sync_values = [
            float(member.get("evidence", {}).get("synchronization_score") or 0)
            for member in members
            if member.get("evidence", {}).get("synchronization_score") is not None
        ]
        synchronization_score = max(sync_values, default=50.0)

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
            + max(-10.0, min(10.0, (avg_next_day_return or 0) * 2.0)) * cohort_ready
            + max(0.0, 5.0 - negative_ratio * 20.0)
            - failed_count * 3.0
        )

        market_environment = environment or build_market_environment(
            self.database, trade_date, as_of=now,
        )
        market_label = str(market_environment.get("label") or "震荡")
        market_penalty = {
            "主升": -6,
            "修复": -2,
            "震荡": 0,
            "退潮": 10,
            "冰点": 18,
        }.get(market_label, 0)
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
            + (cohort_loss_ratio or 0) * 15.0
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
            "quote_sample_count": len(quotes),
            "quote_feedback_ready": len(quotes) / len(codes) >= MIN_FEEDBACK_COVERAGE,
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
            "cohort_observations": observed,
            "cohort_expected_count": expected,
            "cohort_avg_next_day_return": avg_next_day_return,
            "cohort_loss_ratio": cohort_loss_ratio,
            "cohort_feedback_ready": cohort_ready,
            "score_limitations": [
                "催化新颖度目前主要依据首次催化/强化催化标签，"
                "并由准入AI补充90日历史新颖度",
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
            novelty_score = (
                88
                if catalyst_type == "首次催化"
                else 68
                if catalyst_type == "强化催化"
                else 35
            )
            impact_score = (
                75
                if source_kind
                in {"official", "company_disclosure", "industry_primary"}
                else 58
            )
            items.append(
                {
                    "source_tier": source_map.get(source_kind, "industry_media"),
                    "truth_score": truth_map.get(evidence, 55),
                    "novelty_score": novelty_score,
                    "impact_score": impact_score,
                    "duration_score": _duration_score(theme.get("catalyst_duration")),
                }
            )
        return catalyst_quality(items)

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
    return consecutive_boards(status)
