"""Persistent stock watchlist, downstream of local market and direction evidence."""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, time, timedelta
from math import isfinite

from .board_status import consecutive_boards
from .leader_signals import HEIGHT_ONLY_FROM, MIN_WATCH_STRENGTH, advance_link, review_close
from .market_clock import MarketClock
from .market_cycles import cycle_family
from .market_feedback import (
    MAX_SAMPLE_GAP,
    board_tags,
    day_limits,
    day_prices,
    fresh_trade_sample,
    latest_boards,
    price_board,
)
from .market_scope import is_mainboard
from .theme_directions import event_direction, member_relation
from .theme_graph import is_broad_parent_tag
from .theme_intelligence import cluster_metrics, discovery_stage
from .theme_policy import policy_structured_event_clusters
from .tracking_policy import archive_tracking_active

STATE_KEY = "leader_pool:v1:state"
HISTORY_PREFIX = "leader_pool:v1:history:"
HISTORY_END = "leader_pool:v1:history;"
MAX_ACTIVE = 12
MAX_PER_DIRECTION = 2
MAX_PEERS = 12


class LeaderPool:
    def __init__(self, database):
        self.db = database
        self.lock = threading.Lock()

    def observe(self, now, quotes, limits, events, universe, *, coverage_complete):
        """Called once per fresh collection slot, never by discovery replay or a GET."""
        now = MarketClock.normalize(now)
        if MarketClock.session_name(now) not in {"morning", "afternoon"}:
            return
        at = now.isoformat(timespec="seconds")
        day = now.strftime("%Y%m%d")
        with self.lock:
            raw = self.db.get_metadata(STATE_KEY)
            state = json.loads(raw) if raw else {"active": {}}
            if (state.get("at", "") >= at or
                    state.get("slot") == f"{day}-{MarketClock.five_minute_slot(now)}"):
                return
            active = state["active"]
            prices = _live_prices(now, quotes, limits, universe)
            coverage = coverage_complete and bool(prices)
            themes = self.db.direction_tracking_rows()
            links_by_id = {t["id"]: t for t in themes}
            events_by_code = {e["code"]: e for e in events}
            groups = _groups(themes, events) if coverage else {}
            if coverage or active:
                dates = [d for d in self.db.open_trade_dates(day, 5) if d < day][:4]
                prior = self._prior_boards(dates)
            else:
                dates, prior = [], {}
            yesterday = prior[dates[0]][0] if dates else {}
            market_weak = bool(prices) and sum(q["pct"] <= -5 for q in prices.values()) >= (
                0.2 * len(prices)
            )
            measured = _Measures(day, dates, prior, prices, limits, events_by_code)
            history = []
            removed = set()
            for code, stock in list(active.items()):
                stock["quote"] = _display_quote(prices.get(code), at)
                lost = []
                keep = {}
                for name, link in stock["links"].items():
                    theme = links_by_id.get(link.get("theme_id"))
                    if not theme and link.get("theme_id") is not None:
                        lost.append((name, "原题材已移出跟踪范围。"))
                        continue
                    if theme and not archive_tracking_active(theme, now):
                        lost.append((name, "归档题材距首次创建超过14天，停止跟踪。"))
                        continue
                    if code not in universe:
                        lost.append((name, "股票已不在有效沪深主板监控范围。"))
                        continue
                    exit_reason = _review_recent_closes(link, code, dates, prior)
                    if exit_reason:
                        lost.append((name, exit_reason))
                        continue
                    group = groups.get(name, {})
                    if group.get("scope") == "formal" and code in group["codes"]:
                        link.update(theme_id=group["theme_id"], scope="formal")
                    elif theme:
                        link["scope"] = "archived" if theme["status"] == "archived" else "formal"
                    if not coverage:
                        link.update(action="wait", status="数据待补", points=[], window=None,
                                    reason="本次行情覆盖不足，保留跟踪，不判定退潮。")
                    else:
                        height, _ = measured.height(code)
                        if height is not None:
                            link["height"] = height
                        if height is not None and height >= HEIGHT_ONLY_FROM:
                            link["role"] = "height"
                            link["peak_height"] = max(height, link.get("peak_height") or 0)
                        peers = {
                            c: prices[c] for c in link["peers"] if c in prices and (
                                c not in events_by_code or link["scope"] != "formal"
                                or member_relation(events_by_code[c], name, {c})
                            )
                        }
                        event = events_by_code.get(code)
                        conflicting = (
                            event and link["scope"] == "formal"
                            and not member_relation(event, name, {code})
                        )
                        if conflicting:
                            link.update(action="wait", status="归因待确认", points=[], window=None,
                                        reason="本次异动与该题材的已知逻辑不符，等待共振确认。")
                        else:
                            advance_link(
                                link, now, prices.get(code), peers, prior=yesterday,
                                upper=limits.get(code, (None, None))[0], market_weak=market_weak,
                            )
                        if link.get("window"):
                            watch = link["window"]
                            followed = peers.get(watch["follower"])
                            if followed:
                                watch["follower_name"] = followed["name"]
                    keep[name] = link
                stock["links"] = keep
                if not keep:
                    removed.add(code)
                    del active[code]
                    history.append(_transition(stock, "removed", at, [n for n, _ in lost],
                                               "；".join(dict.fromkeys(r for _, r in lost))))
            if coverage and now.time() < time(15):
                self._select(active, groups, measured, now, history, removed, market_weak)
            snapshot = {"at": at, "trade_date": day, "coverage_complete": coverage,
                        "slot": f"{day}-{MarketClock.five_minute_slot(now)}", "active": active,
                        "history_head": history[-1]["id"] if history else state.get("history_head")}
            self._commit(raw, snapshot, history)

    def _prior_boards(self, dates):
        result = {}
        with self.db.connect() as con:
            for day in dates:
                events = latest_boards(con, day, completed=True)
                prices = day_prices(con, day, close_only=True)
                tags = board_tags(prices, day_limits(con, day), events)
                closed = {c: {"pct": q["pct_change"], "sealed": tags.get(c) == "涨停"}
                          for c, q in prices.items() if q["source"] == "official"}
                result[day] = tags, events, closed
        return result

    @staticmethod
    def _select(active, groups, measured, now, history, removed, market_weak):
        counts = Counter(name for item in active.values() for name in item["links"])
        fresh = {}
        for name, group in sorted(groups.items(), key=lambda pair: (
            pair[1]["scope"] != "formal", -len(pair[1]["codes"]), pair[0],
        )):
            if counts[name] >= MAX_PER_DIRECTION:
                continue
            members = list(group["codes"] & measured.prices.keys())
            strong = [
                c for c in members if measured.prices[c]["sealed"]
                or measured.prices[c]["pct"] >= MIN_WATCH_STRENGTH
            ]
            if len(strong) < 2:
                continue
            ranked = []
            for code in strong:
                if (code in removed or name in active.get(code, {}).get("links", {})
                        or (group["scope"] == "formal" and measured.events.get(code)
                            and not member_relation(measured.events[code], name, {code}))):
                    continue
                height, last_seal = measured.height(code)
                if last_seal:
                    ranked.append((code, height, last_seal))
            ranked.sort(key=lambda entry: (
                _leader_order(measured, entry[0], entry[1], strong), entry[0],
            ))
            # Prefer one height reference and one lower-position candidate when both exist.
            if counts[name] == 0:
                high = next((r for r in ranked if (r[1] or 0) >= HEIGHT_ONLY_FROM), None)
                low = next((r for r in ranked if (r[1] or 0) < HEIGHT_ONLY_FROM), None)
                if high and low:
                    ranked = [low, high, *(r for r in ranked if r not in {low, high})]
            for code, height, last_seal in ranked:
                if counts[name] >= MAX_PER_DIRECTION:
                    break
                if code not in active and len(active) >= MAX_ACTIVE:
                    continue
                peers = sorted(
                    (c for c in members if c != code),
                    key=lambda c: (
                        c not in strong, -measured.prices[c]["pct"], c,
                    ),
                )[:MAX_PEERS]
                q = measured.prices[code]
                role = "height" if (height or 0) >= HEIGHT_ONLY_FROM else "core"
                observed_first = _followed(measured, code, strong)
                date = f"{last_seal[:4]}-{last_seal[4:6]}-{last_seal[6:]}"
                basis = (f"近5个交易日内实际涨停（{date}），"
                         f"当前与{name}另{len(strong) - 1}只股票同步走强")
                if observed_first:
                    basis += f"，较早启动并观察到{observed_first}只同方向股票随后触板"
                if role == "height":
                    basis += f"，观察到至少{height}连板，列作情绪参考"
                else:
                    basis += "，列为承接观察候选"
                at = now.isoformat(timespec="seconds")
                if code not in active:
                    stock = {"code": code, "name": q["name"], "entered_at": at,
                             "entry_reason": basis + "。", "links": {},
                             "quote": _display_quote(q, at)}
                    active[code] = fresh[code] = stock
                link = {
                    "name": name, "theme_id": group["theme_id"], "scope": group["scope"],
                    "role": role, "height": height, "peers": peers, "entry_reason": basis + "。",
                    "peak_height": height, "entered_at": at,
                }
                advance_link(
                    link, now, q, {c: measured.prices[c] for c in peers},
                    prior=measured.yesterday, upper=measured.limits.get(code, (None, None))[0],
                    market_weak=market_weak,
                )
                active[code]["links"][name] = link
                counts[name] += 1
        for stock in fresh.values():
            history.append(_transition(stock, "entered", stock["entered_at"],
                                       list(stock["links"]), stock["entry_reason"]))

    def _commit(self, prior, snapshot, history):
        # Compute off-transaction; publish state and audit transitions atomically.
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT value FROM metadata WHERE key=?", (STATE_KEY,)).fetchone()
            if (row[0] if row else None) != prior:
                return
            con.execute(
                "INSERT INTO metadata(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value,updated_at=excluded.updated_at",
                (STATE_KEY, _dump(snapshot), snapshot["at"]),
            )
            con.executemany(
                "INSERT INTO metadata(key,value,updated_at) VALUES (?,?,?)",
                ((HISTORY_PREFIX + event["id"], _dump(event), event["at"]) for event in history),
            )

    def read(self, now):
        """Only read the persisted candidates and authoritative group status."""
        now = MarketClock.normalize(now)
        with self.db.connect() as con:
            con.execute("BEGIN")
            row = con.execute("SELECT value FROM metadata WHERE key=?", (STATE_KEY,)).fetchone()
            if not row:
                return {"state": "awaiting", "observed_at": None, "items": []}
            state = json.loads(row[0])
            ids = {link["theme_id"] for s in state["active"].values()
                   for link in s["links"].values() if link["theme_id"] is not None}
            current = {}
            if ids:
                marks = ",".join("?" for _ in ids)
                current = {r["id"]: dict(r) for r in con.execute(
                    "SELECT id,status,discovered_at FROM candidate_themes "
                    f"WHERE id IN ({marks})", sorted(ids),
                )}
            market_open = con.execute(
                "SELECT is_open FROM trading_calendar WHERE cal_date=?", (now.strftime("%Y%m%d"),),
            ).fetchone()
        as_of = datetime.fromisoformat(state["at"])
        trading = (
            market_open is not None and market_open[0]
            and MarketClock.session_name(now) in {"morning", "afternoon"}
            and now.time() < time(15)
        )
        fresh = (
            as_of.date() == now.date() and timedelta(0) <= now - as_of <= MAX_SAMPLE_GAP
            and MarketClock.session_name(now) == MarketClock.session_name(as_of)
        )
        ready = trading and fresh and state["coverage_complete"]
        items = []
        for stock in state["active"].values():
            links = []
            for link in stock["links"].values():
                t = current.get(link["theme_id"])
                if link["theme_id"] is not None and (
                    not t or t["status"] not in {"confirmed", "archived"}
                    or not archive_tracking_active(t, now)
                ):
                    continue
                view = {k: link.get(k) for k in (
                    "name", "theme_id", "scope", "role", "height", "status", "reason", "action",
                    "entry_reason", "feedback", "peer_pct", "window", "peak_height",
                )}
                if t and t["status"] == "archived" and link["scope"] == "formal":
                    view.update(scope="archived", action="wait", window=None, status="归档跟踪",
                                reason="原题材已归档，等待正式回流。")
                if not ready and view["action"] == "opportunity":
                    view.update(action="wait", window=None, status="待重新确认",
                                reason="原机会窗口需在下一批有效盘中行情重新确认。")
                if view["window"] and now >= datetime.fromisoformat(view["window"]["valid_until"]):
                    view.update(action="wait", window=None, status="窗口已结束",
                                reason="观察窗口已到期，等待新的回踩结构。")
                links.append(view)
            if not links:
                continue
            links.sort(key=lambda v: (
                v["action"] != "opportunity", v["scope"] != "formal",
                v["role"] == "height", v["name"],
            ))
            primary = links[0]
            items.append({
                "code": stock["code"], "name": stock["name"], "entered_at": stock["entered_at"],
                "entry_reason": stock["entry_reason"], "directions": links, "quote": stock["quote"],
                **{key: primary[key] for key in ("action", "status", "reason", "window")},
            })
        items.sort(key=lambda s: (
            s["action"] != "opportunity", s["directions"][0]["role"] == "height",
            s["directions"][0]["scope"] != "formal", s["entered_at"], s["code"],
        ))
        return {
            "observed_at": state["at"], "trade_date": state["trade_date"], "items": items,
            "history_head": state.get("history_head"),
            "state": "ready" if ready else "waiting_for_data" if trading else "closed",
        }

    def history(self, before: str | None = None, limit: int = 20):
        upper = HISTORY_PREFIX + before if before else HISTORY_END
        with self.db.connect() as con:
            rows = con.execute(
                "SELECT value FROM metadata WHERE key>=? AND key<? ORDER BY key DESC LIMIT ?",
                (HISTORY_PREFIX, upper, limit + 1),
            ).fetchall()
        events = [json.loads(row[0]) for row in rows[:limit]]
        return {"items": events, "next_cursor": events[-1]["id"] if len(rows) > limit else None}


class _Measures:
    def __init__(self, day, dates, prior, prices, limits, events):
        self.day, self.dates, self.prior = day, dates, prior
        self.prices, self.limits, self.events = prices, limits, events
        self.yesterday = prior[dates[0]][0] if dates else {}
        self.known = {}

    def height(self, code):
        """Use reported consecutive height or verified daily seals; gaps stay unknown."""
        if code in self.known:
            return self.known[code]
        days = [(d, *self.prior[d][:2]) for d in self.dates]
        if self.prices.get(code, {}).get("sealed"):
            days.insert(0, (self.day, {code: "涨停"}, self.events))
        last_seal = None
        height = None
        count = 0
        for day, tags, events in days:
            tag = tags.get(code)
            if not count and tag != "涨停":
                continue
            if tag != "涨停":
                if tag is not None or count >= HEIGHT_ONLY_FROM:
                    height = count
                break
            last_seal = last_seal or day
            actual = consecutive_boards(events.get(code, {}).get("status"))
            if actual:
                height = count + actual
                break
            count += 1
        else:
            if count >= HEIGHT_ONLY_FROM:
                height = count
        self.known[code] = (height, last_seal)
        return height, last_seal


def _groups(themes, events):
    groups = {}
    archived = set()
    by_code = {e["code"]: e for e in events}
    for theme in themes:
        state = theme.get("direction_state") or {}
        name = state.get("name") or event_direction(
            theme["shared_tag"], theme.get("final_name") or theme["provisional_name"],
        )
        if not name or is_broad_parent_tag(name):
            continue
        name = cycle_family(name)
        if theme["status"] == "archived":
            archived.add(name)
        if theme["status"] != "confirmed":
            continue
        codes = {m["code"] for m in theme["members"] if m["active"] and (
            m["code"] not in by_code or member_relation(by_code[m["code"]], name, {m["code"]},
                                                       set(state.get("validated_tags") or []))
        )}
        old = groups.get(name)
        if not old or len(codes) > len(old["codes"]):
            groups[name] = {"scope": "formal", "theme_id": theme["id"], "codes": codes}
    for node in policy_structured_event_clusters(events):
        tag = node["tag"]
        name = cycle_family(tag)
        if (node.get("parent_only") or is_broad_parent_tag(tag) or name in archived
                or name in groups):
            continue
        metrics = cluster_metrics(node)
        stage = discovery_stage(
            node["touch_count"], metrics["synchronization_score"], node["cluster_confidence"],
        )
        if stage != "noise":
            groups[name] = {"scope": "local", "theme_id": None,
                            "codes": set(node["member_codes"])}
    return groups


def _review_recent_closes(link, code, dates, prior):
    for i in range(min(2, len(dates)) - 1, -1, -1):
        day = dates[i]
        previous_day = dates[i + 1] if i + 1 < len(dates) else None
        if reason := review_close(link, code, prior[day][2], day, previous_day):
            return reason
    return None


def _live_prices(now, quotes, limits, universe):
    result = {}
    for q in quotes:
        when = MarketClock.normalize(q.captured_at)
        if (q.code not in universe or not is_mainboard(q.code)
                or q.pre_close <= 0 or q.close <= 0 or q.amount <= 0 or q.volume <= 0
                or not all(isfinite(value) for value in (q.close, q.pct_change, q.amount))
                or not fresh_trade_sample(now, when, q.trade_time)):
            continue
        tag = price_board({"close": q.close, "high": q.high}, limits.get(q.code, (None, None)))
        result[q.code] = {"code": q.code, "name": q.name, "pct": round(q.pct_change, 4),
                          "price": q.close,
                          "sealed": tag == "涨停", "amount": q.amount}
    return result


def _limit_time(event):
    value = str(event.get("limit_up_time") or "").replace(":", "")
    if len(value) == 6 and value.isdecimal():
        return int(value[:2]) * 60 + int(value[2:4])
    return None


def _followed(measured, code, peers):
    before = _limit_time(measured.events.get(code, {}))
    if before is None:
        return 0
    return sum(
        0 < value - before <= 30 for c in peers if c != code
        and (value := _limit_time(measured.events.get(c, {}))) is not None
    )


def _leader_order(measured, code, height, peers):
    quote = measured.prices[code]
    return (-(height or 0) if (height or 0) >= HEIGHT_ONLY_FROM else 0,
            -_followed(measured, code, peers), not quote["sealed"],
            _limit_time(measured.events.get(code, {})) or 24 * 60, -quote["pct"], -quote["amount"])


def _display_quote(quote, when):
    return {"price": quote["price"], "pct": round(quote["pct"], 2), "at": when} if quote else None


def _dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _transition(stock, event, at, themes, reason):
    return {"id": f"{at}:{stock['code']}:{event}", "at": at, "code": stock["code"],
            "name": stock["name"], "event": event, "directions": themes, "reason": reason,
            "entered_at": stock["entered_at"]}
