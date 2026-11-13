"""Persisted display projections, built by one read-only process, never a GET handler."""
from __future__ import annotations

import copy
import json
import logging
import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime
from pathlib import Path

from .db import Database
from .display_invalidation import PREFIX, SECTIONS, enqueue, get_value, put_value

logger = logging.getLogger(__name__)


def epoch(con, section, theme_id):
    return [get_value(con, f"epoch:{section}:*", 0),
            get_value(con, f"epoch:{section}:{theme_id}", 0)]


def headers(con):
    return [dict(r) for r in con.execute(
        "SELECT id,final_name,suggested_name,provisional_name,shared_tag,status,"
        "admission_status,theme_level,pinned,day1_date,updated_at,"
        "substr(admission_reason,1,120) reason_preview "
        "FROM candidate_themes WHERE status IN "
        "('pending','watching','confirmed','archived','rejected') "
        "ORDER BY pinned DESC,updated_at DESC,id DESC"
    )]


def compact_flow(value):
    if not isinstance(value, dict):
        return value
    return {key: compact_flow(v) if isinstance(v, dict) else v
            for key, v in value.items() if key not in {"members", "raw", "raw_response"}}



def usable_funds(data):
    reports = [data.get("fund_flow", {})]
    reports.extend(m.get("fund_flow", {}) for m in data.get("members", []))
    return any(
        report and (report.get("main_net") is not None or any(
            isinstance(source, dict) and source.get("main_net") is not None
            for source in (report.get("sources") or {}).values()
        ))
        for group in reports for report in (group.get("daily"), group.get("intraday"))
    )


def build_projection(db_path, archive_dir, day, revision, plan):
    """Spawn-safe: no service, scheduler, migrations, network or AI."""
    db = Database(Path(db_path), Path(archive_dir), read_only=True)
    built_at = datetime.now(UTC).isoformat()
    result = {}
    errors = {}
    for raw_id, sections in plan.items():
        identifier = int(raw_id)
        for section in sections:
            key = f"theme:{identifier}:{section}"
            try:
                with db.connect() as con:
                    version = epoch(con, section, identifier)
                theme = build_section(db, identifier, section, day)
                if theme is not None:
                    result[key] = {
                        "data": theme, "snapshot": {"state": "ready", "epoch": version,
                        "revision": revision, "built_at": built_at, "trade_date": day},
                    }
            except Exception as error:
                errors[key] = type(error).__name__
    index = db.theme_index()
    rankings = db.theme_strength_ranking(day)
    for item in index:
        if item["id"] in rankings:
            item["market_rank"] = rankings[item["id"]]
    result["index"] = {"data": {"themes": index}, "snapshot": {
        "state": "ready", "revision": revision, "built_at": built_at, "trade_date": day,
        "builder_pid": os.getpid(),
    }}
    result["_errors"] = errors
    return result


def build_section(db, identifier, section, day):
    if section == "core":
        theme = next(iter(db.list_themes(
            theme_id=identifier, focus_only=True, include_research=False,
        )), None)
        if theme:
            theme["deferred_sections"] = ["fund-flows", "research"]
            if theme["status"] in {"confirmed", "archived"}:
                theme["focus_scope"] = json.loads(db.get_metadata("direction_member_scope") or "{}")
        return theme
    if section == "research":
        return db.theme_research(identifier)
    theme = db.theme_flow_members(identifier)
    if theme:
        db.attach_theme_fund_flows([theme], day, "close")
        theme["fund_flow"] = compact_flow(theme["fund_flow"])
        for member in theme["members"]:
            member["fund_flow"] = compact_flow(member["fund_flow"])
    return theme


class DisplaySnapshots:
    def __init__(self, database, clock):
        self.db, self.clock = database, clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._pool = None

    def market_day(self):
        day = self.clock.china_now().strftime("%Y%m%d")
        dates = self.db.open_trade_dates(day, 1)
        return dates[0] if dates else day

    def start(self):
        with self._lock:
            if (self._thread is not None and self._thread.is_alive()) or self._stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._run, name="display-snapshot-coordinator", daemon=True,
            )
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def request(self, theme_id=None, sections=SECTIONS):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            enqueue(con, sections, theme_id)
        self._wake.set()

    def _state(self, con, envelope, section=None, theme_id=None):
        state = dict(envelope["snapshot"])
        stale = state["trade_date"] != self.market_day()
        if section:
            stale |= state["epoch"] != epoch(con, section, theme_id)
        else:
            stale |= state["revision"] != get_value(con, "revision", 0)
        state["state"] = "stale" if stale else "ready"
        state["last_error"] = get_value(con, "last_error")
        return state

    def read_index(self):
        # Only stored JSON and authoritative header columns; no ranking/quote/history work.
        with self.db.connect() as con:
            con.execute("BEGIN")
            saved = get_value(con, "index")
            current = headers(con)
            known = {r["id"]: r for r in saved["data"]["themes"]} if saved else {}
            items = []
            for row in current:
                item = {**known.get(row["id"], {"member_count": None}), **row}
                items.append(item)
            state = self._state(con, saved) if saved else {"state": "building"}
        return {"themes": items, "snapshot": state}

    def read_theme(self, identifier, section="core"):
        with self.db.connect() as con:
            con.execute("BEGIN")
            row = con.execute(
                "SELECT id,final_name,suggested_name,provisional_name,status,admission_status,"
                "theme_level,pinned,updated_at FROM candidate_themes WHERE id=?", (identifier,),
            ).fetchone()
            if not row:
                return None
            saved = get_value(con, f"theme:{identifier}:{section}")
            if not saved:
                return {"snapshot": {"state": "building"}, "id": identifier}
            result = copy.deepcopy(saved["data"])
            state = self._state(con, saved, section, identifier)
            if section == "core":
                result.update(dict(row))
                # Removing a holding or archiving cannot be undone by an old projection.
                active = {r[0] for r in con.execute(
                    "SELECT code FROM theme_members WHERE theme_id=? AND active=1", (identifier,),
                )}
                result["members"] = [m for m in result["members"] if m["code"] in active]
            return {**result, "snapshot": state}

    def status(self):
        with self.db.connect() as con:
            pending = get_value(con, "pending", {})
            return {"storage": "metadata", "builder": "separate_process",
                    "last_success": get_value(con, "last_success"),
                    "last_error": get_value(con, "last_error"),
                    "pending_sections": {k: len(v) for k, v in pending.items()}}

    def prepare(self, *, debounce=False):
        day = self.market_day()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if get_value(con, "market_day") != day:
                enqueue(con, source=True)
                put_value(con, "market_day", day)
            pending = get_value(con, "pending", {})
            if not pending:
                return None
            if debounce and time.time() - get_value(con, "changed_at", 0) < 0.5:
                return None
            revision = get_value(con, "revision", 0)
            ids = {str(r[0]) for r in con.execute(
                "SELECT id FROM candidate_themes WHERE status='confirmed'")}
            plan = {}
            for section, targets in pending.items():
                selected = ids | (set(targets) - {"*"}) if "*" in targets else set(targets)
                for identifier in selected:
                    plan.setdefault(identifier, []).append(section)
            # New/formal pools are eagerly built; historical records build on demand.
            for identifier in ids:
                if not get_value(con, f"theme:{identifier}:core"):
                    plan[identifier] = list(SECTIONS)
        return day, revision, pending, plan

    def publish(self, day, revision, pending, result):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if revision != get_value(con, "revision", 0):
                return False  # Source changed during computation: never publish the old state.
            errors = dict(result.get("_errors", {}))
            for key, value in result.items():
                if key == "_errors":
                    continue
                if key.endswith(":funds") and not usable_funds(value["data"]):
                    old = get_value(con, key)
                    if old and usable_funds(old["data"]):
                        errors[key] = "DataUnavailable"
                        continue
                put_value(con, key, value)
            remaining = get_value(con, "pending", {})
            for section, targets in pending.items():
                current = set(remaining.get(section, []))
                current -= set(targets)
                if current:
                    remaining[section] = sorted(current)
                else:
                    remaining.pop(section, None)
            for key in errors:
                _, identifier, section = key.split(":")
                remaining[section] = sorted(set(remaining.get(section, [])) | {identifier})
            put_value(con, "pending", remaining)
            put_value(con, "last_error", errors or None)
            put_value(con, "last_success", datetime.now(UTC).isoformat())
        return True

    def refresh_once(self, builder=build_projection):
        prepared = self.prepare()
        if not prepared:
            return False
        day, revision, pending, plan = prepared
        result = builder(str(self.db.path), str(self.db.archive_dir), day, revision, plan)
        return self.publish(day, revision, pending, result)

    def _run(self):
        try:
            self._pool = ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            while not self._stop.is_set():
                self._wake.wait(1)
                self._wake.clear()
                if self._stop.is_set():
                    break
                prepared = self.prepare(debounce=True)
                if not prepared:
                    continue
                day, revision, pending, plan = prepared
                run = self.db.begin_run("display_snapshot")
                started = time.perf_counter()
                try:
                    result = self._pool.submit(
                        build_projection, str(self.db.path), str(self.db.archive_dir),
                        day, revision, plan,
                    ).result()
                    published = self.publish(day, revision, pending, result)
                    if result.get("_errors"):
                        self._stop.wait(2)
                    self.db.finish_run(run, "success" if published else "superseded", len(plan))
                    logger.info("display_snapshot_build revision=%s themes=%s published=%s ms=%.1f",
                                revision, len(plan), published, (time.perf_counter()-started)*1000)
                except Exception as error:
                    # Keep the last good snapshot; no synchronous rebuild fallback.
                    self.db.set_metadata(PREFIX + "last_error", json.dumps(type(error).__name__))
                    self.db.finish_run(run, "failed", detail=type(error).__name__)
                    if isinstance(error, BrokenProcessPool) and not self._stop.is_set():
                        self._pool.shutdown(wait=False, cancel_futures=True)
                        self._pool = ProcessPoolExecutor(
                            max_workers=1, mp_context=multiprocessing.get_context("spawn"))
                    self._stop.wait(2)
        except Exception as error:
            logger.error("display_snapshot_worker_stopped type=%s", type(error).__name__)
        finally:
            if self._pool:
                self._pool.shutdown(wait=False, cancel_futures=True)
