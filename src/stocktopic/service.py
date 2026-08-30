from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .ai import OpenAIThemeExplainer
from .anomaly import AnomalyDetector
from .backtest import PaperTradeTracker
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
        self.discovery = ThemeDiscovery(
            self.database,
            settings.minimum_limit_touches,
        )
        self.scorer = ThemeScorer(self.database)
        self.test_pool = PaperTradeTracker(self.database)
        self.explainer = OpenAIThemeExplainer(
            settings.openai_api_key,
            settings.openai_model,
            settings.openai_base_url,
        )
        self.notifier = WeComNotifier(
            settings.wecom_corp_id,
            settings.wecom_agent_id,
            settings.wecom_secret,
            settings.wecom_to_user,
        )
        self.clock = MarketClock()
        self._collector_lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._ai_lock = threading.Lock()
        self._ai_inflight: set[int] = set()
        self._discovery_inflight: set[str] = set()
        self._startup_backfill_pending = True
        self._stop_event = asyncio.Event()

    def initialize(self) -> None:
        self.settings.ensure_directories()
        self.database.initialize()
        if not self.database.get_metadata("admission_v2_legacy_reclassified"):
            result = self.database.reclassify_legacy_pending(self.settings.minimum_limit_touches)
            self.database.set_metadata(
                "admission_v2_legacy_reclassified",
                f"awaiting_ai={result['awaiting_ai']},failed={result['failed']}",
            )
        self.bootstrap_reference_data()
        self.refresh_test_pool_prices(self.clock.china_now())

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
        recent_dates = self.database.open_trade_dates(compact, 6)
        current_metrics_ready = bool(
            self.database.calendar_status(compact) and now.time() >= time(17, 0)
        )
        reference_date = (
            compact
            if current_metrics_ready
            else previous_date or (recent_dates[0] if recent_dates else None)
        )
        if (
            reference_date
            and self.database.get_metadata("daily_metrics_synced_date") != reference_date
        ):
            self.sync_daily_metrics(reference_date)
        if (
            recent_dates
            and self.database.get_metadata("kpl_history_bootstrapped") != recent_dates[0]
        ):
            for trade_date in reversed(recent_dates[:5]):
                if not self.database.has_kpl_events_for_date(trade_date):
                    self.sync_kpl_events(trade_date)
            self.database.set_metadata("kpl_history_bootstrapped", recent_dates[0])

    def sync_calendar(self, now: datetime | None = None) -> int:
        now = now or self.clock.china_now()
        run_id = self.database.begin_run("sync_calendar")
        try:
            rows = self.provider.trade_calendar(f"{now.year}0101", f"{now.year}1231")
            if len(rows) < 200:
                raise RuntimeError(f"Trading calendar coverage abnormal: {len(rows)} rows")
            self.database.replace_calendar(rows)
            self.database.finish_run(run_id, "success", len(rows))
            return len(rows)
        except Exception as error:
            self.database.finish_run(run_id, "failed", detail=str(error))
            self._data_pull_failure("sync_calendar", error)
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
            self._data_pull_failure("sync_universe", error)
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
            self._data_pull_failure("sync_daily_limits", error)
            return 0

    def sync_daily_metrics(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_daily_metrics")
        try:
            rows = self.provider.daily_basic(trade_date)
            count = self.database.upsert_daily_metrics(rows)
            if count < 2000:
                raise RuntimeError(f"Daily metric coverage abnormal: {count}")
            self.database.set_metadata("daily_metrics_synced_date", trade_date)
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("Daily metric sync degraded: %s", error)
            self._data_pull_failure("sync_daily_metrics", error)
            return 0

    def sync_daily_prices(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_daily_prices")
        try:
            rows = self.provider.daily_prices(trade_date)
            count = self.database.upsert_daily_bars(rows)
            if count < 2000:
                raise RuntimeError(f"Daily price coverage abnormal: {count}")
            self.database.set_metadata(f"daily_prices_synced:{trade_date}", "true")
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("Daily price sync degraded for %s: %s", trade_date, error)
            self._data_pull_failure("sync_daily_prices", error)
            return 0

    def refresh_test_pool_prices(self, now: datetime | None = None) -> dict[str, Any]:
        now = self.clock.normalize(now or self.clock.china_now())
        compact = now.strftime("%Y%m%d")
        calendar_open = self.database.calendar_status(compact)
        if calendar_open and now.time() < time(17, 0):
            ready_through = self.database.previous_trade_date(compact)
        else:
            dates = self.database.open_trade_dates(compact, 1)
            ready_through = dates[0] if dates else None
        passes: list[dict[str, int]] = []
        synced: list[str] = []
        attempted: set[str] = set()
        for _ in range(15):
            settled = self.test_pool.settle()
            passes.append(settled)
            newly_synced = []
            if ready_through:
                for trade_date in self.database.pending_test_pool_price_dates(ready_through):
                    if trade_date in attempted:
                        continue
                    if self.database.get_metadata(f"daily_prices_synced:{trade_date}") == "true":
                        continue
                    attempted.add(trade_date)
                    if self.sync_daily_prices(trade_date) >= 2000:
                        synced.append(trade_date)
                        newly_synced.append(trade_date)
            if not newly_synced and not any(settled.values()):
                break
        return {
            "ready_through": ready_through,
            "synced_dates": synced,
            "passes": passes,
        }

    def add_test_pool_stock(
        self, theme_id: int, code: str, now: datetime | None = None
    ) -> tuple[dict[str, Any], bool]:
        current = self.clock.normalize(now or self.clock.china_now())
        return self.test_pool.add(theme_id, code, current)

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
            self._data_pull_failure("sync_kpl_events", error)
            return -1

    def sync_kpl_concepts(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_kpl_concepts")
        try:
            rows = self.provider.kpl_concept_members(trade_date)
            count = self.database.upsert_kpl_concept_members(rows)
            if rows and count == 0:
                raise RuntimeError(
                    "KPL concept rows returned but none mapped to active stocks; "
                    "check kpl_concept_cons field mapping and universe"
                )
            self.database.set_metadata("kpl_concepts_synced_date", trade_date)
            self.database.set_metadata(
                "kpl_concepts_may_be_truncated", "true" if len(rows) >= 3000 else "false"
            )
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("KPL concept sync degraded: %s", error)
            self._data_pull_failure("sync_kpl_concepts", error)
            return 0

    def discover_trade_date(
        self, trade_date: str, now: datetime | None = None
    ) -> list[int]:
        """Discover threshold candidates using a cached event-level semantic clustering pass."""
        observed_at = self.clock.normalize(now or self.clock.china_now())
        with self._discovery_lock:
            events = self.database.limit_touch_events(trade_date)
            if len(events) < self.settings.minimum_limit_touches:
                return []
            signature_payload = [
                {
                    "code": event.get("code"),
                    "board_tag": event.get("board_tag"),
                    "status": event.get("status"),
                    "themes": event.get("themes", []),
                    "limit_reason": event.get("limit_reason"),
                    "concept_tags": [
                        item.get("tag") for item in event.get("concept_tags", [])
                    ],
                }
                for event in events
            ]
            input_signature = hashlib.sha256(
                json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            cached = self.database.semantic_cluster_run(trade_date, input_signature)
            semantic_clusters: list[dict[str, Any]] | None = None
            if cached and cached.get("status") == "success":
                semantic_clusters = list(cached.get("clusters") or [])
            recent_failed_run = bool(
                cached
                and cached.get("status") == "failed"
                and _within_retry_cooldown(cached.get("created_at"), observed_at, minutes=30)
            )
            if semantic_clusters is None and self.explainer.enabled and not recent_failed_run:
                try:
                    semantic_clusters = self.explainer.cluster_limit_events(
                        trade_date,
                        events,
                        self.settings.minimum_limit_touches,
                    )
                    self.database.save_semantic_cluster_run(
                        trade_date,
                        input_signature,
                        self.settings.openai_model,
                        "success",
                        semantic_clusters,
                    )
                except Exception as error:
                    safe = _safe_error(error)
                    self.database.save_semantic_cluster_run(
                        trade_date,
                        input_signature,
                        self.settings.openai_model,
                        "failed",
                        [],
                        safe,
                    )
                    self._data_pull_failure("semantic_event_clustering", error, observed_at)
                    logger.warning("Semantic event clustering failed: %s", safe)

            if semantic_clusters is None:
                # Resilient fallback when AI is disabled or unavailable. It is marked
                # explicitly so the audit page never presents exact-tag grouping as semantic.
                return self.discovery.discover_for_date(trade_date, observed_at)

            event_by_code = {str(event["code"]): event for event in events}
            enriched = []
            for cluster in semantic_clusters:
                members = [
                    event_by_code[code]
                    for code in cluster.get("member_codes", [])
                    if code in event_by_code
                ]
                if len(members) < self.settings.minimum_limit_touches:
                    continue
                enriched.append(
                    {
                        **cluster,
                        "members": members,
                        "touch_count": len(members),
                        "sealed_count": sum(
                            member.get("board_tag") == "涨停" for member in members
                        ),
                        "failed_count": sum(
                            member.get("board_tag") == "炸板" for member in members
                        ),
                    }
                )
            return self.discovery.discover_for_date(
                trade_date,
                observed_at,
                semantic_clusters=enriched,
            )

    def backfill_recent_trade_days(
        self,
        now: datetime | None = None,
        *,
        refresh_sources: bool = True,
        source: str = "startup",
    ) -> dict[str, Any]:
        """Replay discovery for the latest two open days without touching realtime rt_k."""
        observed_at = self.clock.normalize(now or self.clock.china_now())
        compact = observed_at.strftime("%Y%m%d")
        trade_dates = self.database.open_trade_dates(compact, 2)
        run_id = self.database.begin_run("discovery_backfill")
        candidate_ids: list[int] = []
        failures: list[str] = []
        for trade_date in reversed(trade_dates):
            try:
                if refresh_sources:
                    if self.sync_kpl_events(trade_date) < 0:
                        raise RuntimeError(f"KPL events unavailable for {trade_date}")
                    self.sync_kpl_concepts(trade_date)
                candidate_ids.extend(self.discover_trade_date(trade_date, observed_at))
            except Exception as error:
                failures.append(f"{trade_date}:{_safe_error(error)}")
                self._data_pull_failure("discovery_backfill", error, observed_at)
        candidate_ids = list(dict.fromkeys(candidate_ids))
        if candidate_ids and self.explainer.enabled:
            self._assess_and_admit_candidates(candidate_ids)
        status = "success" if not failures else "degraded"
        detail = (
            f"source={source},dates={','.join(trade_dates)},candidates={len(candidate_ids)}"
            + (f",failures={' | '.join(failures)}" if failures else "")
        )
        self.database.finish_run(run_id, status, len(candidate_ids), detail)
        self.database.set_metadata(
            "last_discovery_backfill",
            f"{observed_at.isoformat(timespec='seconds')}|{detail}",
        )
        return {
            "status": status,
            "trade_dates": trade_dates,
            "candidate_ids": candidate_ids,
            "failures": failures,
        }

    def _discover_and_assess_date(self, trade_date: str, now: datetime) -> None:
        try:
            candidate_ids = self.discover_trade_date(trade_date, now)
            if candidate_ids and self.explainer.enabled:
                self._assess_and_admit_candidates(candidate_ids)
        finally:
            self._discovery_inflight.discard(trade_date)

    def _startup_backfill(self, now: datetime) -> None:
        try:
            self.backfill_recent_trade_days(now, refresh_sources=True, source="startup")
        finally:
            self._discovery_inflight.discard("startup_backfill")

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
                main_quotes = [quote for quote in raw_quotes if quote.code in universe]
                if len(main_quotes) < 2000:
                    raise RuntimeError(
                        f"Quote coverage abnormal: raw={len(raw_quotes)}, main={len(main_quotes)}"
                    )
                invalid_quotes = [
                    quote for quote in main_quotes if quote.pre_close <= 0 or quote.close <= 0
                ]
                quotes = [quote for quote in main_quotes if quote.pre_close > 0 and quote.close > 0]
                if state.session in {"morning", "afternoon"} and len(quotes) < 2000:
                    raise RuntimeError(
                        "Valid quote coverage abnormal: "
                        f"valid={len(quotes)}, invalid={len(invalid_quotes)}"
                    )
                if not quotes:
                    # No indicative prices at 09:15 can be a normal auction state.
                    # Close the slot without creating fake -100% records or a fault alert.
                    self.database.set_metadata("last_quote_slot", slot_key)
                    self.database.finish_run(
                        run_id,
                        "degraded",
                        0,
                        f"auction_prices_unavailable={len(invalid_quotes)}",
                    )
                    return {
                        "status": "degraded",
                        "slot": state.slot,
                        "quotes": 0,
                        "invalid_quotes": len(invalid_quotes),
                        "candidate_ids": [],
                        "scored_ids": [],
                    }
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

                candidate_ids: list[int] = []
                discovery_trade_date: str | None = None
                if started.minute % self.settings.cluster_interval_minutes == 0:
                    kpl_count = self.sync_kpl_events(compact)
                    if kpl_count >= 0:
                        discovery_trade_date = compact
                scored_ids = self.scorer.score_confirmed(started)
                self._evaluate_theme_alerts(started, scored_ids, stale=False)
                self.database.finish_run(
                    run_id,
                    "success",
                    len(quotes),
                    f"internal_events={len(anomalies)}, invalid_quotes={len(invalid_quotes)}, "
                    f"candidates={len(candidate_ids)}",
                )
                return {
                    "status": "success",
                    "slot": state.slot,
                    "quotes": len(quotes),
                    "internal_events": len(anomalies),
                    "invalid_quotes": len(invalid_quotes),
                    "candidate_ids": candidate_ids,
                    "discovery_trade_date": discovery_trade_date,
                    "scored_ids": scored_ids,
                }
            except Exception as error:
                self.database.finish_run(run_id, "failed", detail=str(error))
                logger.exception("Quote collection failed")
                self._data_pull_failure("collect_quotes", error, started)
                return {"status": "failed", "slot": state.slot, "error": str(error)}

    def _assess_and_admit_candidates(self, theme_ids: list[int]) -> None:
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
        try:
            for theme_id in selected:
                theme = themes.get(theme_id)
                if not theme or theme.get("status") not in {"pending", "watching"}:
                    continue
                try:
                    self.database.set_admission_status(
                        theme_id, "analyzing", "正在进行60交易日新颖性与持续性审查"
                    )
                    compact = str(theme["day1_date"]).replace("-", "")
                    dates = self.database.open_trade_dates(
                        compact, self.settings.novelty_lookback_trade_days + 1
                    )
                    cutoff = (
                        f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:8]}T00:00:00"
                        if dates
                        else (self.clock.china_now() - timedelta(days=120)).isoformat()
                    )
                    trigger_codes = [
                        str(member["code"])
                        for member in theme.get("members", [])
                        if member.get("active", 1)
                    ]
                    history = self.database.historical_theme_matches(
                        theme_id=theme_id,
                        shared_tag=str(theme["shared_tag"]),
                        member_codes=trigger_codes,
                        since_date=cutoff,
                    )
                    aliases = list(theme.get("cluster_aliases") or [])
                    aliases.append(str(theme["shared_tag"]))
                    stock_pool = self.database.eligible_members_for_tags(aliases)
                    item = self.explainer.assess_for_admission(theme, history, stock_pool)
                    pool_by_code = {str(member["code"]): member for member in stock_pool}
                    validated_members = []
                    for proposal in item.get("proposed_members", []):
                        code = str(proposal.get("code") or "")
                        if code in trigger_codes or code not in pool_by_code:
                            continue
                        deterministic = pool_by_code[code]
                        validated_members.append(
                            {
                                "code": code,
                                "name": deterministic["name"],
                                "role": proposal.get("role"),
                                "evidence": {
                                    "shared_tag": theme["shared_tag"],
                                    "matched_tags": deterministic.get("matched_tags", []),
                                    "tag_source": deterministic.get("source"),
                                    "tag_confidence": deterministic.get("confidence"),
                                    "ai_reason": proposal.get("reason"),
                                    "validation": "high_confidence_theme_tag_whitelist",
                                },
                            }
                        )
                    valid_codes = set(trigger_codes) | {
                        str(member["code"]) for member in validated_members
                    }
                    source_count = len(
                        {
                            str(source.get("url"))
                            for source in item.get("sources", [])
                            if source.get("url")
                        }
                    )
                    duration_gate = (
                        int(item.get("expected_duration_days") or 0)
                        >= self.settings.minimum_expected_duration_days
                    )
                    upside_gate = (
                        float(item.get("leader_upside_scenario_pct") or 0)
                        >= self.settings.leader_upside_threshold_pct
                    )
                    core_checks = {
                        "new_theme": bool(item.get("is_new_theme")),
                        "novelty_confidence": float(item.get("novelty_confidence") or 0)
                        >= self.settings.novelty_confidence_threshold,
                        "catalyst_confidence": float(item.get("catalyst_confidence") or 0)
                        >= self.settings.catalyst_confidence_threshold,
                        "source_available": source_count >= 1,
                        "duration_or_upside": duration_gate or upside_gate,
                        "valid_leader": str(item.get("leader_candidate_code") or "") in valid_codes,
                    }
                    evidence_grade = _admission_evidence_grade(item.get("catalysts", []))
                    core_passed = all(core_checks.values())
                    formal_evidence = evidence_grade in {"official", "multi_source"}
                    previous_watching = theme.get("status") == "watching"
                    if core_passed and formal_evidence:
                        decision_level = "formal"
                        evidence_reason = (
                            "官方/公司披露证据"
                            if evidence_grade == "official"
                            else "多源产业证据交叉验证"
                        )
                        decision_reason = (
                            "正式题材：市场共识、持续性与新颖性通过，且已有"
                            + evidence_reason
                        )
                    elif core_passed:
                        decision_level = "early_watch"
                        decision_reason = (
                            "早期观察：市场共识、新颖性和持续性通过；当前仅有供应链、"
                            "研报或单一来源信息，等待官方确认或多源交叉验证"
                        )
                    elif previous_watching:
                        decision_level = "early_watch"
                        failed = _failed_check_labels(core_checks)
                        decision_reason = (
                            "继续早期观察：本轮复核仍未满足正式升级条件；"
                            + "、".join(failed)
                        )
                    else:
                        decision_level = "rejected"
                        failed = _failed_check_labels(core_checks)
                        decision_reason = "未纳入：" + "、".join(failed)
                    admitted = decision_level == "formal"
                    self.database.save_admission_review(
                        theme_id,
                        item,
                        history,
                        validated_members,
                        admitted,
                        decision_level,
                        decision_reason,
                    )
                    explanation = {
                        "model": item["model"],
                        "suggested_name": item["suggested_name"],
                        "explanation": (
                            f"新颖性：{item['novelty_reason']}\n"
                            f"持续性：{item['duration_reason']}\n"
                            f"空间情景：{item['upside_scenario_reason']}"
                        ),
                        "catalyst_summary": item["catalyst_summary"],
                        "catalyst_duration": f"预估{item['expected_duration_days']}个交易日",
                        "merge_suggestions": [],
                        "sources": item.get("sources", []),
                        "raw": item.get("raw", {}),
                    }
                    self.database.save_ai_explanation(theme_id, explanation)
                    self.database.save_theme_catalysts(theme_id, item.get("catalysts", []))
                    self.database.set_suggested_name(theme_id, item["suggested_name"])
                    if decision_level in {"formal", "early_watch"}:
                        self.database.add_validated_members(
                            theme_id,
                            validated_members,
                            self.clock.china_now().isoformat(timespec="seconds"),
                        )
                        if decision_level == "formal":
                            self.discovery.confirm(
                                theme_id,
                                item["suggested_name"],
                                float(item.get("catalyst_confidence") or 0),
                                f"预估{item['expected_duration_days']}个交易日",
                            )
                            self.database.set_admission_status(
                                theme_id, "admitted", decision_reason, evidence_grade
                            )
                            confirmed = self.database.get_theme(theme_id) or {}
                            score = self.scorer.calculate(confirmed, self.clock.china_now())
                            if score:
                                self.database.save_score(theme_id, score)
                            self._send_new_theme_alert(theme_id, item, confirmed, "formal")
                        else:
                            self.database.set_theme_status(
                                theme_id,
                                "watching",
                                item["suggested_name"],
                                float(item.get("catalyst_confidence") or 0),
                                f"预估{item['expected_duration_days']}个交易日",
                            )
                            self.database.set_admission_status(
                                theme_id, "early_watch", decision_reason, evidence_grade
                            )
                            watching = self.database.get_theme(theme_id) or {}
                            self._send_new_theme_alert(theme_id, item, watching, "early_watch")
                    else:
                        self.discovery.reject(theme_id)
                        self.database.set_admission_status(
                            theme_id, "not_admitted", decision_reason, evidence_grade
                        )
                except Exception as error:
                    self.database.set_admission_status(
                        theme_id, "analysis_failed", _safe_error(error)
                    )
                    self._data_pull_failure("ai_admission_analysis", error)
                    logger.warning("AI admission analysis failed for theme %s: %s", theme_id, error)
        finally:
            with self._ai_lock:
                self._ai_inflight.difference_update(selected)

    def refresh_theme_catalysts(self, limit: int = 8) -> dict[str, Any]:
        if not self.explainer.enabled:
            return {"status": "disabled", "updated": 0, "new_catalysts": 0}
        themes = [
            item
            for item in self.database.list_themes()
            if item.get("status") in {"confirmed", "watching", "pending"}
        ]
        themes.sort(
            key=lambda item: (
                item.get("status") in {"watching", "confirmed"},
                item.get("status") == "watching",
                sum(bool(member.get("signal_active")) for member in item.get("members", [])),
                float((item.get("score") or {}).get("heat") or 0),
                float((item.get("market_summary") or {}).get("current_average_pct") or 0),
            ),
            reverse=True,
        )
        themes = themes[:limit]
        names = [
            str(item.get("final_name") or item.get("suggested_name") or item["provisional_name"])
            for item in themes
        ]
        with self._ai_lock:
            selected = [theme for theme in themes if int(theme["id"]) not in self._ai_inflight]
            self._ai_inflight.update(int(theme["id"]) for theme in selected)
        started_at = self.clock.china_now().isoformat()
        self.database.set_metadata("last_catalyst_refresh_started_at", started_at)

        def update_one(theme: dict[str, Any]) -> tuple[int, int]:
            theme_id = int(theme["id"])
            existing = [
                {
                    "title": item.get("title"),
                    "url": item.get("source_url"),
                    "published_at": item.get("published_at"),
                }
                for item in theme.get("catalysts", [])[:12]
            ]
            item = self.explainer.explain(theme, names, existing)
            self.database.save_ai_explanation(theme_id, item)
            inserted_count = self.database.save_theme_catalysts(theme_id, item.get("catalysts", []))
            self.database.set_suggested_name(theme_id, item["suggested_name"])
            return theme_id, inserted_count

        updated = 0
        inserted = 0
        failures = 0
        try:
            with ThreadPoolExecutor(max_workers=min(3, max(1, len(selected)))) as executor:
                futures = {
                    executor.submit(update_one, theme): int(theme["id"]) for theme in selected
                }
                for future in as_completed(futures):
                    theme_id = futures[future]
                    try:
                        _, inserted_count = future.result()
                        inserted += inserted_count
                        updated += 1
                    except Exception as error:
                        failures += 1
                        self._data_pull_failure("refresh_theme_catalysts", error)
                        logger.warning("Catalyst refresh failed for theme %s: %s", theme_id, error)
        finally:
            with self._ai_lock:
                self._ai_inflight.difference_update(int(theme["id"]) for theme in selected)
        watching_ids = [
            int(theme["id"]) for theme in selected if theme.get("status") == "watching"
        ]
        if watching_ids:
            self._assess_and_admit_candidates(watching_ids)
        completed_at = self.clock.china_now().isoformat()
        self.database.set_metadata("last_catalyst_refresh_at", completed_at)
        self.database.set_metadata(
            "last_catalyst_refresh_result",
            f"updated={updated},new={inserted},failed={failures}",
        )
        return {
            "status": "success" if updated else "degraded",
            "updated": updated,
            "new_catalysts": inserted,
            "failed": failures,
        }

    def _catalyst_refresh_slot(self, now: datetime) -> str | None:
        slots = sorted(
            value.strip()
            for value in self.settings.catalyst_refresh_hours.split(",")
            if value.strip()
        )
        current = now.strftime("%H:%M")
        due = [slot for slot in slots if slot <= current]
        return due[-1] if due else None

    def _send_new_theme_alert(
        self,
        theme_id: int,
        review: dict[str, Any],
        theme: dict[str, Any],
        level: str = "formal",
    ) -> None:
        name = str(
            theme.get("final_name")
            or theme.get("suggested_name")
            or theme.get("provisional_name")
            or "新重点题材"
        )
        members = [member for member in theme.get("members", []) if member.get("active", 1)]
        members.sort(
            key=lambda member: (
                int(member.get("board_height") or 0),
                float(member.get("current_pct") or 0),
            ),
            reverse=True,
        )
        stock_text = "、".join(f"{member['name']}({member['code']})" for member in members[:8])
        leader_code = str(review.get("leader_candidate_code") or "")
        leader = next(
            (member for member in members if str(member.get("code")) == leader_code),
            {},
        )
        leader_text = f"{leader.get('name', '')}({leader_code})" if leader_code else "未确认"
        body = (
            f"级别：{'正式题材' if level == 'formal' else '早期观察（证据待确认）'}\n"
            f"触发：共同事件至少{self.settings.minimum_limit_touches}只股票当日曾触及涨停\n"
            f"核心股票：{stock_text or '等待行情补全'}\n"
            f"催化：{review.get('catalyst_summary') or '未提供'}\n"
            f"预估持续：{int(review.get('expected_duration_days') or 0)}个交易日\n"
            f"龙头候选：{leader_text}，情景空间"
            f"{float(review.get('leader_upside_scenario_pct') or 0):.1f}%\n"
            f"查看：{self.settings.public_base_url}"
        )
        self._send_alert(
            dedupe_key=f"theme_level:{level}:{theme_id}",
            category="formal_theme" if level == "formal" else "early_watch_theme",
            severity="critical" if level == "formal" else "high",
            title=(f"正式题材：{name}" if level == "formal" else f"早期观察：{name}"),
            body=body,
            theme_id=theme_id,
        )

    def _data_pull_failure(
        self, job_name: str, error: Exception, now: datetime | None = None
    ) -> None:
        current = self.clock.normalize(now or self.clock.china_now())
        self._send_alert(
            dedupe_key=f"data_failure:{job_name}:{current.strftime('%Y%m%d%H')}",
            category="data_pull_failed",
            severity="critical",
            title=f"数据任务失败：{job_name}",
            body=f"{_safe_error(error)}\n时间：{current.isoformat(timespec='seconds')}",
        )

    def _evaluate_theme_alerts(self, now: datetime, theme_ids: list[int], stale: bool) -> None:
        themes = {int(item["id"]): item for item in self.database.list_themes(status="confirmed")}
        for theme_id in theme_ids:
            theme = themes.get(theme_id)
            if not theme or not theme.get("score"):
                continue
            score = theme["score"]
            name = (
                theme.get("final_name") or theme.get("suggested_name") or theme["provisional_name"]
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
            self.database.set_metadata("last_wecom_error", "")
            self.database.set_metadata(
                "last_wecom_success_at", self.clock.china_now().isoformat(timespec="seconds")
            )
        except Exception as error:
            safe = _safe_error(error)
            self.database.mark_alert_pushed(alert_id, safe)
            self.database.set_metadata("last_wecom_error", safe)
            logger.warning("WeCom push failed: %s", safe)

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
            self.sync_daily_metrics(compact)
            backfill = self.backfill_recent_trade_days(
                now, refresh_sources=False, source="end_of_day"
            )
            cohort_count = self._record_daily_cohorts(now)
            backup = self.database.backup()
            self.database.prune_backups()
            archived = self.database.archive_quotes_before(now.date() - timedelta(days=120))
            self.database.set_metadata(run_key, now.isoformat())
            self.database.finish_run(
                run_id,
                "success",
                len(archived),
                f"backup={backup.name}, cohorts={cohort_count}, "
                f"backfill_candidates={len(backfill['candidate_ids'])}",
            )
            return {
                "status": "success",
                "backup": backup.name,
                "archived": len(archived),
                "cohorts": cohort_count,
                "backfill": backfill,
            }
        except Exception as error:
            self.database.finish_run(run_id, "failed", detail=str(error))
            self._data_pull_failure("end_of_day", error, now)
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
            candidate_ids: list[int] = []
            if self._startup_backfill_pending and calendar is not None:
                self._startup_backfill_pending = False
                self._discovery_inflight.add("startup_backfill")
                asyncio.create_task(
                    asyncio.to_thread(self._startup_backfill, now),
                    name="startup-discovery-backfill",
                )
            if state.in_realtime_window and state.slot and now.second < 25:
                result = await asyncio.to_thread(self.collect_once, now)
                candidate_ids.extend(result.get("candidate_ids", []))
                discovery_date = result.get("discovery_trade_date")
                if discovery_date and discovery_date not in self._discovery_inflight:
                    self._discovery_inflight.add(discovery_date)
                    asyncio.create_task(
                        asyncio.to_thread(self._discover_and_assess_date, discovery_date, now),
                        name=f"semantic-discovery-{discovery_date}",
                    )
            elif calendar and time(15, 5) <= now.time() <= time(15, 20):
                await asyncio.to_thread(self.end_of_day, now)
            if now.second < 25:
                candidate_ids.extend(
                    int(theme["id"])
                    for theme in self.database.list_themes(status="pending")
                    if _admission_candidate_due(theme, now)
                )
                candidate_ids = list(dict.fromkeys(candidate_ids))
                if candidate_ids and self.explainer.enabled:
                    asyncio.create_task(
                        asyncio.to_thread(self._assess_and_admit_candidates, candidate_ids),
                        name=f"theme-admission-{now.strftime('%H%M')}",
                    )
            catalyst_slot = self._catalyst_refresh_slot(now) if calendar else None
            if catalyst_slot and now.second < 25:
                refresh_key = f"catalyst_refresh:{now.date().isoformat()}:{catalyst_slot}"
                if not self.database.get_metadata(refresh_key):
                    self.database.set_metadata(refresh_key, now.isoformat())
                    asyncio.create_task(
                        asyncio.to_thread(self.refresh_theme_catalysts),
                        name=f"catalyst-refresh-{catalyst_slot.replace(':', '')}",
                    )
            settlement_slot = now.strftime("%H:%M")
            if settlement_slot in {"08:35", "17:10"} and now.second < 25:
                refresh_key = f"test_pool_refresh:{now.date().isoformat()}:{settlement_slot}"
                if not self.database.get_metadata(refresh_key):
                    self.database.set_metadata(refresh_key, now.isoformat())
                    asyncio.create_task(
                        asyncio.to_thread(self.refresh_test_pool_prices, now),
                        name=f"test-pool-refresh-{settlement_slot.replace(':', '')}",
                    )
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
            "version": __version__,
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
            "latest_catalyst_refresh_at": self.database.get_metadata("last_catalyst_refresh_at"),
            "test_pool": {
                "total_count": self.database.test_pool_summary()["total_count"],
                "latest_price_sync": self.database.latest_run("sync_daily_prices"),
            },
            "latest_catalyst_refresh_started_at": self.database.get_metadata(
                "last_catalyst_refresh_started_at"
            ),
            "latest_catalyst_refresh_result": self.database.get_metadata(
                "last_catalyst_refresh_result"
            ),
            "daily_metrics_trade_date": self.database.get_metadata("daily_metrics_synced_date"),
            "latest_wecom_error": self.database.get_metadata("last_wecom_error"),
            "latest_discovery_backfill": self.database.get_metadata(
                "last_discovery_backfill"
            ),
            "admission_policy": {
                "minimum_limit_touches": self.settings.minimum_limit_touches,
                "failed_boards_count": True,
                "semantic_event_clustering": True,
                "backfill_trade_days": 2,
                "levels": ["early_watch", "formal"],
                "analysis_failure_retry_minutes": 30,
                "novelty_lookback_trade_days": self.settings.novelty_lookback_trade_days,
                "minimum_expected_duration_days": (self.settings.minimum_expected_duration_days),
                "leader_upside_threshold_pct": self.settings.leader_upside_threshold_pct,
            },
            "warnings": self.settings.validate_integrations(),
        }


def _parse_board_height(status: Any) -> int:
    value = str(status or "")
    if "首板" in value:
        return 1
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _safe_error(error: Exception) -> str:
    message = str(error)
    message = re.sub(r"(?i)(access_token=)[^&\s]+", r"\1***", message)
    message = re.sub(r"(?i)(corpsecret=)[^&\s]+", r"\1***", message)
    message = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1***", message)
    return (message or type(error).__name__)[:500]


def _within_retry_cooldown(value: Any, now: datetime, *, minutes: int) -> bool:
    try:
        created_at = datetime.fromisoformat(str(value))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=now.tzinfo)
        return now - created_at.astimezone(now.tzinfo) < timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return False


def _admission_candidate_due(
    theme: dict[str, Any], now: datetime, *, retry_minutes: int = 30
) -> bool:
    status = str(theme.get("admission_status") or "")
    if status == "awaiting_ai":
        return True
    if status != "analysis_failed":
        return False
    reviewed_at = theme.get("admission_reviewed_at")
    if not reviewed_at:
        return True
    return not _within_retry_cooldown(reviewed_at, now, minutes=retry_minutes)


def _admission_evidence_grade(catalysts: list[dict[str, Any]]) -> str:
    evidenced = [item for item in catalysts if item.get("url")]
    if any(
        str(item.get("evidence_level") or "") == "官方确认"
        or str(item.get("source_kind") or "")
        in {"official", "company_disclosure", "regulator"}
        for item in evidenced
    ):
        return "official"
    cross_sources = [
        item
        for item in evidenced
        if str(item.get("evidence_level") or "") == "多源交叉验证"
        or str(item.get("source_kind") or "")
        in {
            "industry_primary",
            "authoritative_media",
            "brokerage_research",
            "supply_chain_report",
        }
    ]
    domains = {
        urlsplit(str(item.get("url") or "")).netloc.lower().removeprefix("www.")
        for item in cross_sources
        if urlsplit(str(item.get("url") or "")).netloc
    }
    if len(domains) >= 2:
        return "multi_source"
    if evidenced:
        return "supply_chain_unconfirmed"
    return "weak"


def _failed_check_labels(checks: dict[str, bool]) -> list[str]:
    labels = {
        "new_theme": "不属于首次广泛发酵",
        "novelty_confidence": "新颖性置信度不足",
        "catalyst_confidence": "催化可信度不足",
        "source_available": "没有可核验新闻来源",
        "duration_or_upside": "未满足持续3日或龙头30%情景空间",
        "valid_leader": "龙头候选缺少确定性股票证据",
    }
    return [labels.get(key, key) for key, passed in checks.items() if not passed]
