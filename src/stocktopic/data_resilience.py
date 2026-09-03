from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime, time
from time import sleep
from typing import Any

from .providers.tushare import TushareClient
from .service import StockTopicService

_QUOTE_MIN_RAW_ROWS = 2000
_QUOTE_RETRY_DELAYS = (1.0, 2.0)
_DAILY_METRICS_READY_TIME = time(17, 20)
_DAILY_METRICS_ALERT_TIME = time(18, 0)

_installed = False


def install_data_resilience() -> None:
    """Install production guards for transient Tushare publication gaps.

    The production service is started with ``python -m stocktopic``. Installing the
    guards before the FastAPI app is imported keeps the core service deterministic
    while avoiding false WeCom critical alerts caused by short upstream empty windows.
    """
    global _installed
    if _installed:
        return
    _installed = True

    original_realtime_quotes = TushareClient.realtime_quotes
    original_data_pull_failure = StockTopicService._data_pull_failure
    original_collect_once = StockTopicService.collect_once
    original_sync_daily_metrics = StockTopicService.sync_daily_metrics
    original_run_scheduler = StockTopicService.run_scheduler

    def resilient_realtime_quotes(self: TushareClient, captured_at: datetime):
        best = original_realtime_quotes(self, captured_at)
        if len(best) >= _QUOTE_MIN_RAW_ROWS:
            return best
        for delay in _QUOTE_RETRY_DELAYS:
            sleep(delay)
            rows = original_realtime_quotes(self, captured_at)
            if len(rows) > len(best):
                best = rows
            if len(rows) >= _QUOTE_MIN_RAW_ROWS:
                return rows
        return best

    def resilient_data_pull_failure(
        self: StockTopicService,
        job_name: str,
        error: Exception,
        now: datetime | None = None,
    ) -> None:
        current = self.clock.normalize(now or self.clock.china_now())
        message = str(error)

        if (
            job_name == "sync_daily_metrics"
            and "Daily metric coverage abnormal" in message
            and current.time() < _DAILY_METRICS_ALERT_TIME
        ):
            self.database.set_metadata(
                "last_suppressed_daily_metrics_alert",
                f"{current.isoformat(timespec='seconds')}|{message}",
            )
            return

        if job_name == "collect_quotes" and "coverage abnormal" in message.lower():
            count = _quote_failure_streak(self, current)
            if count < 2:
                self.database.set_metadata(
                    "last_suppressed_quote_alert",
                    f"{current.isoformat(timespec='seconds')}|{message}",
                )
                return

        original_data_pull_failure(self, job_name, error, current)

    def resilient_collect_once(
        self: StockTopicService, now: datetime | None = None
    ) -> dict[str, Any]:
        result = original_collect_once(self, now)
        if result.get("status") != "failed":
            self.database.set_metadata(
                "quote_failure_streak",
                json.dumps({"count": 0}, ensure_ascii=False),
            )
        return result

    def resilient_sync_daily_metrics(self: StockTopicService, trade_date: str) -> int:
        current = self.clock.china_now()
        compact = str(trade_date).replace("-", "")
        if compact == current.strftime("%Y%m%d") and current.time() < _DAILY_METRICS_READY_TIME:
            run_id = self.database.begin_run("sync_daily_metrics")
            self.database.finish_run(
                run_id,
                "deferred",
                0,
                "current-day daily_basic publication window; retry scheduled after 17:20",
            )
            return 0
        return original_sync_daily_metrics(self, trade_date)

    async def resilient_run_scheduler(self: StockTopicService) -> None:
        companion = asyncio.create_task(
            _post_close_daily_metrics_loop(self),
            name="post-close-daily-metrics",
        )
        try:
            await original_run_scheduler(self)
        finally:
            companion.cancel()
            with suppress(asyncio.CancelledError):
                await companion

    TushareClient.realtime_quotes = resilient_realtime_quotes
    StockTopicService._data_pull_failure = resilient_data_pull_failure
    StockTopicService.collect_once = resilient_collect_once
    StockTopicService.sync_daily_metrics = resilient_sync_daily_metrics
    StockTopicService.run_scheduler = resilient_run_scheduler


def _quote_failure_streak(service: StockTopicService, current: datetime) -> int:
    raw = service.database.get_metadata("quote_failure_streak")
    try:
        state = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        state = {}

    previous_at = _parse_datetime(state.get("at"))
    previous_slot = str(state.get("slot") or "")
    current_slot = current.strftime("%Y%m%d%H%M")
    count = int(state.get("count") or 0)

    if previous_slot == current_slot:
        return max(1, count)

    consecutive = bool(
        previous_at
        and 0 < (current - previous_at).total_seconds() <= 10 * 60
    )
    count = count + 1 if consecutive else 1
    service.database.set_metadata(
        "quote_failure_streak",
        json.dumps(
            {
                "count": count,
                "slot": current_slot,
                "at": current.isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
    )
    return count


def _parse_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


async def _post_close_daily_metrics_loop(service: StockTopicService) -> None:
    while not service._stop_event.is_set():
        now = service.clock.china_now()
        compact = now.strftime("%Y%m%d")
        is_open = service.database.calendar_status(compact) is True
        within_window = _DAILY_METRICS_READY_TIME <= now.time() <= _DAILY_METRICS_ALERT_TIME
        synced = service.database.get_metadata("daily_metrics_synced_date") == compact

        if is_open and within_window and not synced and now.minute % 5 == 0 and now.second < 25:
            slot = now.strftime("%H:%M")
            attempt_key = f"daily_metrics_attempt:{now.date().isoformat()}:{slot}"
            if not service.database.get_metadata(attempt_key):
                service.database.set_metadata(attempt_key, now.isoformat(timespec="seconds"))
                await asyncio.to_thread(service.sync_daily_metrics, compact)

        try:
            await asyncio.wait_for(service._stop_event.wait(), timeout=10.0)
        except TimeoutError:
            pass
