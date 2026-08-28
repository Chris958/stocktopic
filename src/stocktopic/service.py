from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        self.discovery = ThemeDiscovery(
            self.database,
            settings.minimum_limit_touches,
            settings.maximum_candidates_per_run,
        )
        self.scorer = ThemeScorer(self.database)
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
        self._ai_lock = threading.Lock()
        self._ai_inflight: set[int] = set()
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
                if started.minute % self.settings.cluster_interval_minutes == 0:
                    kpl_count = self.sync_kpl_events(compact)
                    if kpl_count >= 0:
                        candidate_ids = self.discovery.discover(started)
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
                if not theme or theme.get("status") != "pending":
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
                    stock_pool = self.database.eligible_members_for_tag(str(theme["shared_tag"]))
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
                    evidenced_catalysts = [
                        catalyst
                        for catalyst in item.get("catalysts", [])
                        if catalyst.get("url")
                        and str(catalyst.get("evidence_level") or "") == "明确证据"
                    ]
                    duration_gate = (
                        int(item.get("expected_duration_days") or 0)
                        >= self.settings.minimum_expected_duration_days
                    )
                    upside_gate = (
                        float(item.get("leader_upside_scenario_pct") or 0)
                        >= self.settings.leader_upside_threshold_pct
                    )
                    checks = {
                        "new_theme": bool(item.get("is_new_theme")),
                        "novelty_confidence": float(item.get("novelty_confidence") or 0)
                        >= self.settings.novelty_confidence_threshold,
                        "catalyst_confidence": float(item.get("catalyst_confidence") or 0)
                        >= self.settings.catalyst_confidence_threshold,
                        "reliable_source": source_count >= 1 and bool(evidenced_catalysts),
                        "duration_or_upside": duration_gate or upside_gate,
                        "valid_leader": str(item.get("leader_candidate_code") or "") in valid_codes,
                    }
                    admitted = all(checks.values())
                    failed_checks = [key for key, passed in checks.items() if not passed]
                    decision_reason = (
                        "通过：新颖性、可靠催化及持续性/龙头空间门槛均满足"
                        if admitted
                        else "未通过：" + "、".join(failed_checks)
                    )
                    self.database.save_admission_review(
                        theme_id,
                        item,
                        history,
                        validated_members,
                        admitted,
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
                    if admitted:
                        self.database.add_validated_members(
                            theme_id,
                            validated_members,
                            self.clock.china_now().isoformat(timespec="seconds"),
                        )
                        self.discovery.confirm(
                            theme_id,
                            item["suggested_name"],
                            float(item.get("catalyst_confidence") or 0),
                            f"预估{item['expected_duration_days']}个交易日",
                        )
                        self.database.set_admission_status(theme_id, "admitted", decision_reason)
                        confirmed = self.database.get_theme(theme_id) or {}
                        score = self.scorer.calculate(confirmed, self.clock.china_now())
                        if score:
                            self.database.save_score(theme_id, score)
                        self._send_new_theme_alert(theme_id, item, confirmed)
                    else:
                        self.discovery.reject(theme_id)
                        self.database.set_admission_status(
                            theme_id, "not_admitted", decision_reason
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
            if item.get("status") in {"confirmed", "pending"}
        ]
        themes.sort(
            key=lambda item: (
                item.get("status") == "confirmed",
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
        self, theme_id: int, review: dict[str, Any], theme: dict[str, Any]
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
            f"触发：同题材至少{self.settings.minimum_limit_touches}只股票当日曾触及涨停\n"
            f"核心股票：{stock_text or '等待行情补全'}\n"
            f"催化：{review.get('catalyst_summary') or '未提供'}\n"
            f"预估持续：{int(review.get('expected_duration_days') or 0)}个交易日\n"
            f"龙头候选：{leader_text}，情景空间"
            f"{float(review.get('leader_upside_scenario_pct') or 0):.1f}%\n"
            f"查看：{self.settings.public_base_url}"
        )
        self._send_alert(
            dedupe_key=f"new_key_theme:{theme_id}",
            category="new_key_theme",
            severity="critical",
            title=f"新重点题材：{name}",
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
            if state.in_realtime_window and state.slot and now.second < 25:
                result = await asyncio.to_thread(self.collect_once, now)
                candidate_ids.extend(result.get("candidate_ids", []))
            elif calendar and time(15, 5) <= now.time() <= time(15, 20):
                await asyncio.to_thread(self.end_of_day, now)
            if now.second < 25:
                candidate_ids.extend(
                    int(theme["id"])
                    for theme in self.database.list_themes(status="pending")
                    if theme.get("admission_status") == "awaiting_ai"
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
            "latest_catalyst_refresh_at": self.database.get_metadata("last_catalyst_refresh_at"),
            "latest_catalyst_refresh_started_at": self.database.get_metadata(
                "last_catalyst_refresh_started_at"
            ),
            "latest_catalyst_refresh_result": self.database.get_metadata(
                "last_catalyst_refresh_result"
            ),
            "daily_metrics_trade_date": self.database.get_metadata("daily_metrics_synced_date"),
            "latest_wecom_error": self.database.get_metadata("last_wecom_error"),
            "admission_policy": {
                "minimum_limit_touches": self.settings.minimum_limit_touches,
                "failed_boards_count": True,
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
