"""Deterministic, sampled evidence. Only the web watchlist receives these signals."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from .market_clock import MarketClock
from .market_feedback import MAX_SAMPLE_GAP, MIN_FEEDBACK_COVERAGE, PRICE_TOLERANCE

HEIGHT_ONLY_FROM = 3
MIN_WATCH_STRENGTH = 3.0
SEVERE_LOSS = -5.0
WINDOW_DURATION = timedelta(minutes=20)
SETUP_DURATION = timedelta(minutes=30)


def advance_link(link, now, quote, peer_quotes, *, prior, upper, market_weak):
    """Advance a preselected leader against the peers fixed at entry."""
    link.update(action="wait", status="数据待补", reason="当前股票或固定对照股行情不全，保留跟踪。")
    codes = {*link["peers"], *([quote["code"]] if quote else [])}
    expected = {code for code in codes if prior.get(code) == "涨停"}
    observed = {q["code"]: q["pct"] for q in peer_quotes.values()}
    if quote:
        observed[quote["code"]] = quote["pct"]
    known = expected & observed.keys()
    severe = sum(observed[c] <= SEVERE_LOSS for c in known)
    link["feedback"] = {"observed": len(known), "expected": len(expected), "severe": severe}
    if (not quote or not link["peers"] or
            len(peer_quotes) / len(link["peers"]) < MIN_FEEDBACK_COVERAGE):
        link.update(points=[], window=None)
        return
    peer_pct = median(q["pct"] for q in peer_quotes.values())
    sample = {
        "at": now.isoformat(timespec="seconds"), "price": quote["price"], "pct": quote["pct"],
        "peer_pct": peer_pct, "peers": {code: q["pct"] for code, q in peer_quotes.items()},
    }
    points = link.get("points", [])
    last = datetime.fromisoformat(points[-1]["at"]) if points else None
    if (not last or now.date() != last.date() or now - last > MAX_SAMPLE_GAP
            or MarketClock.session_name(last) != MarketClock.session_name(now)):
        points = []
        link["window"] = None
    points = [p for p in points if now - datetime.fromisoformat(p["at"]) <= SETUP_DURATION]
    points.append(sample)
    link["points"] = points[-8:]
    link["peer_pct"] = round(peer_pct, 2)

    retreat, weak_reason = _weakness(quote, peer_quotes)

    previous_evidence = sum(c in prior for c in [quote["code"], *link["peers"]])
    feedback_ready = (
        previous_evidence / (1 + len(link["peers"])) >= MIN_FEEDBACK_COVERAGE
        and (not expected or len(known) / len(expected) >= MIN_FEEDBACK_COVERAGE)
    )
    heavy_loss = bool(known) and severe / len(known) >= 0.34
    if quote["pct"] <= SEVERE_LOSS or heavy_loss or market_weak:
        _wait(link, "负反馈较重", "候选或昨日强股出现明显负反馈，暂停入场观察。")
    elif weak_reason:
        _wait(link, "退潮观察" if retreat else "龙头地位观察", f"{weak_reason}，继续观察能否修复。")
    elif link["scope"] != "formal":
        if link["scope"] == "archived":
            _wait(link, "归档跟踪", "归档期限内继续观察，本方向尚未回到正式池。")
        else:
            _wait(link, "本地观察", "方向仍待正式确认，继续看其他成员能否跟上。")
    elif link["role"] == "height":
        _wait(link, "高标参考", "用于观察题材情绪；优先等低位候选出现修复。")
    elif not feedback_ready or not upper or link["height"] is None:
        _wait(link, "待验证", "连板或隔日反馈样本不足，暂不提示入场。")
    elif quote["sealed"] or quote["price"] >= upper - PRICE_TOLERANCE:
        _wait(link, "等分歧", "当前处在涨停价，等待重新打开后的承接证据。")
    elif now.strftime("%H:%M") == "15:00":
        _wait(link, "收盘观察", "当日监控已结束，下一交易日重新确认。")
    else:
        _setup_or_wait(link, now, points)


def review_close(link, code, prices, day, previous_day):
    """Only confirmed closing data advances the two-trading-day exit clock."""
    old = link.get("weak_close", {})
    if old.get("day", "") >= day or link["entered_at"][:10].replace("-", "") > day:
        return None
    stock = prices.get(code)
    peers = {c: prices[c] for c in link["peers"] if c in prices}
    if not stock or not link["peers"] or len(peers) / len(link["peers"]) < MIN_FEEDBACK_COVERAGE:
        return None
    _, reason = _weakness(stock, peers)
    consecutive = old.get("count", 0) + 1 if previous_day and old.get("day") == previous_day else 1
    link["weak_close"] = {"day": day, "count": consecutive if reason else 0}
    if reason and consecutive >= 2:
        return f"截至 {day} 连续两个交易日正式收盘：{reason}，停止跟踪该方向。"
    return None


def _weakness(quote, peer_quotes):
    peer_pct = median(q["pct"] for q in peer_quotes.values())
    other_strength = any(q["sealed"] or q["pct"] >= MIN_WATCH_STRENGTH
                         for q in peer_quotes.values())
    retreat = quote["pct"] <= -2 and peer_pct <= -1 and not other_strength
    replaced = (quote["pct"] <= -1 and peer_pct >= 0 and
                any(q["pct"] >= max(MIN_WATCH_STRENGTH, quote["pct"] + 4)
                    for q in peer_quotes.values()))
    return retreat, ("题材与个股同时走弱" if retreat else
                     "题材仍有强股，原候选持续落后" if replaced else "")


def _wait(link, status, reason):
    link.update(status=status, reason=reason, window=None)


def _setup_or_wait(link, now, points):
    current = points[-1]
    window = link.get("window")
    if window:
        expired = now >= datetime.fromisoformat(window["valid_until"])
        valid = (
            current["price"] > window["support_price"]
            and current["peers"].get(window["follower"], float("-inf")) >= window["peer_floor"]
            and current["pct"] >= current["peer_pct"]
            and current["pct"] >= 1.0
        )
        if not expired and valid:
            _publish_window(link, window)
            return
        _wait(link, "窗口已结束", "原观察窗口到期或修复未能维持，等待新的回踩结构。")
        return
    # Need strength before the pullback, then a rebound and a later follow-through sample.
    if len(points) >= 4:
        before, low, rebound, current = points[-4:]
        drop = before["pct"] - low["pct"]
        peer_drop = before["peer_pct"] - low["peer_pct"]
        relative_support = (peer_drop >= 1 and drop <= peer_drop + 0.5) or (
            peer_drop < 1 and low["peer_pct"] >= 0
        )
        continuation = (
            before["pct"] >= 2 and drop >= 1 and relative_support
            and low["at"] > link.get("last_setup", "")
            and rebound["pct"] - low["pct"] >= 0.7
            and current["pct"] >= max(1.5, rebound["pct"] - 0.2, current["peer_pct"] + 0.5)
        )
        followers = [
            (code, base + 0.6) for code, base in low["peers"].items()
            if rebound["peers"].get(code, float("inf")) < base + 0.5
            and current["peers"].get(code, float("-inf")) >= max(base + 0.8, 0.5)
        ]
        if continuation and followers:
            code, floor = sorted(followers)[0]
            link["last_setup"] = low["at"]
            _publish_window(link, {
                "kind": "分歧修复" if peer_drop >= 1 else "发酵延续",
                "triggered_at": now.isoformat(timespec="seconds"),
                "valid_until": (now + WINDOW_DURATION).isoformat(timespec="seconds"),
                "support_price": low["price"],
                "follower": code,
                "peer_floor": max(0.5, floor),
            })
            return
    _wait(link, "持续跟踪", "等待回踩后的相对抗跌与同题材股票跟随，单股反弹不触发窗口。")


def _publish_window(link, window):
    link.update(
        action="opportunity", window=window, status=window["kind"],
        reason="候选先稳住并率先回升，随后有同题材股票跟随，修复已连续两次采样维持。",
    )
