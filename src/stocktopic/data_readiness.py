"""Trading-date readiness for live qualification and delayed official evidence."""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, time, timedelta
from typing import Any

from .market_clock import MarketClock


class DiscoveryDataReadiness:
    def __init__(self, service: Any):
        self.service = service
        self.db = service.database
        self.lock = threading.Lock()
        self._date_locks: dict[str, Any] = {}

    def limit_coverage(self, day: str) -> dict:
        iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        universe = self.db.active_stock_map()
        main = {code for code, stock in universe.items() if stock.get("market") == "主板"}
        limits = self.db.daily_limit_map(iso)
        valid = {code for code in main if self._valid_upper(limits.get(code, (None, None))[0])}
        return {
            "trade_date": day,
            "expected_mainboard_count": len(main),
            "valid_mainboard_count": len(valid),
            "coverage_ratio": round(len(valid) / len(main), 4) if main else None,
            "missing_codes_sample": sorted(main - valid)[:30],
            "ready": bool(main) and len(valid) >= len(main) * 0.95,
        }

    def ensure_limits(self, day: str, now: datetime) -> dict:
        current = MarketClock.normalize(now)
        with self.lock:
            date_lock = self._date_locks.setdefault(day, threading.Lock())
        with date_lock:
            coverage = self.limit_coverage(day)
            key = f"daily_limit_readiness:{day}"
            previous = json.loads(self.db.get_metadata(key) or "{}")
            attempt_key = f"daily_limit_attempt:{day}"
            last = self.db.get_metadata(attempt_key)
            pending = day == current.strftime("%Y%m%d") and current.time() < time(9)
            cooldown = last and (current - datetime.fromisoformat(last)).total_seconds() < 300
            if not coverage["ready"] and not pending and not cooldown:
                self.db.set_metadata(attempt_key, current.isoformat())
                requested = datetime.strptime(day, "%Y%m%d").replace(
                    hour=current.hour,
                    minute=current.minute,
                    tzinfo=current.tzinfo,
                )
                count = self.service.sync_daily_limits(requested)
                coverage = self.limit_coverage(day)
                if count and not coverage["ready"] and previous.get("state") != "incomplete":
                    self.service._data_pull_failure(
                        "daily_limit_coverage",
                        RuntimeError(
                            f"{day} mainboard official upper-limit coverage "
                            f"{coverage['valid_mainboard_count']}/"
                            f"{coverage['expected_mainboard_count']}"
                        ),
                        current,
                    )
            coverage.update(
                state="ready"
                if coverage["ready"]
                else "pending_publication"
                if pending
                else "incomplete",
                checked_at=current.isoformat(timespec="seconds"),
                retry_after_seconds=300,
            )
            self.db.set_metadata(key, json.dumps(coverage, ensure_ascii=False))
            return coverage

    def kpl_for_date(self, day: str, now: datetime) -> int:
        current = MarketClock.normalize(now)
        today = current.strftime("%Y%m%d")
        age = (current.date() - datetime.strptime(day, "%Y%m%d").date()).days
        if day >= today or (age == 1 and current.time() < time(6)):
            self.db.set_metadata(
                f"kpl_event_coverage:{day}",
                json.dumps(
                    {
                        "trade_date": day,
                        "state": "pending_publication",
                        "row_count": None,
                        "checked_at": current.isoformat(timespec="seconds"),
                        "reason": "official_kpl_publishes_previous_trade_date_after_06_00",
                    }
                ),
            )
            return 0
        return self.service.sync_kpl_events(day)

    def signal_coverage(self, day: str, now: datetime, quotes: list, limits: dict) -> dict:
        stocks = self.db.active_stock_map()
        expected = sum(s.get("market") == "主板" for s in stocks.values())
        main = [q for q in quotes if stocks.get(q.code, {}).get("market") == "主板"]
        known = {q.code for q in main if self._valid_upper(limits.get(q.code, (None, None))[0])}
        kpl = json.loads(self.db.get_metadata(f"kpl_event_coverage:{day}") or "{}")
        ready = json.loads(self.db.get_metadata(f"daily_limit_readiness:{day}") or "{}")
        live_ready = bool(ready.get("ready")) and bool(expected) and len(known) >= expected * 0.95
        value = {
            "trade_date": day,
            "checked_at": now.isoformat(timespec="seconds"),
            "expected_mainboard_count": expected,
            "mainboard_quote_count": len(main),
            "mainboard_with_limit_count": len(known),
            "mainboard_missing_limit_count": len(main) - len(known),
            "mainboard_available": live_ready or kpl.get("state") == "available",
            "strong_input_counts": self.db.strong_input_counts(day),
        }
        self.db.set_metadata(f"market_signal_coverage:{day}", json.dumps(value))
        return value

    @staticmethod
    def _valid_upper(value: Any) -> bool:
        try:
            return math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError):
            return False

    def due_completed_dates(self, now: datetime) -> list[str]:
        current = MarketClock.normalize(now)
        if current.time() < time(6, 10):
            return []
        dates = self.db.open_trade_dates(current.strftime("%Y%m%d"), 3)
        due = []
        for day in dates:
            if day >= current.strftime("%Y%m%d"):
                continue
            state = json.loads(self.db.get_metadata(f"completed_sources:{day}") or "{}")
            if state.get("state") == "ready":
                continue
            last = state.get("attempted_at")
            if last and current - datetime.fromisoformat(last) < timedelta(minutes=30):
                continue
            due.append(day)
        return due[:2]

    def reconcile(self, day: str, now: datetime) -> dict:
        current = MarketClock.normalize(now)
        if day >= current.strftime("%Y%m%d"):
            return {"state": "pending_publication", "trade_date": day}
        key = f"completed_sources:{day}"
        existing = json.loads(self.db.get_metadata(key) or "{}")
        if existing.get("state") == "ready":
            return existing
        result = {"trade_date": day, "state": "incomplete", "attempted_at": current.isoformat()}
        self.db.set_metadata(key, json.dumps(result))
        run = self.db.begin_run("reconcile_completed_sources")
        try:
            coverage = self.ensure_limits(day, current)
            kpl_count = self.kpl_for_date(day, current)
            self.service.sync_kpl_concepts(day)
            kpl = json.loads(self.db.get_metadata(f"kpl_event_coverage:{day}") or "{}")
            concepts = json.loads(self.db.get_metadata(f"kpl_concept_coverage:{day}") or "{}")
            if self.db.get_metadata(f"daily_prices_synced:{day}") != "true":
                self.service.sync_daily_prices(day)
            replayed = self.db.backfill_mainboard_limit_events(day, current)
            self.db.upsert_chinext_daily_growth_events(day, self.db.daily_bars_for_date(day))
            result.update(
                daily_limits=coverage,
                kpl_rows=kpl_count,
                kpl_concepts=concepts,
                mainboard_replayed=replayed,
                daily_prices_ready=self.db.get_metadata(f"daily_prices_synced:{day}") == "true",
            )
            if (
                coverage["ready"]
                and kpl.get("state") == "available"
                and concepts.get("state") in {"available", "empty"}
                and result["daily_prices_ready"]
            ):
                result["state"] = "ready"
            # This job repairs source evidence only. It never runs historical AI or sends
            # retrospective opportunity notifications with backdated discovery times.
            self.db.finish_run(
                run,
                "success" if result["state"] == "ready" else "degraded",
                replayed,
                json.dumps(result, ensure_ascii=False),
            )
        except Exception as error:
            from .service import _safe_error

            result["error"] = _safe_error(error)
            self.db.finish_run(run, "degraded", detail=result["error"])
            self.service._data_pull_failure("reconcile_completed_sources", error, current)
        self.db.set_metadata(key, json.dumps(result, ensure_ascii=False))
        return result
