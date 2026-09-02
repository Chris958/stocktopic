from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .ai import OpenAIThemeExplainer
from .anomaly import AnomalyDetector, StockContext
from .backtest import TestPoolManager
from .config import Settings
from .db import Database
from .level2 import Level2Analyzer, Level2Client, Level2Trade
from .market_clock import ChinaMarketClock
from .providers.tushare import TushareClient
from .scoring import ThemeScorer
from .themes import ThemeDiscovery
from .wecom import WeComNotifier

logger = logging.getLogger(__name__)


class StockTopicService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.clock = ChinaMarketClock()
        self.database = Database(settings.database_path, settings.backup_dir)
        self.provider = TushareClient(settings.tushare_token)
        self.detector = AnomalyDetector()
        self.discovery = ThemeDiscovery(
            self.database, settings.minimum_limit_touches
        )
        self.scorer = ThemeScorer(self.database)
        self.explainer = OpenAIThemeExplainer(
            settings.openai_api_key,
            settings.openai_model,
            settings.openai_base_url,
            settings.openai_timeout_seconds,
            usage_callback=self.database.record_ai_usage,
            task_models={
                "catalyst_refresh": settings.openai_catalyst_model,
                "admission_analysis": settings.openai_admission_model,
                "semantic_event_clustering": settings.openai_cluster_model,
            },
        )
        self.notifier = WeComNotifier(
            settings.wecom_corp_id,
            settings.wecom_agent_id,
            settings.wecom_secret,
            settings.wecom_to_user,
        )
        self.level2 = Level2Client(
            settings.level2_base_url,
            settings.level2_api_key,
            timeout=settings.level2_timeout_seconds,
        )
        self.test_pool = TestPoolManager(
            self.database,
            take_profit_pct=settings.test_pool_take_profit_pct,
            stop_loss_pct=settings.test_pool_stop_loss_pct,
        )
        self._stop_event = asyncio.Event()
        self._last_eod_day: date | None = None
        self._ai_inflight: set[int] = set()
        self._ai_lock = threading.Lock()
        self._discovery_inflight: set[str] = set()
        self._discovery_lock = threading.Lock()
        self._startup_backfill_pending = True
        self._fund_flow_inflight: set[str] = set()
        self._fund_flow_lock = threading.Lock()

    def initialize(self) -> None:
        self.database.initialize()
        if self.database.get_metadata("database_timezone") != "Asia/Shanghai":
            self.database.set_metadata("database_timezone", "Asia/Shanghai")
        if self.database.get_metadata("scheduler_started_at") is None:
            self.database.set_metadata(
                "scheduler_started_at", self.clock.china_now().isoformat(timespec="seconds")
            )

    def status(self) -> dict[str, Any]:
        now = self.clock.china_now()
        compact = now.strftime("%Y%m%d")
        calendar = self.database.calendar_status(compact)
        state = self.clock.state(now, calendar)
        return {
            "now": now.isoformat(timespec="seconds"),
            "market": {
                "is_open_day": state.is_open_day,
                "session": state.session,
                "slot": state.slot,
                "next_action_at": state.next_action_at.isoformat(timespec="seconds"),
            },
            "provider": {
                "tushare_configured": bool(self.settings.tushare_token),
                "openai_configured": self.explainer.enabled,
                "wecom_configured": self.notifier.enabled,
                "level2_configured": self.level2.enabled,
            },
            "metadata": self.database.metadata_snapshot(),
        }

    def sync_calendar(self, now: datetime | None = None) -> int:
        observed = self.clock.normalize(now or self.clock.china_now())
        start = (observed.date() - timedelta(days=35)).strftime("%Y%m%d")
        end = (observed.date() + timedelta(days=35)).strftime("%Y%m%d")
        rows = self.provider.trade_calendar(start, end)
        return self.database.replace_calendar(rows)

    def sync_stocks(self) -> int:
        rows = self.provider.stock_basic()
        return self.database.upsert_stocks(rows)

    def sync_daily_limits(self, now: datetime | None = None) -> int:
        observed = self.clock.normalize(now or self.clock.china_now())
        compact = observed.strftime("%Y%m%d")
        rows = self.provider.stock_limits(compact)
        return self.database.upsert_daily_limits(rows)

    def sync_daily_metrics(self, trade_date: str) -> int:
        rows = self.provider.daily_basic(trade_date)
        return self.database.upsert_daily_metrics(rows)

    def sync_chinext_growth_signals(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_chinext_growth")
        try:
            rows = self.provider.daily_prices(trade_date)
            filtered = [
                item
                for item in rows
                if str(item.get("ts_code") or "").startswith("3")
                and float(item.get("pct_chg") or 0) > 10
            ]
            count = self.database.upsert_chinext_daily_growth_events(filtered)
            self.database.finish_run(run_id, "success", count)
            return count
        except Exception as error:
            self.database.finish_run(run_id, "degraded", detail=str(error))
            logger.warning("ChiNext daily growth sync degraded: %s", error)
            return -1

    def sync_kpl_events(self, trade_date: str) -> int:
        run_id = self.database.begin_run("sync_kpl_events")
        try:
            rows = []
            for tag in ("涨停", "炸板", "跌停"):
                rows.extend(self.provider.kpl_list(trade_date, tag))
            count = self.database.upsert_kpl_events(rows)
            self.database.set_metadata("kpl_synced_date", trade_date)
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
        """Discover 2-3 stock early resonance locally; use AI only at the >=4 formal gate."""
        observed_at = self.clock.normalize(now or self.clock.china_now())
        with self._discovery_lock:
            events = self.database.limit_touch_events(trade_date)
            if len(events) < 2:
                return []

            # Persist 2-3 member deterministic/graph-like clusters before any web call.
            # This gives the system an early observation layer without multiplying AI token use.
            exact_clusters = self.database.kpl_theme_clusters(trade_date)
            early_clusters = [
                cluster
                for cluster in exact_clusters
                if 2 <= int(cluster.get("touch_count") or 0) < self.settings.minimum_limit_touches
            ]
            if early_clusters:
                self.discovery.discover_for_date(
                    trade_date,
                    observed_at,
                    semantic_clusters=early_clusters,
                )

            # Four touches remain the hard gate for expensive semantic/web verification.
            if len(events) < self.settings.minimum_limit_touches:
                return []

            input_signature = _semantic_event_signature(events)
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
                        self._ai_model_for_task("semantic_event_clustering"),
                        "success",
                        semantic_clusters,
                    )
                except Exception as error:
                    safe = _safe_error(error)
                    self.database.save_semantic_cluster_run(
                        trade_date,
                        input_signature,
                        self._ai_model_for_task("semantic_event_clustering"),
                        "failed",
                        [],
                        safe,
                    )
                    self._data_pull_failure("semantic_event_clustering", error, observed_at)
                    logger.warning("Semantic event clustering failed: %s", safe)

            if semantic_clusters is None:
                # AI unavailable: deterministic clustering still promotes >=4 candidates.
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
                        "growth_count": sum(
                            member.get("board_tag") == "创业板涨幅超10%"
                            for member in members
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

    # Remaining methods are unchanged in this patch; the contents API requires full-file
    # replacement, so this sentinel is intentionally invalid and must never be committed.
