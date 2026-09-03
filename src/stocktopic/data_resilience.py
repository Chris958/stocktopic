from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, time
from time import sleep
from typing import Any

from .providers.tushare import TushareClient, TushareError
from .service import StockTopicService

_QUOTE_MIN_RAW_ROWS = 2000
_QUOTE_RETRY_DELAYS = (1.0, 2.0)
_TUSHARE_NETWORK_RETRY_DELAYS = (1.0, 3.0)
_DAILY_METRICS_READY_TIME = time(17, 20)
_DAILY_METRICS_ALERT_TIME = time(18, 0)
_NETWORK_ALERT_AFTER_SECONDS = 10 * 60
_NETWORK_INCIDENT_GAP_SECONDS = 20 * 60

_installed = False
_last_tushare_success_at: datetime | None = None


def install_data_resilience() -> None:
    """Install guards for transient Tushare publication and network gaps."""
    global _installed
    if _installed:
        return
    _installed = True

    original_tushare_call = TushareClient.call
    original_realtime_quotes = TushareClient.realtime_quotes
    original_data_pull_failure = StockTopicService._data_pull_failure
    original_collect_once = StockTopicService.collect_once
    original_sync_daily_metrics = StockTopicService.sync_daily_metrics
    original_sync_kpl_concepts = StockTopicService.sync_kpl_concepts
    original_run_scheduler = StockTopicService.run_scheduler

    def resilient_tushare_call(
        self: TushareClient,
        api_name: str,
        params: Mapping[str, Any] | None = None,
        fields: str = "",
    ) -> list[dict[str, Any]]:
        try:
            result = original_tushare_call(self, api_name, params, fields)
            _mark_tushare_success()
            return result
        except TushareError as error:
            if str(error.code) != "network":
                raise
            last_error = error

        for delay in _TUSHARE_NETWORK_RETRY_DELAYS:
            sleep(delay)
            try:
                result = original_tushare_call(self, api_name, params, fields)
                _mark_tushare_success()
                return result
            except TushareError as error:
                if str(error.code) != "network":
                    raise
                last_error = error
        raise last_error

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

        if _is_tushare_network_error(message):
            _mark_network_retry(self, job_name)
            state = _record_tushare_network_failure(self, job_name, current, message)
            if state["should_alert"]:
                jobs = "、".join(state["jobs"])
                duration = int(state["duration_seconds"] // 60)
                summary = RuntimeError(
                    f"Tushare network/DNS unavailable for {duration} minutes; "
                    f"affected tasks: {jobs}; last error: {message}"
                )
                original_data_pull_failure(self, "tushare_network", summary, current)
            else:
                self.database.set_metadata(
                    "last_suppressed_tushare_network_alert",
                    f"{current.isoformat(timespec='seconds')}|{job_name}|{message}",
                )
            return

        if _is_dependent_network_failure(self, job_name, message, current):
            self.database.set_metadata("discovery_backfill_retry_pending", "true")
            self.database.set_metadata(
                "last_suppressed_discovery_backfill_alert",
                f"{current.isoformat(timespec='seconds')}|{message}",
            )
            return

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

    def resilient_sync_kpl_concepts(self: StockTopicService, trade_date: str) -> int:
        compact = str(trade_date).replace("-", "")
        self.database.set_metadata("active_kpl_concepts_trade_date", compact)
        try:
            result = original_sync_kpl_concepts(self, compact)
        finally:
            self.database.set_metadata("active_kpl_concepts_trade_date", "")
        if self.database.get_metadata("kpl_concepts_synced_date") == compact:
            self.database.set_metadata("kpl_concepts_retry_date", "")
        return result

    async def resilient_run_scheduler(self: StockTopicService) -> None:
        companions = [
            asyncio.create_task(
                _post_close_daily_metrics_loop(self),
                name="post-close-daily-metrics",
            ),
            asyncio.create_task(
                _tushare_recovery_loop(self),
                name="tushare-network-recovery",
            ),
        ]
        try:
            await original_run_scheduler(self)
        finally:
            for companion in companions:
                companion.cancel()
            for companion in companions:
                with suppress(asyncio.CancelledError):
                    await companion

    TushareClient.call = resilient_tushare_call
    TushareClient.realtime_quotes = resilient_realtime_quotes
    StockTopicService._data_pull_failure = resilient_data_pull_failure
    StockTopicService.collect_once = resilient_collect_once
    StockTopicService.sync_daily_metrics = resilient_sync_daily_metrics
    StockTopicService.sync_kpl_concepts = resilient_sync_kpl_concepts
    StockTopicService.run_scheduler = resilient_run_scheduler


def _mark_tushare_success() -> None:
    global _last_tushare_success_at
    _last_tushare_success_at = datetime.now().astimezone()


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
        previous_at and 0 < (current - previous_at).total_seconds() <= 10 * 60
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


def _record_tushare_network_failure(
    service: StockTopicService,
    job_name: str,
    current: datetime,
    message: str,
) -> dict[str, Any]:
    raw = service.database.get_metadata("tushare_network_outage")
    try:
        state = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        state = {}

    previous_last = _parse_datetime(state.get("last_at"))
    recovered_between = bool(
        _last_tushare_success_at
        and previous_last
        and _last_tushare_success_at > previous_last
    )
    gap_too_long = bool(
        previous_last
        and (current - previous_last).total_seconds() > _NETWORK_INCIDENT_GAP_SECONDS
    )
    if recovered_between or gap_too_long:
        state = {}

    first_at = _parse_datetime(state.get("first_at")) or current
    jobs = sorted({*state.get("jobs", []), job_name})
    duration_seconds = max(0.0, (current - first_at).total_seconds())
    alerted = bool(state.get("alerted"))
    should_alert = duration_seconds >= _NETWORK_ALERT_AFTER_SECONDS and not alerted
    if should_alert:
        alerted = True

    stored = {
        "first_at": first_at.isoformat(timespec="seconds"),
        "last_at": current.isoformat(timespec="seconds"),
        "jobs": jobs,
        "last_error": message,
        "alerted": alerted,
    }
    service.database.set_metadata(
        "tushare_network_outage",
        json.dumps(stored, ensure_ascii=False),
    )
    return {
        **stored,
        "duration_seconds": duration_seconds,
        "should_alert": should_alert,
    }


def _mark_network_retry(service: StockTopicService, job_name: str) -> None:
    if job_name == "sync_daily_limits":
        service.database.set_metadata("daily_limits_retry_pending", "true")
    elif job_name in {"sync_kpl_events", "discovery_backfill"}:
        service.database.set_metadata("discovery_backfill_retry_pending", "true")
    elif job_name == "sync_kpl_concepts":
        trade_date = service.database.get_metadata("active_kpl_concepts_trade_date")
        if trade_date:
            service.database.set_metadata("kpl_concepts_retry_date", str(trade_date))
    elif job_name == "sync_daily_prices":
        service.database.set_metadata("test_pool_price_retry_pending", "true")
    elif job_name == "sync_universe":
        service.database.set_metadata("universe_retry_pending", "true")


def _is_dependent_network_failure(
    service: StockTopicService,
    job_name: str,
    message: str,
    current: datetime,
) -> bool:
    if job_name != "discovery_backfill":
        return False
    if not (
        "KPL events unavailable" in message
        or "ChiNext daily growth signals unavailable" in message
    ):
        return False
    state = _network_outage_state(service)
    last_at = _parse_datetime(state.get("last_at"))
    return bool(last_at and 0 <= (current - last_at).total_seconds() <= 10 * 60)


def _network_outage_state(service: StockTopicService) -> dict[str, Any]:
    raw = service.database.get_metadata("tushare_network_outage")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _is_tushare_network_error(message: str) -> bool:
    normalized = message.lower()
    return "tushare error network" in normalized or "urlopen error" in normalized


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


async def _tushare_recovery_loop(service: StockTopicService) -> None:
    while not service._stop_event.is_set():
        now = service.clock.china_now()
        due = now.minute % 5 == 0 and now.second < 25
        outage = _network_outage_state(service)
        retry_date = str(service.database.get_metadata("kpl_concepts_retry_date") or "")
        pending = any(
            service.database.get_metadata(key) == "true"
            for key in (
                "daily_limits_retry_pending",
                "discovery_backfill_retry_pending",
                "test_pool_price_retry_pending",
                "universe_retry_pending",
            )
        )

        if due and (outage or retry_date or pending):
            slot = now.strftime("%Y%m%d%H%M")
            attempt_key = f"tushare_recovery_attempt:{slot}"
            if not service.database.get_metadata(attempt_key):
                service.database.set_metadata(attempt_key, now.isoformat(timespec="seconds"))
                await _attempt_tushare_recovery(service, now)

        try:
            await asyncio.wait_for(service._stop_event.wait(), timeout=10.0)
        except TimeoutError:
            pass


async def _attempt_tushare_recovery(service: StockTopicService, now: datetime) -> None:
    compact = now.strftime("%Y%m%d")
    try:
        await asyncio.to_thread(service.provider.trade_calendar, compact, compact)
    except TushareError as error:
        service._data_pull_failure("tushare_network_probe", error, now)
        return

    previous = _network_outage_state(service)
    if previous:
        service.database.set_metadata(
            "last_tushare_network_recovery",
            f"{now.isoformat(timespec='seconds')}|jobs={','.join(previous.get('jobs', []))}",
        )
        service.database.set_metadata("tushare_network_outage", "")

    if service.database.get_metadata("universe_retry_pending") == "true":
        if await asyncio.to_thread(service.sync_universe) >= 2000:
            service.database.set_metadata("universe_retry_pending", "false")

    if (
        service.database.get_metadata("daily_limits_retry_pending") == "true"
        and service.database.calendar_status(compact) is True
    ):
        if await asyncio.to_thread(service.sync_daily_limits, now) > 0:
            service.database.set_metadata("daily_limits_retry_pending", "false")

    retry_date = str(service.database.get_metadata("kpl_concepts_retry_date") or "")
    if retry_date:
        await asyncio.to_thread(service.sync_kpl_concepts, retry_date)

    if service.database.get_metadata("discovery_backfill_retry_pending") == "true":
        result = await asyncio.to_thread(
            service.backfill_recent_trade_days,
            now,
            refresh_sources=True,
            source="network_recovery",
        )
        if result.get("status") == "success":
            service.database.set_metadata("discovery_backfill_retry_pending", "false")

    if service.database.get_metadata("test_pool_price_retry_pending") == "true":
        await asyncio.to_thread(service.refresh_test_pool_prices, now)
        service.database.set_metadata("test_pool_price_retry_pending", "false")
