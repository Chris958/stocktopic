from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

BUY_FLAGS = {"b", "buy", "1", "+1", "买", "买入", "主动买", "外盘"}
SELL_FLAGS = {"s", "sell", "2", "-1", "卖", "卖出", "主动卖", "内盘"}
THRESHOLDS = ((500_000.0, "50W+"), (1_000_000.0, "100W+"))


def analyze_level2_orders(
    trades: list[dict[str, Any]],
    orders: list[dict[str, Any]] | None = None,
    *,
    code: str,
    name: str,
    trade_date: str,
    upper_limit: float | None = None,
    generated_at: str | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """Aggregate executions by the aggressor's order ID, never by isolated trade size."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    bs_profile: Counter[str] = Counter()
    trade_code_profile: Counter[str] = Counter()
    valid_amount = directional_amount = grouped_amount = 0.0
    valid_trade_count = directional_trade_count = grouped_trade_count = 0

    for row in trades:
        amount = _amount(row)
        if amount <= 0:
            continue
        valid_trade_count += 1
        valid_amount += amount
        raw_flag = _profile_value(row.get("bs_flag"))
        raw_code = _profile_value(row.get("trade_code"))
        bs_profile[raw_flag] += 1
        trade_code_profile[raw_code] += 1
        direction = _direction(row.get("bs_flag"))
        if not direction:
            continue
        directional_trade_count += 1
        directional_amount += amount
        order_id = _identifier(
            row.get("buy_order_id") if direction == "buy" else row.get("sell_order_id")
        )
        if not order_id:
            continue
        grouped_trade_count += 1
        grouped_amount += amount
        key = (direction, order_id)
        group = groups.setdefault(
            key,
            {
                "direction": direction,
                "order_id": order_id,
                "amount": 0.0,
                "volume": 0.0,
                "fill_count": 0,
                "prices": [],
                "times": [],
            },
        )
        group["amount"] += amount
        group["volume"] += max(0.0, _number(row.get("volume")))
        group["fill_count"] += 1
        price = _number(row.get("price"))
        if price > 0:
            group["prices"].append(price)
        timestamp = _time_milliseconds(row.get("time"))
        if timestamp is not None:
            group["times"].append(timestamp)

    finalized = [_finalize_group(group, upper_limit) for group in groups.values()]
    finalized.sort(key=lambda item: (-item["amount"], item["direction"], item["order_id"]))
    thresholds = [_threshold_summary(finalized, value, label) for value, label in THRESHOLDS]
    order_rows = orders or []
    order_profile = _order_profile(order_rows)
    events = [item for item in finalized if item["amount"] >= THRESHOLDS[0][0]][:30]
    return {
        "code": code,
        "name": name,
        "trade_date": trade_date,
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "partial": partial,
        "method": "aggressor_order_id_aggregation",
        "thresholds": thresholds,
        "coverage": {
            "valid_trade_count": valid_trade_count,
            "directional_trade_count": directional_trade_count,
            "grouped_trade_count": grouped_trade_count,
            "valid_amount": round(valid_amount, 2),
            "directional_amount_coverage_pct": _percentage(directional_amount, valid_amount),
            "order_id_amount_coverage_pct": _percentage(grouped_amount, valid_amount),
            "active_order_count": len(finalized),
        },
        "events": events,
        "raw_profile": {
            "bs_flag": dict(bs_profile.most_common()),
            "trade_code": dict(trade_code_profile.most_common()),
            "order_side": order_profile["side"],
            "order_type": order_profile["order_type"],
            "order_row_count": len(order_rows),
        },
        "limitations": [
            "金额按主动方向对应的委托号聚合，代表该委托已成交部分，不代表原始委托总额",
            "无法识别方向或缺少主动方委托号的成交不进入50W+/100W+统计",
            "大单撤单、封板资金和炸板卖单需用真实order_type及盘口样本校准后启用",
        ],
    }


def format_level2_report(report: dict[str, Any]) -> str:
    rows = [f"{report['name']}({report['code']}) · {report['trade_date']}"]
    for item in report.get("thresholds", []):
        ratio = item.get("buy_ratio_pct")
        label = f"{ratio:.0f}%" if ratio is not None else "无有效大单"
        rows.append(f"{item['label']:<6} 买入 {label:<8} {_bar(ratio)}")
    values = {item["label"]: item for item in report.get("thresholds", [])}
    rows.append(f"大单净主动流入：{_money(values.get('50W+', {}).get('net_inflow', 0))}")
    rows.append(f"超大单净主动流入：{_money(values.get('100W+', {}).get('net_inflow', 0))}")
    coverage = report.get("coverage", {})
    rows.append(
        "主动方向覆盖："
        f"{coverage.get('directional_amount_coverage_pct', 0):.1f}% · "
        f"委托号覆盖：{coverage.get('order_id_amount_coverage_pct', 0):.1f}%"
    )
    return "\n".join(rows)


def serialize_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"))


def _threshold_summary(
    groups: list[dict[str, Any]], threshold: float, label: str
) -> dict[str, Any]:
    selected = [item for item in groups if item["amount"] >= threshold]
    buys = [item for item in selected if item["direction"] == "buy"]
    sells = [item for item in selected if item["direction"] == "sell"]
    buy_amount = sum(item["amount"] for item in buys)
    sell_amount = sum(item["amount"] for item in sells)
    total = buy_amount + sell_amount
    return {
        "label": label,
        "threshold": threshold,
        "buy_amount": round(buy_amount, 2),
        "sell_amount": round(sell_amount, 2),
        "net_inflow": round(buy_amount - sell_amount, 2),
        "buy_ratio_pct": round(buy_amount / total * 100, 2) if total else None,
        "buy_order_count": len(buys),
        "sell_order_count": len(sells),
    }


def _finalize_group(group: dict[str, Any], upper_limit: float | None) -> dict[str, Any]:
    prices = group.pop("prices")
    times = group.pop("times")
    first = min(times) if times else None
    last = max(times) if times else None
    duration = last - first if first is not None and last is not None else None
    distinct_prices = len({round(value, 4) for value in prices})
    direction = group["direction"]
    at_limit = bool(
        direction == "buy"
        and upper_limit
        and prices
        and max(abs(value - upper_limit) for value in prices) <= 0.001
    )
    if at_limit:
        event_type, event_label = "limit_up_sweep", "涨停板扫单"
    elif group["fill_count"] >= 2 and duration is not None and duration <= 2000:
        event_type, event_label = (
            ("continuous_sweep", "连续扫单")
            if direction == "buy"
            else ("continuous_smash", "连续砸盘")
        )
    else:
        event_type, event_label = (
            ("large_active_buy", "主动买单")
            if direction == "buy"
            else ("large_active_sell", "主动卖单")
        )
    average_price = group["amount"] / group["volume"] if group["volume"] else None
    return {
        **group,
        "amount": round(group["amount"], 2),
        "volume": round(group["volume"], 2),
        "average_price": round(average_price, 4) if average_price else None,
        "first_time": _format_milliseconds(first),
        "last_time": _format_milliseconds(last),
        "duration_ms": duration,
        "distinct_price_count": distinct_prices,
        "event_type": event_type,
        "event_label": event_label,
    }


def _order_profile(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    sides: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for row in rows:
        sides[_profile_value(row.get("side"))] += 1
        types[_profile_value(row.get("order_type"))] += 1
    return {"side": dict(sides.most_common()), "order_type": dict(types.most_common())}


def _direction(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in BUY_FLAGS:
        return "buy"
    if normalized in SELL_FLAGS:
        return "sell"
    return None


def _amount(row: dict[str, Any]) -> float:
    amount = _number(row.get("amount"))
    if amount > 0:
        return amount
    return max(0.0, _number(row.get("price")) * _number(row.get("volume")))


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _identifier(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    result = str(value).strip()
    return "" if result.lower() in {"none", "null", "nan", "0"} else result


def _profile_value(value: Any) -> str:
    result = str(value).strip() if value is not None else ""
    return result or "<空>"


def _time_milliseconds(value: Any) -> int | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) < 6:
        return None
    digits = digits[:9].ljust(9, "0")
    hour, minute, second, millisecond = (
        int(digits[:2]),
        int(digits[2:4]),
        int(digits[4:6]),
        int(digits[6:9]),
    )
    if hour > 23 or minute > 59 or second > 59:
        return None
    return ((hour * 60 + minute) * 60 + second) * 1000 + millisecond


def _format_milliseconds(value: int | None) -> str | None:
    if value is None:
        return None
    milliseconds = value % 1000
    seconds = value // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}.{milliseconds:03d}"


def _percentage(numerator: float, denominator: float) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def _bar(value: float | None, width: int = 20) -> str:
    filled = round(max(0.0, min(100.0, value or 0.0)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _money(value: Any) -> str:
    amount = _number(value)
    sign = "+" if amount > 0 else "" if amount == 0 else "-"
    absolute = abs(amount)
    if absolute >= 100_000_000:
        return f"{sign}{absolute / 100_000_000:.2f}亿"
    if absolute >= 10_000:
        return f"{sign}{absolute / 10_000:,.0f}万"
    return f"{sign}{absolute:,.0f}元"
