"""Stable investable directions, with catalysts and daily membership kept separately."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime

from .market_clock import MarketClock
from .theme_graph import is_broad_parent_tag

# These are investable directions, not generic industry parents. Component labels
# such as bearings, reducers or Tesla alone do NOT imply a robot relationship.
DIRECTION_ALIASES = {
    "机器人": {"机器人", "人形机器人", "工业机器人", "服务机器人", "具身智能"},
    "低空经济": {"低空经济", "低空", "飞行汽车", "飞行汽车(eVTOL)", "eVTOL"},
    "AI内容": {"AI内容", "AI影视", "AI短剧", "AI漫剧", "AI语料", "AI视频", "AIGC"},
    "文化内容": {
        "文化内容",
        "出版",
        "出版传媒",
        "出版改革",
        "影视院线",
        "影视",
        "短剧",
        "漫剧",
        "IP经济",
        "文化出海",
        "电影票房",
        "数字出版",
    },
    "算力": {"算力", "算力租赁", "数据中心", "液冷", "液冷超节点", "东数西算"},
    "光通信": {"光通信", "光模块", "CPO", "硅光", "光纤通信"},
    "半导体": {"半导体", "芯片", "存储芯片", "存储器", "先进封装", "光刻机"},
    "智能驾驶": {"智能驾驶", "自动驾驶", "无人驾驶", "车路云", "FSD"},
    "新能源汽车": {"新能源汽车", "新能源车", "汽车整车", "固态电池", "锂电池"},
    "商业航天": {"商业航天", "卫星互联网", "卫星导航", "可回收火箭"},
    "创新药": {"创新药", "创新药出海", "生物创新药"},
    "生猪养殖": {"生猪养殖", "生猪", "猪肉", "猪周期", "养猪", "猪价"},
    "稀土永磁": {"稀土永磁", "稀土", "永磁材料"},
    "PCB": {"PCB", "PCB材料", "PTFE正交背板", "英伟达PTFE正交背板"},
}


def _compact(value: str) -> str:
    return re.sub(r"\s|(?:概念|板块)$", "", str(value or "")).casefold()


def label_direction(value: str) -> str | None:
    text = _compact(value)
    return next(
        (
            name
            for name, aliases in DIRECTION_ALIASES.items()
            if text in {_compact(a) for a in aliases}
        ),
        None,
    )


def event_direction(
    tag: str,
    event_name: str = "",
    logic: str = "",
    proposed_direction: str = "",
) -> str | None:
    direct = label_direction(tag)
    if direct:
        return direct
    text = f"{tag} {event_name} {logic}"
    small_event = r"催化|公告|发布|订单|量产|首发|签约|合作|增持|新作|上星|改革|事件|新政策"
    proposed = str(proposed_direction or "").strip()
    if (
        2 <= len(proposed) <= 20
        and not is_broad_parent_tag(proposed)
        and not re.search(small_event, proposed)
        and proposed in text
    ):
        return label_direction(proposed) or proposed
    # Naming can use already-verified event logic. Daily automatic stock additions
    # use exact local graph labels instead, never these text matches.
    inferred = [
        name
        for name, aliases in DIRECTION_ALIASES.items()
        if any(len(a) >= 3 and a.casefold() in text.casefold() for a in aliases)
    ]
    if len(inferred) == 1:
        return inferred[0]
    if len(inferred) > 1:
        return None  # An ambiguous multi-direction catalyst must not select the first family.
    if tag and len(tag) <= 20 and not is_broad_parent_tag(tag) and not re.search(small_event, tag):
        return tag.removesuffix("概念")
    return None


def event_tags(event: dict) -> set[str]:
    return {
        str(tag)
        for tag in [
            *(event.get("themes") or []),
            *(
                item.get("tag")
                for key in ("concept_tags", "knowledge_graph_tags")
                for item in event.get(key) or []
                if isinstance(item, dict)
            ),
        ]
        if tag
    }


def member_relation(
    event: dict,
    direction: str,
    previous: set[str],
    validated_tags: set[str] | None = None,
) -> str | None:
    reason = str(event.get("limit_reason") or "")
    if re.search(r"非机器人|与机器人无关|不涉及机器人", reason) and direction == "机器人":
        return None
    reason_directions = {
        name
        for name, aliases in DIRECTION_ALIASES.items()
        if any(len(a) >= 3 and a in reason for a in aliases)
    }
    if reason_directions and direction not in reason_directions:
        return None  # A known holding can be strong today for a different reason.
    if re.search(r"控制权|实控人变更|要约收购|摘帽", reason) and not reason_directions:
        return None
    tags = event_tags(event)
    matched = sorted(
        tag
        for tag in tags
        if label_direction(tag) == direction or tag == direction or tag in (validated_tags or set())
    )
    if matched:
        return "local_graph:" + "/".join(matched)
    if event["code"] in previous:
        return "previously_admitted_member"
    return None


class DirectionDeferred(RuntimeError):
    """The archive changed, or the observation has no new post-archive resonance."""


class ThemeDirectionPool:
    def __init__(self, database):
        self.db = database
        self.lock = threading.RLock()

    def normalize_existing(self, now: datetime | None = None) -> None:
        for name, root in self._roots().items():
            self.db.name_theme_direction(root["id"], name)
            for source_id in root.get("duplicate_formal_ids", []):
                outcome = self.db.adopt_direction_event(
                    root["id"],
                    source_id,
                    name,
                    (now or datetime.now().astimezone()).isoformat(),
                    None,
                    root["catalyst_strength"],
                    root["catalyst_duration"] or "",
                )
                if outcome is not None:
                    self.db.set_metadata(
                        f"direction_consolidation:{source_id}",
                        json.dumps(
                            {
                                "direction": name,
                                "formal_theme_id": root["id"],
                                "reason": "same_previously_admitted_direction",
                            },
                            ensure_ascii=False,
                        ),
                    )

    def _roots(self) -> dict[str, dict]:
        groups = {}
        for row in self.db.direction_tracking_rows():
            state = row.get("direction_state") or {}
            name = state.get("name") or event_direction(
                row["shared_tag"],
                row.get("final_name") or row["provisional_name"],
            )
            if name:
                groups.setdefault(name, []).append(row)
        result = {}
        for name, rows in groups.items():
            root = min(
                rows,
                key=lambda r: (r["status"] != "confirmed", not r["qualified_formal"], int(r["id"])),
            )
            root["duplicate_formal_ids"] = [
                r["id"] for r in rows if r["status"] == "confirmed" and r != root
            ]
            result[name] = root
        return result

    def _fresh_after_archive(self, root: dict, day: str, now: datetime, events: list) -> bool:
        if root["status"] != "archived":
            return True
        current = MarketClock.normalize(now)
        if day != current.strftime("%Y%m%d") or len({e["code"] for e in events}) < 4:
            return False  # A historical replay is not renewed live resonance.
        archived = MarketClock.normalize(datetime.fromisoformat(root["archived_at"]))
        if day > archived.strftime("%Y%m%d"):
            return True
        if current <= archived:
            return False
        mark = json.loads(self.db.get_metadata(f"direction_archive:{root['id']}") or "{}")
        if not mark:
            return False  # Older archives without a watermark wait for the next trade date.
        return bool(
            {e["code"] for e in events} - set(mark.get("member_codes", []))
            or {e["code"] for e in events if e.get("board_tag") == "涨停"}
            - set(mark.get("sealed_codes", []))
        )

    @staticmethod
    def _members(events: list[dict], name: str, now: datetime, day: str) -> list[dict]:
        return [
            {
                "code": e["code"],
                "name": e.get("name") or e["code"],
                "membership_source": "direction_daily_strength",
                "evidence": {
                    "shared_tag": name,
                    "trade_date": day,
                    "board_tag": e.get("board_tag"),
                    "board_status": e.get("status"),
                    "first_limit_time": e.get("limit_up_time"),
                    "limit_reason": e.get("limit_reason"),
                    "signal_source": e.get("signal_source"),
                    "limit_time_basis": e.get("limit_time_basis"),
                    "source_themes": sorted(event_tags(e)),
                    "direction_relation": e.get("direction_relation"),
                    "evidence_available_at": now.isoformat(),
                },
            }
            for e in events
        ]

    def observe(self, day: str, now: datetime, events: list[dict]) -> list[dict]:
        """Update existing pools from local evidence; return fixed >=4-stock AI nodes."""
        with self.lock:
            known = []
            for name, root in self._roots().items():
                previous = {
                    m["code"] for m in root["members"] if m["active"] and root["qualified_formal"]
                }
                matched = [
                    {**e, "direction_relation": relation}
                    for e in events
                    if (
                        relation := member_relation(
                            e,
                            name,
                            previous,
                            set((root.get("direction_state") or {}).get("validated_tags", [])),
                        )
                    )
                ]
                matched = list({e["code"]: e for e in matched}.values())
                if not matched:
                    continue
                last_day = (root.get("direction_state") or {}).get("last_trade_date", "")
                if day >= last_day:
                    self.db.update_direction_members(
                        root["id"],
                        self._members(matched, name, now, day),
                        now.isoformat(),
                    )
                    state = {
                        **(root.get("direction_state") or {}),
                        "name": name,
                        "last_trade_date": day,
                        "last_observed_at": now.isoformat(),
                        "strong_codes": sorted(e["code"] for e in matched),
                        "tracking_status": root["status"],
                    }
                    self.db.set_metadata(
                        f"direction_state:{root['id']}", json.dumps(state, ensure_ascii=False)
                    )
                    self.db.set_metadata(
                        f"direction_tracking:{day}:{root['id']}",
                        json.dumps(
                            {
                                "direction": name,
                                "theme_id": root["id"],
                                "touch_count": len(matched),
                                "member_codes": state["strong_codes"],
                                "observed_at": now.isoformat(),
                                "added_codes": sorted({e["code"] for e in matched} - previous),
                                "archive_eligible": self._fresh_after_archive(
                                    root, day, now, matched
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    )
                    self.db.name_theme_direction(root["id"], name)
                if len(matched) < 4 or not self._fresh_after_archive(root, day, now, matched):
                    continue
                known.append(
                    {
                        "tag": name,
                        "direction_name": name,
                        "known_direction_id": root["id"],
                        "known_formal": bool(root["qualified_formal"]),
                        "known_status": root["status"],
                        "archive_epoch": root["archived_at"],
                        "member_codes": sorted(e["code"] for e in matched)[:15],
                        "member_reasons": {e["code"]: e["direction_relation"] for e in matched},
                        "cluster_confidence": root["cluster_confidence"],
                        "touch_count": min(15, len(matched)),
                        "cluster_method": "direction_continuation",
                    }
                )
            return known

    def prepare_clusters(self, day: str, now: datetime, clusters: list[dict]) -> list[dict]:
        with self.lock:
            roots, result = self._roots(), []
            for item in clusters:
                if len(set(item.get("member_codes") or [])) < 4:
                    early_name = event_direction(item["tag"])
                    early_name = next(
                        (
                            name
                            for name, root in roots.items()
                            if item["tag"]
                            in (root.get("direction_state") or {}).get("validated_tags", [])
                        ),
                        early_name,
                    )
                    if early_name in roots and roots[early_name]["status"] == "confirmed":
                        continue
                    result.append({**item, "direction_name": early_name or item["tag"]})
                    continue
                name = event_direction(
                    item["tag"],
                    item.get("canonical_name", ""),
                    item.get("common_logic", ""),
                    item.get("direction_name", ""),
                )
                if not name:
                    self.db.set_metadata(
                        f"direction_reject:{day}:{item['tag']}", "investable_direction_unresolved"
                    )
                    continue
                root = roots.get(name)
                context = {
                    "name": name,
                    "anchor_tag": item["tag"],
                    "event_name": item.get("canonical_name") or item["tag"],
                    "trade_date": day,
                    "known_formal": bool(root and root["qualified_formal"]),
                    "target_id": root["id"] if root else None,
                    "archive_epoch": root["archived_at"] if root else None,
                    "review_identity": (root["archived_at"] or f"pool:{root['id']}")
                    if root
                    else None,
                }
                context["evidence_revision"] = hashlib.sha256(
                    json.dumps(
                        {
                            "codes": sorted(item.get("member_codes") or []),
                            "event": context["event_name"],
                            "logic": item.get("common_logic"),
                            "urls": sorted(c.get("url") or "" for c in item.get("catalysts") or []),
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                if root:
                    events = item.get("members") or []
                    if not self._fresh_after_archive(root, day, now, events):
                        self.db.set_metadata(
                            f"direction_reject:{day}:{item['tag']}", "no_new_post_archive_resonance"
                        )
                        continue
                    if root["status"] == "confirmed":
                        last = (root.get("direction_state") or {}).get("last_trade_date", "")
                        if day >= last:
                            self.db.update_direction_members(
                                root["id"], self._members(events, name, now, day), now.isoformat()
                            )
                        self.db.save_theme_catalysts(root["id"], item.get("catalysts") or [])
                        self._record_event(root["id"], context, now)
                        # Already admitted directions need no duplicate daily admission.
                        continue
                result.append({**item, "direction_name": name, "direction_context": context})
            return result

    def admission_context(self, theme: dict) -> dict:
        saved = theme.get("direction_context") or {}
        evidence = [m.get("evidence") or {} for m in theme.get("members", [])]
        name = saved.get("name") or event_direction(
            theme["shared_tag"],
            next((e.get("canonical_name") for e in evidence if e.get("canonical_name")), ""),
            next((e.get("common_logic") for e in evidence if e.get("common_logic")), ""),
        )
        root = self._roots().get(name)
        if root and int(root["id"]) == int(theme["id"]):
            root = None
        return {
            **saved,
            "name": name,
            "known_formal": bool(root and root["qualified_formal"]),
            "target_id": root["id"] if root else None,
            "archive_epoch": root["archived_at"] if root else None,
            "event_name": saved.get("event_name") or theme["provisional_name"],
        }

    def _record_event(self, theme_id: int, context: dict, now: datetime) -> None:
        state = json.loads(self.db.get_metadata(f"direction_state:{theme_id}") or "{}")
        events = state.get("events", [])
        signature = (context.get("trade_date"), context.get("event_name"), context.get("source_id"))
        events = [
            e
            for e in events
            if (e.get("trade_date"), e.get("event_name"), e.get("source_id")) != signature
        ]
        events.append({**context, "available_at": now.isoformat()})
        state.update(name=context["name"], events=events[-30:])
        anchor = context.get("anchor_tag") or ""
        if (
            context["name"] not in DIRECTION_ALIASES
            and anchor
            and not is_broad_parent_tag(anchor)
            and anchor
            not in {
                "轴承",
                "汽车零部件",
                "零部件",
                "设备",
                "材料",
                "软件",
                "电子",
            }
            and event_direction(anchor) is not None
        ):
            state["validated_tags"] = sorted(set(state.get("validated_tags", [])) | {anchor})[:30]
        if context.get("trade_date", "") >= state.get("last_trade_date", ""):
            state["latest_event_name"] = context.get("event_name")
        self.db.set_metadata(f"direction_state:{theme_id}", json.dumps(state, ensure_ascii=False))

    def admit(self, theme: dict, context: dict, item: dict, now: datetime) -> dict:
        with self.lock:
            name = context["name"]
            if not name:
                raise DirectionDeferred("investable_direction_unresolved")
            roots = self._roots()
            root = roots.get(name)
            target = root["id"] if root else theme["id"]
            day = str(theme["day1_date"]).replace("-", "")
            if root and root["status"] == "archived":
                codes = {m["code"] for m in theme["members"] if m.get("active", 1)}
                events = [e for e in self.db.limit_touch_events(day) if e["code"] in codes]
                if not self._fresh_after_archive(root, day, now, events):
                    raise DirectionDeferred("no_new_post_archive_resonance")
            epoch = context.get("archive_epoch")
            outcome = self.db.adopt_direction_event(
                target,
                theme["id"],
                name,
                now.isoformat(),
                epoch,
                float(item.get("catalyst_confidence") or 0),
                f"预估{item['expected_duration_days']}个交易日",
            )
            if outcome is None:
                raise DirectionDeferred("archive_changed_during_verification")
            self._record_event(
                target,
                {
                    **context,
                    "trade_date": day,
                    "source_id": theme["id"],
                    "previous_day1_date": root["day1_date"] if root else None,
                },
                now,
            )
            return {
                "theme_id": target,
                **outcome,
                "reentry_key": hashlib.sha256(str(epoch).encode()).hexdigest()[:12]
                if outcome["reactivated"]
                else "",
            }
