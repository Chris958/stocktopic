"""Deterministic market observations, independent of new-event admission."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .board_status import parse_board_status
from .theme_graph import is_broad_parent_tag

STATE_KEY = "market_cycles_v1"


def cycle_family(tag: str) -> str:
    text = re.sub(r"(?:概念|板块)$", "", tag).strip()
    # Market context only. These aliases never merge investable event nodes or
    # authorize an AI member outside its original local node.
    groups = {
        "机器人": {"机器人", "人形机器人", "具身智能", "具身智能机器人"},
        "低空经济": {"低空经济", "飞行汽车", "飞行汽车(eVTOL)", "eVTOL"},
        "生猪养殖": {"猪肉", "生猪", "生猪养殖", "养猪", "猪周期", "猪价"},
    }
    return next((name for name, aliases in groups.items() if text in aliases), text)


class MarketCycleTracker:
    def __init__(self, database):
        self.db = database

    def observe(
        self,
        day: str,
        now: datetime,
        events: list[dict],
        clusters: list[dict],
        *,
        coverage_complete: bool | None = None,
    ) -> None:
        by_code = {str(e["code"]): e for e in events}
        groups: dict[str, dict[str, Any]] = {}
        # A market fact must not disappear behind the AI's top-30 node budget.
        extra: dict[str, set[str]] = {}
        for event in events:
            tags = [
                *list(event.get("themes") or []),
                *[
                    t.get("tag")
                    for key in ("concept_tags", "knowledge_graph_tags")
                    for t in event.get(key) or []
                    if isinstance(t, dict)
                ],
            ]
            for tag in tags:
                if isinstance(tag, str) and 2 <= len(tag.strip()) <= 50:
                    extra.setdefault(tag.strip(), set()).add(str(event["code"]))
        nodes = [
            *clusters,
            *[
                {"tag": tag, "member_codes": sorted(codes)}
                for tag, codes in extra.items()
                if len(codes) >= 2
            ],
        ]
        for node in nodes:
            if node.get("parent_only") or is_broad_parent_tag(node["tag"]):
                continue
            codes = set(node.get("member_codes") or []) & by_code.keys()
            if len(codes) < 2:
                continue
            family = cycle_family(node["tag"])
            group = groups.setdefault(family, {"codes": set(), "tags": set()})
            group["codes"].update(codes)
            group["tags"].add(node["tag"])
        state = json.loads(self.db.get_metadata(STATE_KEY) or "{}")
        streaks = self.db.previous_limit_streaks(day, list(by_code))
        for family in sorted(set(groups) | set(state)):
            group = groups.get(family, {"codes": set(), "tags": set()})
            codes = sorted(group["codes"])
            old = state.get(family)
            if not old and not codes:
                continue
            if old and day < old["last_trade_date"]:
                continue  # A later replay must not replace a live market snapshot.
            if (
                old
                and old.get("inactive_complete_days", 0) >= 3
                and not codes
                and coverage_complete is True
            ):
                continue
            snapshots = list(old.get("snapshots", [])) if old else []
            previous = next((s for s in reversed(snapshots) if s["trade_date"] < day), None)
            gap = (self.db.count_open_days(old["last_trade_date"], day) - 1) if old else 0
            if old and gap > 3 and coverage_complete is True and codes:
                old, snapshots, previous = None, [], None
            start = old["start_trade_date"] if old else day
            ladder, members = Counter(), []
            for code in codes:
                event = by_code[code]
                parsed = parse_board_status(event.get("status"))
                height = parsed["consecutive"] if event.get("board_tag") == "涨停" else None
                basis = "provider_reported" if height else "unknown"
                if (
                    height is None
                    and event.get("board_tag") == "涨停"
                    and streaks.get(code) is not None
                ):
                    height, basis = streaks[code] + 1, "local_price_streak_estimate"
                if height:
                    ladder[str(height)] += 1
                members.append(
                    {
                        "code": code,
                        "name": event.get("name"),
                        "board_height": height,
                        "board_basis": basis,
                        "board_status": parsed["label"],
                        "board_tag": event.get("board_tag"),
                    }
                )
            old_codes = set(previous["member_codes"]) if previous else set()
            retained = len(set(codes) & old_codes)
            new = len(set(codes) - old_codes)
            old_heights = {
                m["code"]: m.get("board_height") for m in (previous or {}).get("members", [])
            }
            promotions = sum(
                bool(
                    m["board_height"]
                    and old_heights.get(m["code"])
                    and m["board_height"] > old_heights[m["code"]]
                )
                for m in members
            )
            if coverage_complete is not True:
                stage = "数据待补"
            elif not previous:
                stage = "启动观察" if len(codes) >= 4 else "早期观察"
            elif previous.get("coverage_complete") is not True:
                stage = "跟踪恢复"
            elif not codes:
                stage = "降温观察"
            elif len(codes) >= 4 and previous["touch_count"] < 4:
                stage = "修复观察"
            elif len(codes) > previous["touch_count"] and new:
                stage = "扩散"
            elif len(codes) < previous["touch_count"]:
                stage = "分化"
            else:
                stage = "延续"
            snapshot = {
                "trade_date": day,
                "available_at": now.isoformat(timespec="seconds"),
                "historical_replay": day != now.strftime("%Y%m%d"),
                "coverage_complete": coverage_complete,
                "stage": stage,
                "touch_count": len(codes),
                "member_codes": codes,
                "members": members,
                "board_ladder": dict(ladder),
                "unknown_board_count": sum(
                    m["board_tag"] == "涨停" and m["board_height"] is None for m in members
                ),
                "non_limit_strong_count": sum(m["board_tag"] != "涨停" for m in members),
                "new_strong_count": new,
                "retained_strong_count": retained,
                "promotion_count": promotions,
                "tags": sorted(group["tags"]),
            }
            snapshot["comparison_trade_date"] = (previous or {}).get("trade_date")
            if (
                coverage_complete is not True
                or previous
                and previous.get("coverage_complete") is not True
            ):
                snapshot.update(
                    new_strong_count=None, retained_strong_count=None, promotion_count=None
                )
            snapshots = [s for s in snapshots if s["trade_date"] != day] + [snapshot]
            inactive = 0
            for item in reversed(snapshots):
                if item["touch_count"] or item["coverage_complete"] is not True:
                    break
                inactive += 1
            cycle_id = (old or {}).get("cycle_id") or hashlib.sha256(
                f"market-cycle-v1|{family}|{start}".encode()
            ).hexdigest()[:20]
            state[family] = {
                "cycle_id": cycle_id,
                "family": family,
                "start_trade_date": start,
                "last_trade_date": day,
                "inactive_complete_days": inactive,
                "snapshots": snapshots[-10:],
                "latest": snapshot,
            }
        # Bound metadata, while original event/admission traces remain in their own tables.
        state = dict(
            sorted(state.items(), key=lambda p: p[1]["last_trade_date"], reverse=True)[:120]
        )
        self.db.set_metadata(STATE_KEY, json.dumps(state, ensure_ascii=False))

    def dashboard(self, now: datetime) -> list[dict]:
        state = json.loads(self.db.get_metadata(STATE_KEY) or "{}")
        since = (now - timedelta(days=90)).date().isoformat()
        refs = self.db.market_cycle_theme_refs(since)
        result = []
        today = now.strftime("%Y%m%d")
        recent = set(self.db.open_trade_dates(today, 3)) | {today}
        for cycle in state.values():
            latest = cycle["latest"]
            if latest["trade_date"] > today:
                continue
            if latest["trade_date"] not in recent:
                continue
            related = [r for r in refs if cycle_family(r["shared_tag"]) == cycle["family"]]
            admitted = [r for r in related if r["qualified_event"]]
            if (
                latest["touch_count"] < 4
                and not admitted
                and not any(s["touch_count"] >= 4 for s in cycle["snapshots"])
            ):
                continue  # Existing 2-3-stock observation cards already cover this layer.
            # Archived records remain archived; relation is context, never new admission.
            current = [
                r
                for r in admitted
                if r["status"] in {"confirmed", "watching"}
                and r["day1_date"].replace("-", "") >= cycle["start_trade_date"]
            ]
            history = [
                {
                    key: s[key]
                    for key in (
                        "trade_date",
                        "available_at",
                        "historical_replay",
                        "coverage_complete",
                        "stage",
                        "touch_count",
                        "board_ladder",
                    )
                }
                for s in cycle["snapshots"]
            ]
            visible_latest = {
                **latest,
                "members": sorted(
                    latest["members"],
                    key=lambda m: (m["board_height"] or 0, m["code"]),
                    reverse=True,
                )[:12],
            }
            result.append(
                {
                    **{k: v for k, v in cycle.items() if k not in {"snapshots", "latest"}},
                    "latest": visible_latest,
                    "history": history,
                    "related_themes": related[-12:],
                    "event_admission": "linked_active_event"
                    if current
                    else "historical_event_only"
                    if admitted
                    else "not_verified",
                    "as_of_previous_trade_date": latest["trade_date"] != today,
                }
            )
        result.sort(
            key=lambda c: (
                c["last_trade_date"],
                c["latest"]["touch_count"] >= 4,
                c["event_admission"] == "linked_active_event",
                c["latest"]["touch_count"],
            ),
            reverse=True,
        )
        return result[:8]
