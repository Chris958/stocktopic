from __future__ import annotations

from collections.abc import Iterable
from statistics import median
from typing import Any


def clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def discovery_stage(touch_count: int, sync_score: float, confidence: float) -> str:
    """Separate early observation from formal confirmation.

    Two or three coherent strong stocks are allowed into the observation layer.
    Four or more touches remain the hard gate for AI admission.
    """
    if touch_count >= 4:
        return "formal_candidate"
    if touch_count >= 3 and sync_score >= 45:
        return "early_observation"
    if touch_count >= 2 and sync_score >= 60 and confidence >= 0.55:
        return "early_observation"
    return "noise"


def cluster_metrics(cluster: dict[str, Any]) -> dict[str, Any]:
    members = list(cluster.get("members") or [])
    pct_values = [
        _float(member.get("pct_change", member.get("rt_pct_chg")))
        for member in members
        if member.get("pct_change", member.get("rt_pct_chg")) is not None
    ]
    median_pct = median(pct_values) if pct_values else 0.0
    up_ratio = sum(value > 0 for value in pct_values) / len(pct_values) if pct_values else 0.0
    strong_ratio = sum(value >= 5 for value in pct_values) / len(pct_values) if pct_values else 0.0

    times = sorted(
        value
        for value in (_minutes(member.get("limit_up_time")) for member in members)
        if value is not None
    )
    if len(times) >= 2:
        spread = max(times) - min(times)
        time_sync = clamp(100 - min(100, spread / 45 * 100))
    elif len(times) == 1:
        time_sync = 35.0
    else:
        time_sync = 20.0

    touch_count = int(cluster.get("touch_count") or 0)
    sealed_count = int(cluster.get("sealed_count") or 0)
    failed_count = int(cluster.get("failed_count") or 0)
    growth_count = int(cluster.get("growth_count") or 0)
    denominator = max(1, touch_count)
    board_quality = clamp(
        sealed_count / denominator * 65
        + growth_count / denominator * 20
        - failed_count / denominator * 35
        + 15
    )
    breadth_score = clamp(
        45 * up_ratio
        + 35 * strong_ratio
        + 20 * clamp((median_pct + 2) * 8) / 100
    )
    sync_score = clamp(0.55 * time_sync + 0.45 * board_quality)
    return {
        "median_pct": round(median_pct, 3),
        "up_ratio": round(up_ratio, 3),
        "strong_ratio": round(strong_ratio, 3),
        "time_sync_score": time_sync,
        "board_quality_score": board_quality,
        "synchronization_score": sync_score,
        "breadth_score": breadth_score,
    }


def core_stock_structure(
    quotes: Iterable[dict[str, Any]],
    kpl_events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    quote_map = {str(item.get("code")): item for item in quotes}
    events = list(kpl_events)
    if not quote_map:
        return {}

    ranked = []
    for code, quote in quote_map.items():
        event = next((item for item in events if str(item.get("code")) == code), {})
        pct = _float(quote.get("pct_change"))
        height = _board_height(event.get("status"))
        limit_time = _minutes(event.get("limit_up_time"))
        amount = _float(event.get("amount"))
        turnover = _float(event.get("turnover_rate"))
        pioneer_score = clamp(
            max(0, 70 - ((limit_time or 13 * 60) - 9 * 60 - 30) * 0.8)
            + min(20, height * 5)
            + max(0, pct - 5)
        )
        space_score = clamp(height * 24 + max(0, pct) * 3)
        capacity_score = clamp(
            min(70, amount / 1e8 * 8) + max(0, pct) * 2 + min(15, turnover)
        )
        influence_score = clamp(
            0.45 * pioneer_score + 0.35 * space_score + 0.20 * capacity_score
        )
        ranked.append(
            {
                "code": code,
                "name": quote.get("name") or event.get("name"),
                "pct_change": round(pct, 3),
                "board_height": height,
                "pioneer_score": pioneer_score,
                "space_score": space_score,
                "capacity_score": capacity_score,
                "influence_score": influence_score,
            }
        )

    def pick(key: str, excluded: set[str] | None = None) -> dict[str, Any] | None:
        excluded = excluded or set()
        pool = [item for item in ranked if item["code"] not in excluded]
        return max(pool, key=lambda item: item[key], default=None)

    pioneer = pick("pioneer_score")
    space = pick("space_score")
    capacity = pick("capacity_score")
    influence = pick("influence_score")
    leaders = {item["code"] for item in (pioneer, space, capacity, influence) if item}
    followers = sorted(
        [item for item in ranked if item["code"] not in leaders],
        key=lambda item: item["pct_change"],
        reverse=True,
    )
    return {
        "pioneer": pioneer,
        "space_leader": space,
        "capacity_core": capacity,
        "influence_leader": influence,
        "elastic_followers": followers[:3],
    }


def catalyst_quality(catalysts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(catalysts)
    if not items:
        return {
            "score": 0.0,
            "truth": 0.0,
            "novelty": 0.0,
            "impact": 0.0,
            "duration": 0.0,
        }

    source_weight = {
        "central_policy": 100,
        "ministry_policy": 95,
        "exchange_announcement": 92,
        "company_announcement": 90,
        "official_media": 82,
        "industry_media": 68,
        "broker_research": 52,
        "social": 25,
        "rumor": 10,
    }
    truth_values, novelty_values, impact_values, duration_values = [], [], [], []
    for item in items:
        tier = str(item.get("source_tier") or item.get("source_type") or "industry_media")
        truth_values.append(float(item.get("truth_score") or source_weight.get(tier, 55)))
        novelty_values.append(float(item.get("novelty_score") or 55))
        impact_values.append(
            float(item.get("impact_score") or item.get("industry_impact") or 55)
        )
        duration_values.append(
            float(item.get("duration_score") or item.get("persistence_score") or 50)
        )
    truth = max(truth_values)
    novelty = max(novelty_values)
    impact = max(impact_values)
    duration = max(duration_values)
    score = clamp(0.34 * truth + 0.24 * novelty + 0.24 * impact + 0.18 * duration)
    return {
        "score": score,
        "truth": clamp(truth),
        "novelty": clamp(novelty),
        "impact": clamp(impact),
        "duration": clamp(duration),
    }


def counter_evidence(
    *,
    median_pct: float,
    negative_ratio: float,
    failed_count: int,
    concentration: float,
    divergence: bool,
    market_regime: str,
) -> dict[str, Any]:
    evidence: list[str] = []
    score = 0.0
    if median_pct < 1.0:
        evidence.append("板块中位数涨幅偏弱")
        score += 18
    if negative_ratio >= 0.25:
        evidence.append("后排负反馈比例较高")
        score += min(25, negative_ratio * 60)
    if failed_count >= 2:
        evidence.append("炸板数量偏多")
        score += min(25, failed_count * 7)
    if concentration >= 0.48:
        evidence.append("上涨过度集中于少数核心股")
        score += 18
    if divergence:
        evidence.append("核心股与板块后排发生背离")
        score += 22
    if market_regime in {"退潮", "冰点"}:
        evidence.append(f"市场环境处于{market_regime}")
        score += 18 if market_regime == "退潮" else 25
    return {"score": clamp(score), "items": evidence}


def market_regime(metrics: dict[str, Any]) -> dict[str, Any]:
    """Classify the whole market using breadth, carry and negative-feedback data."""
    limit_up = int(metrics.get("limit_up_count") or 0)
    limit_down = int(metrics.get("limit_down_count") or 0)
    seal_rate = _float(metrics.get("seal_rate"))
    promotion_rate = _float(metrics.get("promotion_rate"))
    yesterday_return = _float(metrics.get("yesterday_limit_return"))
    board_trade_return = _float(metrics.get("board_trade_return"))
    failed_rate = _float(metrics.get("failed_rate"))
    max_board_height = int(metrics.get("max_board_height") or 0)
    nuclear_button_ratio = _float(metrics.get("nuclear_button_ratio"))
    break_board_return = _float(metrics.get("break_board_return"))
    earth_sky_count = int(metrics.get("earth_sky_count") or 0)

    score = clamp(
        30
        + min(22, limit_up / 60 * 22)
        - min(22, limit_down / 20 * 22)
        + seal_rate * 0.15
        + promotion_rate * 0.14
        + max(-14, min(14, yesterday_return * 2.8))
        + max(-10, min(10, board_trade_return * 2.0))
        + min(8, max_board_height * 1.2)
        - failed_rate * 0.14
        - nuclear_button_ratio * 0.18
        + max(-8, min(4, break_board_return * 1.5))
        - min(8, earth_sky_count * 2)
    )
    if score >= 78:
        label = "主升"
    elif score >= 60:
        label = "修复"
    elif score >= 43:
        label = "震荡"
    elif score >= 28:
        label = "退潮"
    else:
        label = "冰点"
    return {"label": label, "score": score}


def lifecycle_stage(
    *,
    day_number: int,
    heat: float,
    entry_risk: float,
    divergence: bool,
    negative_ratio: float,
    synchronization_score: float = 50,
    median_pct: float = 0,
) -> str:
    if heat < 22 and day_number >= 3:
        return "衰退"
    if entry_risk >= 78 or negative_ratio >= 0.38:
        return "退潮"
    if divergence:
        return "分歧"
    if heat >= 84 and day_number >= 2:
        return "高潮"
    if day_number >= 2 and heat >= 48 and entry_risk < 62:
        return "修复" if synchronization_score < 45 else "加速"
    if heat >= 55 or median_pct >= 3:
        return "发酵"
    if day_number <= 1 and synchronization_score >= 55:
        return "启动"
    return "潜伏"


def _minutes(value: Any) -> int | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) < 4:
        return None
    digits = digits.zfill(6)
    try:
        return int(digits[-6:-4]) * 60 + int(digits[-4:-2])
    except ValueError:
        return None


def _board_height(status: Any) -> int:
    value = str(status or "")
    if "首板" in value:
        return 1
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
