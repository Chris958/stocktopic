from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from datetime import datetime, time, timedelta
from typing import Any

from .ai import OpenAIThemeExplainer
from .anomaly import AnomalyDetector
from .config import Settings
from .db import Database
from .domain import StockContext
from .market_clock import MarketClock
from .providers import TushareClient
from .scoring import ThemeScorer
from .themes import ThemeDiscovery
from .wecom import WeComNotifier

logger = logging.getLogger(__name__)


class StockTopicService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.db_path, settings.archive_dir)
        self.provider = TushareClient(settings.tushare_token)
        self.detector = AnomalyDetector()
        self.discovery = ThemeDiscovery(self.database)
        self.scorer = ThemeScorer(self.database)
        self.explainer = OpenAIThemeExplainer(
            settings.openai_api_key, settings.openai_model
        )
        self.notifier = WeComNotifier(
            settings.wecom_corp_id,
            settings.wecom_agent_id,
            settings.wecom_secret,
            settings.wecom_to_user,
        )
        self.clock = MarketClock()
        self._collector_lock = threading.Lock()
        self._ai_lock = threading.Lock()
        self._ai_inflight: set[int] = set()
        self._stop_event = asyncio.Event()

    def initialize(self) -> None:
        self.settings.ensure_directories()
        self.database.initialize()
        self.bootstrap_reference_data()

    def bootstrap_reference_data(self) -> None:
        now = self.clock.china_now()
        compact = now.strftime("%Y%m%d")
        calendar_status = self.database.calendar_status(compact)
        if calendar_status is None:
            try:
                self.sync_calendar(now)
            except Exception:
                logger.exception("Initial calendar sync failed; collector remains fail-closed")
        universe_last = self.database.get_metadata("universe_synced_at")
        if not universe_last or datetime.fromisoformat(universe_last).date() < now.date():
            try:
                self.sync_universe()
            except Exception:
                logger.exception("Initial universe sync failed")
        previous_date = self.database.previous_trade_date(compact)
        if (
            previous_date
            and self.database.get_metadata("kpl_concepts_synced_date") != previous_date
        ):
            self.sync_kpl_concepts(previous_date)
        if self.database.calendar_status(compact):
            self.sync_daily_limits(now)

    def sync_calendar(self, now: datetime | None = None) -> int:
        now = now or self.clock.china_now()
        run_id = self.database.begin_run("sync_calendar")
        try:
            rows = self.provider.trade_calendar(
                f"{now.year}0101", f"{now.year}1231"
            )
            if len(rows) < 200:
                raise RuntimeError(f"Trading calendar coverage abnormal: {len(rows)} rows")
            self.database.replace_calendar(rows)
            self.database.finish_run(run_id, "success", len(rows))
            return len(rows)
        except Exception as error:
            self.database.finish_run(run_id, "failed", detail=str(error))
            raise

    def sync_universe(self) -> int:
        run_id = self.database.begin_run("sync_universe")
        try:
            rows = self.provider.stock_basic()
            count = self.database.upsert_stocks(rows)
            if count < 2000:
                raise RuntimeError(f"Main-board universe coverage abnormal: {count}")
            self.database.set_metadata(
                "universe_synced_at", datetime.now().astimezone().isoformat()
            )
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "failed", detail=str(error))
            raise

    def sync_daily_limits(self, now: datetime | None = None) -> int:
        now = now or self.clock.china_now()
        compact = now.strftime("%Y%m%d")
        run_id = self.database.begin_run("sync_daily_limits")
        try:
            rows = self.provider.stock_limits(compact)
            count = self.database.upsert_daily_limits(now.date().isoformat(), rows)
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            # Limit data improves accuracy but quote collection can still run without it.
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("Daily limit sync degraded: %s", error)
            return 0

    def sync_kpl_events(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_kpl_events")
        try:
            rows = []
            for tag in ("涨停", "炸板", "跌停"):
                rows.extend(self.provider.kpl_list(trade_date, tag))
            count = self.database.upsert_kpl_events(rows)
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("KPL event sync degraded: %s", error)
            return 0

    def sync_kpl_concepts(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_kpl_concepts")
        try:
            rows = self.provider.kpl_concept_members(trade_date)
            count = self.database.upsert_kpl_concept_members(rows)
            self.database.set_metadata("kpl_concepts_synced_date", trade_date)
            self.database.set_metadata(
                "kpl_concepts_may_be_truncated", "true" if len(rows) >= 3000 else "false"
            )
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("KPL concept sync degraded: %s", error)
            return 0

    def collect_once(self, now: datetime | None = None) -> dict[str, Any]:
        with self._collector_lock:
            started = self.clock.normalize(now or self.clock.china_now())
            compact = started.strftime("%Y%m%d")
            state = self.clock.state(started, self.database.calendar_status(compact))
            if not state.in_realtime_window:
                return {"status": "idle", "reason": state.reason, "session": state.session}
            if not state.slot:
                return {"status": "idle", "reason": "no_valid_slot"}
            slot_key = f"{started.date().isoformat()}-{state.slot}"
            if self.database.get_metadata("last_quote_slot") == slot_key:
                return {"status": "duplicate_skipped", "slot": state.slot}

            run_id = self.database.begin_run("collect_quotes")
            try:
                raw_quotes = self.provider.realtime_quotes(started)
                universe = self.database.active_stock_map()
                quotes = [quote for quote in raw_quotes if quote.code in universe]
                if len(quotes) < 2000:
                    raise RuntimeError(
                        f"Quote coverage abnormal: raw={len(raw_quotes)}, main={len(quotes)}"
                    )
                signature = hashlib.sha256(
                    f"{sum(q.amount for q in quotes):.0f}|{sum(q.volume for q in quotes)}".encode()
                ).hexdigest()
                previous_signature = self.database.get_metadata("last_quote_signature")
                unchanged = previous_signature == signature
                if (
                    unchanged
                    and state.session in {"morning", "afternoon"}
                    and state.slot not in {"09:30", "13:00"}
                ):
                    self._data_stale_alert(started, state.slot)
                    raise RuntimeError("Full-market cumulative amount and volume did not change")

                trade_date = started.date().isoformat()
                history = self.database.latest_quote_history(trade_date, depth=2)
                limits = self.database.daily_limit_map(trade_date)
                anomalies = []
                for quote in quotes:
                    upper, lower = limits.get(quote.code, (None, None))
                    stock = universe[quote.code]
                    anomalies.extend(
                        self.detector.detect(
                            quote,
                            StockContext(
                                code=quote.code,
                                name=quote.name,
                                list_date=str(stock.get("list_date") or ""),
                                upper_limit=upper,
                                lower_limit=lower,
                            ),
                            history.get(quote.code, []),
                        )
                    )
                self.database.save_quotes(quotes, trade_date, state.slot)
                self.database.save_anomalies(anomalies)
                self.database.set_metadata("last_quote_slot", slot_key)
                self.database.set_metadata("last_quote_signature", signature)
                self.database.set_metadata(
                    "last_fresh_quote_at", started.isoformat(timespec="seconds")
                )

                high_impact = sum(
                    event.is_hard_event and event.direction.value == "positive"
                    for event in anomalies
                )
                should_cluster = started.minute % self.settings.cluster_interval_minutes == 0
                candidate_ids: list[int] = []
                if should_cluster or high_impact >= 2:
                    self.sync_kpl_events(compact)
                    candidate_ids = self.discovery.discover(started)
                scored_ids = self.scorer.score_confirmed(started)
                self._evaluate_theme_alerts(started, scored_ids, stale=False)
                self.database.finish_run(
                    run_id,
                    "success",
                    len(quotes),
                    f"anomalies={len(anomalies)}, candidates={len(candidate_ids)}",
                )
                return {
                    "status": "success",
                    "slot": state.slot,
                    "quotes": len(quotes),
                    "anomalies": len(anomalies),
                    "candidate_ids": candidate_ids,
                    "scored_ids": scored_ids,
                }
            except Exception as error:
                self.database.finish_run(run_id, "failed", detail=str(error))
                logger.exception("Quote collection failed")
                return {"status": "failed", "slot": state.slot, "error": str(error)}

    def _enrich_new_candidates(self, theme_ids: list[int]) -> None:
        if not self.explainer.enabled:
            return
        with self._ai_lock:
            selected = [
                theme_id
                for theme_id in dict.fromkeys(theme_ids)
                if theme_id not in self._ai_inflight
            ]
            self._ai_inflight.update(selected)
        themes = {int(item["id"]): item for item in self.database.list_themes()}
        other_names = [
            str(item.get("final_name") or item.get("suggested_name") or item["provisional_name"])
            for item in themes.values()
        ]
        try:
            for theme_id in selected:
                if self.database.has_ai_explanation(theme_id):
                    continue
                theme = themes.get(theme_id)
                if not theme:
                    continue
                try:
                    item = self.explainer.explain(theme, other_names)
                    self.database.save_ai_explanation(theme_id, item)
                    self.database.set_suggested_name(theme_id, item["suggested_name"])
                except Exception as error:
                    logger.warning("AI explanation failed for theme %s: %s", theme_id, error)
        finally:
            with self._ai_lock:
                self._ai_inflight.difference_update(selected)

    def _evaluate_theme_alerts(
        self, now: datetime, theme_ids: list[int], stale: bool
    ) -> None:
        themes = {int(item["id"]): item for item in self.database.list_themes(status="confirmed")}
        for theme_id in theme_ids:
            theme = themes.get(theme_id)
            if not theme or not theme.get("score"):
                continue
            score = theme["score"]
            name = (
                theme.get("final_name")
                or theme.get("suggested_name")
                or theme["provisional_name"]
            )
            if (
                not stale
                and float(score["heat"]) >= 55
                and float(score["persistence"]) >= 70
                and float(score["entry_risk"]) <= 45
            ):
                self._send_alert(
                    dedupe_key=f"opportunity:{theme_id}:{now.strftime('%Y%m%d%H')}",
                    category="high_value_opportunity",
                    severity="high",
                    title=f"题材机会：{name}",
                    body=(
                        f"Heat {score['heat']} · Persistence {score['persistence']} · "
                        f"Risk {score['entry_risk']} · {score['lifecycle']}"
                    ),
                    theme_id=theme_id,
                )
            if float(score["entry_risk"]) >= 75 or score["leader_theme_divergence"]:
                warning = "龙头—板块背离" if score["leader_theme_divergence"] else "接盘风险过高"
                self._send_alert(
                    dedupe_key=f"risk:{theme_id}:{now.strftime('%Y%m%d%H')}",
                    category="high_risk",
                    severity="critical",
                    title=f"题材风险：{name}",
                    body=f"{warning}，Entry Risk {score['entry_risk']}，当前不宜追高。",
                    theme_id=theme_id,
                )

    def _data_stale_alert(self, now: datetime, slot: str) -> None:
        self._send_alert(
            dedupe_key=f"stale:{now.strftime('%Y%m%d')}:{slot}",
            category="data_stale",
            severity="critical",
            title="行情数据陈旧",
            body="全市场累计成交量与成交额未变化，已熔断本周期机会信号。",
        )

    def _send_alert(
        self,
        *,
        dedupe_key: str,
        category: str,
        severity: str,
        title: str,
        body: str,
        theme_id: int | None = None,
        code: str | None = None,
    ) -> None:
        alert_id = self.database.create_alert(
            dedupe_key, category, severity, title, body, theme_id, code
        )
        if alert_id is None or not self.notifier.enabled:
            return
        try:
            self.notifier.send_text(title, body)
            self.database.mark_alert_pushed(alert_id)
        except Exception as error:
            self.database.mark_alert_pushed(alert_id, str(error))
            logger.warning("WeCom push failed: %s", error)

    def end_of_day(self, now: datetime | None = None) -> dict[str, Any]:
        now = self.clock.normalize(now or self.clock.china_now())
        compact = now.strftime("%Y%m%d")
        if not self.database.calendar_status(compact):
            return {"status": "idle", "reason": "exchange_closed"}
        run_key = f"eod_completed:{now.date().isoformat()}"
        if self.database.get_metadata(run_key):
            return {"status": "duplicate_skipped"}
        run_id = self.database.begin_run("end_of_day")
        try:
            self.sync_daily_limits(now)
            self.sync_kpl_events(compact)
            self.sync_kpl_concepts(compact)
            cohort_count = self._record_daily_cohorts(now)
            backup = self.database.backup()
            self.database.prune_backups()
            archived = self.database.archive_quotes_before(now.date() - timedelta(days=120))
            self.database.set_metadata(run_key, now.isoformat())
            self.database.finish_run(
                run_id,
                "success",
                len(archived),
                f"backup={backup.name}, cohorts={cohort_count}",
            )
            return {
                "status": "success",
                "backup": backup.name,
                "archived": len(archived),
                "cohorts": cohort_count,
            }
        except Exception as error:
            self.database.finish_run(run_id, "failed", detail=str(error))
            return {"status": "failed", "error": str(error)}

    def _record_daily_cohorts(self, now: datetime) -> int:
        compact = now.strftime("%Y%m%d")
        trade_date = now.date().isoformat()
        quotes = {str(row["code"]): row for row in self.database.latest_quotes()}
        previous = self.database.previous_trade_date(compact)
        if previous:
            self.database.update_cohort_next_day_returns(
                previous,
                {code: float(row["pct_change"]) for code, row in quotes.items()},
            )
        total = 0
        for theme in self.database.list_themes(status="confirmed"):
            members = [member for member in theme["members"] if member.get("active", 1)]
            codes = [str(member["code"]) for member in members]
            kpl = self.database.kpl_events_for_codes(compact, codes)
            by_code = {str(event["code"]): event for event in kpl}
            cohort = []
            for member in members:
                code = str(member["code"])
                event = by_code.get(code)
                quote = quotes.get(code, {})
                outcome = str(event["board_tag"]) if event else "普通"
                if not event and float(quote.get("pct_change", 0)) <= -5:
                    outcome = "大幅负反馈"
                cohort.append(
                    {
                        "code": code,
                        "board_level": _parse_board_height(event.get("status") if event else None),
                        "outcome": outcome,
                    }
                )
            self.database.record_cohort(int(theme["id"]), trade_date, cohort)
            total += len(cohort)
        return total

    async def run_scheduler(self) -> None:
        while not self._stop_event.is_set():
            now = self.clock.china_now()
            compact = now.strftime("%Y%m%d")
            calendar = self.database.calendar_status(compact)
            if calendar is None:
                try:
                    await asyncio.to_thread(self.sync_calendar, now)
                    calendar = self.database.calendar_status(compact)
                except Exception:
                    logger.exception("Calendar unavailable; collector remains fail-closed")
            state = self.clock.state(now, calendar)
            if state.in_realtime_window and state.slot and now.second < 25:
                result = await asyncio.to_thread(self.collect_once, now)
                candidate_ids = result.get("candidate_ids", [])
                if candidate_ids and self.explainer.enabled:
                    asyncio.create_task(
                        asyncio.to_thread(self._enrich_new_candidates, candidate_ids),
                        name=f"theme-ai-{now.strftime('%H%M')}",
                    )
            elif calendar and time(15, 5) <= now.time() <= time(15, 20):
                await asyncio.to_thread(self.end_of_day, now)
            current = self.clock.china_now()
            delay = max(0.25, 10.0 - (current.second % 10) - current.microsecond / 1_000_000)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    def health(self) -> dict[str, Any]:
        now = self.clock.china_now()
        compact = now.strftime("%Y%m%d")
        state = self.clock.state(now, self.database.calendar_status(compact))
        latest = self.database.latest_run("collect_quotes")
        return {
            "status": "ok" if self.database.integrity_check() == "ok" else "degraded",
            "china_time": now.isoformat(timespec="seconds"),
            "market": {
                "is_open_day": state.is_open_day,
                "session": state.session,
                "realtime_collection_enabled": state.in_realtime_window,
                "reason": state.reason,
            },
            "universe_count": self.database.active_stock_count(),
            "latest_quote_run": latest,
            "integrations": {
                "tushare": True,
                "openai": self.explainer.enabled,
                "wecom": self.notifier.enabled,
                "apns": False,
            },
            "warnings": self.settings.validate_integrations(),
        }


def _parse_board_height(status: Any) -> int:
    value = str(status or "")
    if "首板" in value:
        return 1
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0
