from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .domain import Anomaly, Quote


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, archive_dir: Path):
        self.path = path
        self.archive_dir = archive_dir

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.executescript(schema)
            self._migrate_schema(connection)
            connection.execute("DELETE FROM anomaly_events WHERE pct_change<=-99")
            connection.execute("DELETE FROM quote_snapshots WHERE close<=0 OR pre_close<=0")

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(candidate_themes)").fetchall()
        }
        additions = {
            "pinned": "INTEGER NOT NULL DEFAULT 0",
            "archived_at": "TEXT",
            "admission_status": "TEXT NOT NULL DEFAULT 'legacy'",
            "admission_reason": "TEXT",
            "admission_reviewed_at": "TEXT",
            "theme_level": "TEXT NOT NULL DEFAULT 'candidate'",
            "observation_at": "TEXT",
            "cluster_method": "TEXT NOT NULL DEFAULT 'exact_tag'",
            "cluster_confidence": "REAL NOT NULL DEFAULT 0",
            "cluster_aliases_json": "TEXT NOT NULL DEFAULT '[]'",
            "evidence_grade": "TEXT NOT NULL DEFAULT 'unreviewed'",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE candidate_themes ADD COLUMN {name} {definition}")
        review_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(theme_admission_reviews)").fetchall()
        }
        if "decision_level" not in review_columns:
            connection.execute(
                "ALTER TABLE theme_admission_reviews ADD COLUMN "
                "decision_level TEXT NOT NULL DEFAULT 'rejected'"
            )
        catalyst_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(theme_catalysts)").fetchall()
        }
        if "source_kind" not in catalyst_columns:
            connection.execute(
                "ALTER TABLE theme_catalysts ADD COLUMN "
                "source_kind TEXT NOT NULL DEFAULT 'unknown'"
            )
        test_pool_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(test_pool_entries)").fetchall()
        }
        test_pool_additions = {
            "buy_confirmed_at": "TEXT",
            "buy_confirmation_source": "TEXT",
            "current_price": "REAL",
            "current_return_pct": "REAL",
            "current_high": "REAL",
            "current_high_return_pct": "REAL",
            "live_updated_at": "TEXT",
        }
        for name, definition in test_pool_additions.items():
            if name not in test_pool_columns:
                connection.execute(f"ALTER TABLE test_pool_entries ADD COLUMN {name} {definition}")

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now_iso()),
            )

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def replace_calendar(self, rows: Sequence[dict[str, Any]]) -> None:
        synced_at = utc_now_iso()
        values = [
            (
                str(row["cal_date"]),
                int(str(row["is_open"]) == "1"),
                row.get("pretrade_date"),
                synced_at,
            )
            for row in rows
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO trading_calendar(cal_date, is_open, pretrade_date, synced_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cal_date) DO UPDATE SET
                    is_open=excluded.is_open,
                    pretrade_date=excluded.pretrade_date,
                    synced_at=excluded.synced_at
                """,
                values,
            )

    def calendar_status(self, cal_date: str) -> bool | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT is_open FROM trading_calendar WHERE cal_date=?", (cal_date,)
            ).fetchone()
        return bool(row["is_open"]) if row else None

    def previous_trade_date(self, cal_date: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT pretrade_date FROM trading_calendar WHERE cal_date=?", (cal_date,)
            ).fetchone()
        return str(row["pretrade_date"]) if row and row["pretrade_date"] else None

    def count_open_days(self, start: str, end: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM trading_calendar
                WHERE cal_date BETWEEN ? AND ? AND is_open=1
                """,
                (start, end),
            ).fetchone()
        return int(row["count"])

    def open_trade_dates(self, end: str, limit: int = 10) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT cal_date FROM trading_calendar
                WHERE cal_date<=? AND is_open=1
                ORDER BY cal_date DESC LIMIT ?
                """,
                (end, limit),
            ).fetchall()
        return [str(row["cal_date"]) for row in rows]

    def next_open_trade_dates(self, after: str, limit: int = 2) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT cal_date FROM trading_calendar
                WHERE cal_date>? AND is_open=1
                ORDER BY cal_date ASC LIMIT ?
                """,
                (after, limit),
            ).fetchall()
        return [str(row["cal_date"]) for row in rows]

    def upsert_stocks(self, rows: Sequence[dict[str, Any]]) -> int:
        now = utc_now_iso()
        values: list[tuple[Any, ...]] = []
        tag_values: list[tuple[Any, ...]] = []
        active_codes: set[str] = set()
        for row in rows:
            code = str(row.get("ts_code", ""))
            name = str(row.get("name", ""))
            market = str(row.get("market", ""))
            is_supported = bool(
                re.match(r"^(600|601|603|605)\d{3}\.SH$", code)
                or re.match(r"^(000|001|002|003)\d{3}\.SZ$", code)
                or re.match(r"^(300|301)\d{3}\.SZ$", code)
            )
            excluded = ""
            if not is_supported:
                excluded = "not_main_or_chinext"
            elif "ST" in name.upper() or "退" in name:
                excluded = "risk_warning_or_delisting"
            active = int(not excluded)
            if active:
                active_codes.add(code)
            values.append(
                (
                    code,
                    row.get("symbol"),
                    name,
                    row.get("exchange"),
                    market,
                    row.get("industry"),
                    row.get("list_date"),
                    active,
                    excluded or None,
                    now,
                )
            )
            industry = str(row.get("industry", "")).strip()
            if active and industry:
                tag_values.append((code, industry, "industry", "tushare_stock_basic", 0.5, now))
        with self.connect() as connection:
            connection.execute(
                "UPDATE stocks SET active=0, excluded_reason='not_in_latest_universe'"
            )
            connection.executemany(
                """
                INSERT INTO stocks(
                    code, symbol, name, exchange, market, industry, list_date,
                    active, excluded_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    symbol=excluded.symbol, name=excluded.name, exchange=excluded.exchange,
                    market=excluded.market, industry=excluded.industry,
                    list_date=excluded.list_date, active=excluded.active,
                    excluded_reason=excluded.excluded_reason, updated_at=excluded.updated_at
                """,
                values,
            )
            connection.executemany(
                """
                INSERT INTO stock_tags(code, tag, tag_type, source, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, tag, source) DO UPDATE SET
                    confidence=excluded.confidence, updated_at=excluded.updated_at
                """,
                tag_values,
            )
        return len(active_codes)

    def active_stock_map(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT code, name, list_date, industry, market FROM stocks WHERE active=1"
            ).fetchall()
        return {str(row["code"]): dict(row) for row in rows}

    def active_stock_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM stocks WHERE active=1"
            ).fetchone()
        return int(row["count"])

    def upsert_daily_limits(self, trade_date: str, rows: Sequence[dict[str, Any]]) -> int:
        values = [
            (
                trade_date,
                row.get("ts_code"),
                row.get("pre_close"),
                row.get("up_limit"),
                row.get("down_limit"),
            )
            for row in rows
            if row.get("ts_code")
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO daily_limits(trade_date, code, pre_close, upper_limit, lower_limit)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    pre_close=excluded.pre_close,
                    upper_limit=excluded.upper_limit,
                    lower_limit=excluded.lower_limit
                """,
                values,
            )
        return len(values)

    def daily_limit_map(self, trade_date: str) -> dict[str, tuple[float | None, float | None]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT code, upper_limit, lower_limit FROM daily_limits WHERE trade_date=?",
                (trade_date,),
            ).fetchall()
        return {str(row["code"]): (row["upper_limit"], row["lower_limit"]) for row in rows}

    def upsert_daily_metrics(self, rows: Sequence[dict[str, Any]]) -> int:
        now = utc_now_iso()
        active = self.active_stock_map()
        values = [
            (
                str(row.get("trade_date") or ""),
                str(row.get("ts_code") or ""),
                row.get("close"),
                row.get("turnover_rate"),
                row.get("volume_ratio"),
                row.get("float_share"),
                row.get("total_mv"),
                row.get("circ_mv"),
                now,
            )
            for row in rows
            if str(row.get("ts_code") or "") in active and row.get("trade_date")
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO stock_daily_metrics(
                    trade_date, code, close, turnover_rate, volume_ratio,
                    float_share, total_mv, circ_mv, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    close=excluded.close,
                    turnover_rate=excluded.turnover_rate,
                    volume_ratio=excluded.volume_ratio,
                    float_share=excluded.float_share,
                    total_mv=excluded.total_mv,
                    circ_mv=excluded.circ_mv,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def upsert_daily_bars(self, rows: Sequence[dict[str, Any]]) -> int:
        updated_at = utc_now_iso()
        values = []
        for row in rows:
            code = str(row.get("ts_code") or row.get("code") or "")
            trade_date = str(row.get("trade_date") or "").replace("-", "")
            if not code or not trade_date:
                continue
            values.append(
                (
                    trade_date,
                    code,
                    _number(row.get("open")),
                    _number(row.get("high")),
                    _number(row.get("low")),
                    _number(row.get("close")),
                    _number(row.get("pre_close")),
                    _number(row.get("pct_chg") or row.get("pct_change")),
                    _number(row.get("vol") or row.get("volume")),
                    _number(row.get("amount")),
                    updated_at,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO stock_daily_bars(
                    trade_date, code, open, high, low, close, pre_close,
                    pct_change, volume, amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, pre_close=excluded.pre_close,
                    pct_change=excluded.pct_change, volume=excluded.volume,
                    amount=excluded.amount, updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def daily_bar(self, trade_date: str, code: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stock_daily_bars WHERE trade_date=? AND code=?",
                (trade_date.replace("-", ""), code),
            ).fetchone()
        return dict(row) if row else None

    def daily_bars_for_date(self, trade_date: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stock_daily_bars WHERE trade_date=? ORDER BY code",
                (trade_date.replace("-", ""),),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_quote_history(
        self, trade_date: str, depth: int = 2
    ) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER(PARTITION BY code ORDER BY captured_at DESC) AS rn
                    FROM quote_snapshots WHERE trade_date=?
                )
                SELECT * FROM ranked WHERE rn <= ? ORDER BY code, captured_at DESC
                """,
                (trade_date, depth),
            ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row["code"]), []).append(dict(row))
        return result

    def save_quotes(self, quotes: Iterable[Quote], trade_date: str, slot: str) -> int:
        values = [
            (
                trade_date,
                slot,
                quote.captured_at.isoformat(timespec="seconds"),
                quote.code,
                quote.name,
                quote.pre_close,
                quote.open,
                quote.high,
                quote.low,
                quote.close,
                quote.volume,
                quote.amount,
                quote.trades,
                quote.trade_time,
                quote.pct_change,
            )
            for quote in quotes
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO quote_snapshots(
                    trade_date, slot, captured_at, code, name, pre_close, open, high,
                    low, close, volume, amount, trades, provider_trade_time, pct_change
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return len(values)

    def save_anomalies(self, anomalies: Sequence[Anomaly]) -> int:
        values = [
            (
                anomaly.captured_at.date().isoformat(),
                anomaly.captured_at.isoformat(timespec="seconds"),
                anomaly.code,
                anomaly.name,
                anomaly.direction.value,
                anomaly.severity,
                anomaly.pct_change,
                anomaly.change_5m,
                anomaly.amount_delta,
                anomaly.trade_delta,
                int(anomaly.is_hard_event),
                json.dumps(anomaly.event_types, ensure_ascii=False),
                json.dumps(anomaly.reasons, ensure_ascii=False),
                json.dumps(anomaly.metrics, ensure_ascii=False),
            )
            for anomaly in anomalies
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO anomaly_events(
                    trade_date, captured_at, code, name, direction, severity,
                    pct_change, change_5m, amount_delta, trade_delta, is_hard_event,
                    event_types_json, reasons_json, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return len(values)

    def recent_anomalies(
        self, since: datetime, direction: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM anomaly_events WHERE captured_at >= ?"
        params: list[Any] = [since.isoformat(timespec="seconds")]
        if direction:
            sql += " AND direction=?"
            params.append(direction)
        sql += " ORDER BY captured_at DESC, severity DESC"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return _decode_anomaly_rows(rows)

    def latest_anomaly_trade_date(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) AS trade_date FROM anomaly_events"
            ).fetchone()
        return str(row["trade_date"]) if row and row["trade_date"] else None

    def anomalies_for_trade_date(self, trade_date: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM anomaly_events WHERE trade_date=?
                ORDER BY captured_at DESC, severity DESC LIMIT ?
                """,
                (trade_date, limit),
            ).fetchall()
        return _decode_anomaly_rows(rows)

    def high_signal_anomalies_for_trade_date(
        self, trade_date: str, minimum_severity: float = 68.0, limit: int = 160
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER(
                        PARTITION BY code, direction
                        ORDER BY captured_at DESC, severity DESC
                    ) AS rn
                    FROM anomaly_events
                    WHERE trade_date=? AND (severity>=? OR is_hard_event=1)
                )
                SELECT * FROM ranked WHERE rn=1
                ORDER BY is_hard_event DESC, severity DESC, pct_change DESC
                LIMIT ?
                """,
                (trade_date, minimum_severity, limit),
            ).fetchall()
        items = _decode_anomaly_rows(rows)
        tags = self.tags_for_codes([str(item["code"]) for item in items])
        for item in items:
            stock_tags = tags.get(str(item["code"]), [])
            item["themes"] = _unique_tags(stock_tags, "theme")[:6]
            item["industries"] = _unique_tags(stock_tags, "industry")[:3]
        return items

    def tags_for_codes(self, codes: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT code, tag, tag_type, source, confidence FROM stock_tags
                WHERE code IN ({placeholders})
                """,
                list(codes),
            ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row["code"]), []).append(dict(row))
        return result

    def upsert_kpl_events(self, rows: Sequence[dict[str, Any]]) -> int:
        now = utc_now_iso()
        active = self.active_stock_map()
        event_values: list[tuple[Any, ...]] = []
        tag_values: list[tuple[Any, ...]] = []
        for row in rows:
            code = str(row.get("ts_code") or "")
            if code not in active:
                continue
            themes = _split_tags(str(row.get("theme") or ""))
            event_values.append(
                (
                    str(row.get("trade_date") or ""),
                    code,
                    str(row.get("name") or active[code]["name"]),
                    str(row.get("tag") or ""),
                    json.dumps(themes, ensure_ascii=False),
                    row.get("status"),
                    row.get("lu_time"),
                    row.get("open_time"),
                    row.get("last_time"),
                    row.get("lu_desc"),
                    row.get("pct_chg"),
                    row.get("rt_pct_chg"),
                    row.get("amount"),
                    json.dumps(row, ensure_ascii=False),
                    now,
                )
            )
            for theme in themes:
                tag_values.append((code, theme, "theme", "tushare_kpl_list", 0.95, now))
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO kpl_events(
                    trade_date, code, name, board_tag, themes_json, status,
                    limit_up_time, open_time, last_limit_time, limit_reason,
                    pct_change, realtime_pct_change, amount, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code, board_tag) DO UPDATE SET
                    themes_json=excluded.themes_json, status=excluded.status,
                    limit_up_time=excluded.limit_up_time, open_time=excluded.open_time,
                    last_limit_time=excluded.last_limit_time, limit_reason=excluded.limit_reason,
                    pct_change=excluded.pct_change,
                    realtime_pct_change=excluded.realtime_pct_change,
                    amount=excluded.amount, raw_json=excluded.raw_json, synced_at=excluded.synced_at
                """,
                event_values,
            )
            connection.executemany(
                """
                INSERT INTO stock_tags(code, tag, tag_type, source, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, tag, source) DO UPDATE SET
                    confidence=excluded.confidence, updated_at=excluded.updated_at
                """,
                tag_values,
            )
        return len(event_values)

    def upsert_chinext_growth_events(
        self, trade_date: str, quotes: Sequence[Quote]
    ) -> int:
        """Persist ChiNext stocks whose intraday high first exceeds +10%."""
        active = self.active_stock_map()
        selected = []
        for quote in quotes:
            if not _is_chinext_code(quote.code) or quote.code not in active:
                continue
            if quote.pre_close <= 0 or quote.high <= 0:
                continue
            peak_pct = (quote.high / quote.pre_close - 1.0) * 100.0
            if peak_pct <= 10.0:
                continue
            selected.append((quote, peak_pct))
        if not selected:
            return 0
        tags = self.tags_for_codes([quote.code for quote, _ in selected])
        now = utc_now_iso()
        values = []
        for quote, peak_pct in selected:
            themes = _high_confidence_theme_tags(tags.get(quote.code, []))
            signal_time = quote.captured_at.strftime("%H%M%S")
            reason = (
                f"创业板盘中最高涨幅{peak_pct:.2f}%，按涨停等效样本进入共同事件聚合；"
                "具体上涨原因结合题材标签、公司关联和当日新闻核查"
            )
            raw = {
                "source": "tushare_rt_k",
                "qualification": "chinext_intraday_high_gt_10pct",
                "peak_pct_change": round(peak_pct, 4),
                "current_pct_change": round(quote.pct_change, 4),
            }
            values.append(
                (
                    trade_date.replace("-", ""),
                    quote.code,
                    quote.name or str(active[quote.code]["name"]),
                    "创业板涨幅超10%",
                    json.dumps(themes, ensure_ascii=False),
                    "创业板强势",
                    signal_time,
                    reason,
                    peak_pct,
                    quote.pct_change,
                    quote.amount,
                    json.dumps(raw, ensure_ascii=False),
                    now,
                )
            )
        return self._upsert_chinext_growth_values(values)

    def upsert_chinext_daily_growth_events(
        self, trade_date: str, rows: Sequence[dict[str, Any]]
    ) -> int:
        """Rebuild missed ChiNext >10% intraday signals from official daily highs."""
        active = self.active_stock_map()
        selected = []
        for row in rows:
            code = str(row.get("ts_code") or row.get("code") or "")
            if not _is_chinext_code(code) or code not in active:
                continue
            pre_close = _number(row.get("pre_close"))
            high = _number(row.get("high"))
            if pre_close <= 0 or high <= 0:
                continue
            peak_pct = (high / pre_close - 1.0) * 100.0
            if peak_pct > 10.0:
                selected.append((row, code, peak_pct))
        if not selected:
            return 0
        tags = self.tags_for_codes([code for _, code, _ in selected])
        now = utc_now_iso()
        values = []
        for row, code, peak_pct in selected:
            close = _number(row.get("close"))
            pre_close = _number(row.get("pre_close"))
            close_pct = (close / pre_close - 1.0) * 100.0 if close > 0 else 0.0
            themes = _high_confidence_theme_tags(tags.get(code, []))
            reason = (
                f"创业板当日最高涨幅{peak_pct:.2f}%，由正式日线回补为涨停等效样本；"
                "具体上涨原因结合题材标签、公司关联和当日新闻核查"
            )
            raw = {
                "source": "tushare_daily",
                "qualification": "chinext_intraday_high_gt_10pct_backfill",
                "peak_pct_change": round(peak_pct, 4),
                "close_pct_change": round(close_pct, 4),
            }
            values.append(
                (
                    trade_date.replace("-", ""),
                    code,
                    str(row.get("name") or active[code]["name"]),
                    "创业板涨幅超10%",
                    json.dumps(themes, ensure_ascii=False),
                    "创业板强势",
                    "150000",
                    reason,
                    peak_pct,
                    close_pct,
                    _number(row.get("amount")),
                    json.dumps(raw, ensure_ascii=False),
                    now,
                )
            )
        return self._upsert_chinext_growth_values(values)

    def _upsert_chinext_growth_values(self, values: Sequence[tuple[Any, ...]]) -> int:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO kpl_events(
                    trade_date, code, name, board_tag, themes_json, status,
                    limit_up_time, limit_reason, pct_change, realtime_pct_change,
                    amount, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code, board_tag) DO UPDATE SET
                    name=excluded.name,
                    themes_json=CASE WHEN excluded.themes_json='[]'
                        THEN kpl_events.themes_json ELSE excluded.themes_json END,
                    status=excluded.status,
                    limit_up_time=COALESCE(kpl_events.limit_up_time, excluded.limit_up_time),
                    limit_reason=excluded.limit_reason,
                    pct_change=MAX(COALESCE(kpl_events.pct_change, 0), excluded.pct_change),
                    realtime_pct_change=excluded.realtime_pct_change,
                    amount=excluded.amount,
                    raw_json=excluded.raw_json,
                    synced_at=excluded.synced_at
                """,
                values,
            )
        return len(values)

    def upsert_kpl_concept_members(self, rows: Sequence[dict[str, Any]]) -> int:
        now = utc_now_iso()
        active = self.active_stock_map()
        tag_values = []
        membership_values = []
        for row in rows:
            # Tushare kpl_concept_cons: ts_code/name identify the concept;
            # con_code/con_name identify the constituent stock.
            code = str(row.get("con_code") or "")
            tag = str(row.get("name") or "").strip()
            if code in active and tag:
                tag_values.append((code, tag, "theme", "tushare_kpl_concept_cons", 0.9, now))
                membership_values.append(
                    (
                        str(row.get("trade_date") or ""),
                        str(row.get("ts_code") or ""),
                        tag,
                        code,
                        str(row.get("con_name") or active[code]["name"]),
                        row.get("desc"),
                        int(_number(row.get("hot_num"))),
                        now,
                    )
                )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO stock_tags(code, tag, tag_type, source, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, tag, source) DO UPDATE SET
                    confidence=excluded.confidence, updated_at=excluded.updated_at
                """,
                tag_values,
            )
            connection.executemany(
                """
                INSERT INTO kpl_concept_memberships(
                    trade_date, concept_id, concept_name, code, name,
                    description, hot_num, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, concept_id, code) DO UPDATE SET
                    concept_name=excluded.concept_name,
                    name=excluded.name,
                    description=excluded.description,
                    hot_num=excluded.hot_num,
                    synced_at=excluded.synced_at
                """,
                membership_values,
            )
        return len(membership_values)

    def limit_touch_events(self, trade_date: str) -> list[dict[str, Any]]:
        """Return one deterministic limit-touch record per stock for semantic clustering."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER(
                        PARTITION BY code
                        ORDER BY CASE board_tag
                                     WHEN '涨停' THEN 0
                                     WHEN '创业板涨幅超10%' THEN 1
                                     ELSE 2
                                 END,
                                 synced_at DESC
                    ) AS rn
                    FROM kpl_events
                    WHERE trade_date=?
                      AND board_tag IN ('涨停','创业板涨幅超10%','炸板')
                )
                SELECT ranked.trade_date, ranked.code, ranked.name, ranked.board_tag,
                       ranked.themes_json, ranked.status, ranked.limit_up_time,
                       ranked.open_time, ranked.last_limit_time, ranked.limit_reason,
                       ranked.pct_change, ranked.realtime_pct_change, ranked.synced_at,
                       stocks.market
                FROM ranked LEFT JOIN stocks ON stocks.code=ranked.code
                WHERE rn=1
                ORDER BY ranked.limit_up_time, ranked.code
                """,
                (trade_date,),
            ).fetchall()
            codes = [str(row["code"]) for row in rows]
            tag_rows: list[sqlite3.Row] = []
            if codes:
                placeholders = ",".join("?" for _ in codes)
                tag_rows = connection.execute(
                    f"""
                    SELECT code, concept_name AS tag,
                           'tushare_kpl_concept_cons' AS source,
                           0.9 AS confidence
                    FROM kpl_concept_memberships
                    WHERE trade_date=? AND code IN ({placeholders})
                    ORDER BY hot_num DESC, concept_name
                    """,
                    [trade_date, *codes],
                ).fetchall()
                tag_rows.extend(
                    connection.execute(
                        f"""
                        SELECT code, tag, source, confidence
                        FROM stock_tags
                        WHERE code IN ({placeholders}) AND tag_type='theme'
                          AND confidence>=0.9
                        ORDER BY confidence DESC, updated_at DESC, tag
                        """,
                        codes,
                    ).fetchall()
                )
        tags: dict[str, list[dict[str, Any]]] = {}
        for row in tag_rows:
            bucket = tags.setdefault(str(row["code"]), [])
            if len(bucket) < 12 and str(row["tag"]) not in {str(item["tag"]) for item in bucket}:
                bucket.append(dict(row))
        result = []
        for row in rows:
            item = dict(row)
            item["themes"] = json.loads(item.pop("themes_json") or "[]")
            item["concept_tags"] = tags.get(str(item["code"]), [])
            result.append(item)
        return result

    def kpl_events_for_codes(self, trade_date: str, codes: Sequence[str]) -> list[dict[str, Any]]:
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM kpl_events WHERE trade_date=? AND code IN ({placeholders})",
                [trade_date, *codes],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["themes"] = json.loads(item.pop("themes_json"))
            item.pop("raw_json", None)
            result.append(item)
        return result

    def has_kpl_events_for_date(self, trade_date: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM kpl_events WHERE trade_date=? LIMIT 1", (trade_date,)
            ).fetchone()
        return row is not None

    def kpl_theme_clusters(self, trade_date: str) -> list[dict[str, Any]]:
        """Return themes grouped by qualifying same-day market-strength signals.

        Limit-up, failed boards and ChiNext intraday highs above 10% count. A stock
        is counted once per theme, preferring the strongest available record.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, code, name, board_tag, themes_json, status,
                       limit_up_time, open_time, last_limit_time, limit_reason,
                       pct_change, realtime_pct_change, synced_at
                FROM kpl_events
                WHERE trade_date=?
                  AND board_tag IN ('涨停','创业板涨幅超10%','炸板')
                ORDER BY CASE board_tag
                             WHEN '涨停' THEN 0
                             WHEN '创业板涨幅超10%' THEN 1
                             ELSE 2
                         END,
                         limit_up_time ASC
                """,
                (trade_date,),
            ).fetchall()
        clusters: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            themes = json.loads(str(item.pop("themes_json") or "[]"))
            for theme in themes:
                tag = str(theme).strip()
                if not tag:
                    continue
                clusters.setdefault(tag, {}).setdefault(str(item["code"]), item)
        return [
            {
                "tag": tag,
                "members": list(members.values()),
                "touch_count": len(members),
                "sealed_count": sum(item.get("board_tag") == "涨停" for item in members.values()),
                "growth_count": sum(
                    item.get("board_tag") == "创业板涨幅超10%"
                    for item in members.values()
                ),
                "failed_count": sum(item.get("board_tag") == "炸板" for item in members.values()),
            }
            for tag, members in sorted(
                clusters.items(), key=lambda item: len(item[1]), reverse=True
            )
        ]

    def eligible_members_for_tag(self, tag: str, limit: int = 160) -> list[dict[str, Any]]:
        return self.eligible_members_for_tags([tag], limit)

    def eligible_members_for_tags(
        self, tags: Sequence[str], limit: int = 160
    ) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.code, s.name, s.industry,
                       GROUP_CONCAT(DISTINCT st.tag) AS matched_tags,
                       MAX(st.confidence) AS confidence,
                       MAX(st.updated_at) AS updated_at,
                       'tushare_theme_evidence' AS source
                FROM stock_tags st
                JOIN stocks s ON s.code=st.code
                WHERE st.tag IN ({placeholders}) AND st.tag_type='theme' AND st.confidence>=0.9
                  AND s.active=1
                GROUP BY s.code, s.name, s.industry
                ORDER BY MAX(st.confidence) DESC, MAX(st.updated_at) DESC
                LIMIT ?
                """,
                (*normalized, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["matched_tags"] = _split_tags(str(item.get("matched_tags") or ""))
            result.append(item)
        return result

    def semantic_cluster_run(self, trade_date: str, input_signature: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM semantic_cluster_runs
                WHERE trade_date=? AND input_signature=?
                """,
                (trade_date, input_signature),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["clusters"] = json.loads(item.pop("clusters_json") or "[]")
        return item

    def save_semantic_cluster_run(
        self,
        trade_date: str,
        input_signature: str,
        model: str,
        status: str,
        clusters: Sequence[dict[str, Any]],
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO semantic_cluster_runs(
                    trade_date, input_signature, created_at, model,
                    status, clusters_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, input_signature) DO UPDATE SET
                    created_at=excluded.created_at,
                    model=excluded.model,
                    status=excluded.status,
                    clusters_json=excluded.clusters_json,
                    error=excluded.error
                """,
                (
                    trade_date,
                    input_signature,
                    utc_now_iso(),
                    model,
                    status,
                    json.dumps(list(clusters), ensure_ascii=False),
                    error[:1000] if error else None,
                ),
            )

    def historical_theme_matches(
        self,
        *,
        theme_id: int,
        shared_tag: str,
        member_codes: Sequence[str],
        since_date: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, shared_tag, provisional_name, suggested_name, final_name,
                       status, discovered_at, day1_date, admission_status
                FROM candidate_themes
                WHERE id<>? AND discovered_at>=?
                ORDER BY discovered_at DESC
                """,
                (theme_id, since_date),
            ).fetchall()
            candidates = [dict(row) for row in rows]
            if not candidates:
                return []
            ids = [int(item["id"]) for item in candidates]
            placeholders = ",".join("?" for _ in ids)
            member_rows = connection.execute(
                f"SELECT theme_id, code FROM theme_members WHERE theme_id IN ({placeholders}) "
                "AND active=1",
                ids,
            ).fetchall()
        members_by_theme: dict[int, set[str]] = {}
        for row in member_rows:
            members_by_theme.setdefault(int(row["theme_id"]), set()).add(str(row["code"]))
        current = set(member_codes)
        matches = []
        for item in candidates:
            old = members_by_theme.get(int(item["id"]), set())
            overlap = len(current & old) / max(1, min(len(current), len(old)))
            names = {
                str(item.get("shared_tag") or ""),
                str(item.get("provisional_name") or ""),
                str(item.get("suggested_name") or ""),
                str(item.get("final_name") or ""),
            }
            exact_tag = shared_tag in names or str(item.get("shared_tag")) == shared_tag
            if not exact_tag and overlap < 0.5:
                continue
            item["member_overlap"] = round(overlap, 3)
            item["exact_tag_match"] = exact_tag
            matches.append(item)
        return matches[:limit]

    def recent_live_theme_fingerprint(
        self, shared_tag: str, direction: str, since: datetime
    ) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint FROM candidate_themes
                WHERE shared_tag=? AND direction=? AND status IN ('pending','watching','confirmed')
                  AND updated_at>=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (shared_tag, direction, since.isoformat(timespec="seconds")),
            ).fetchone()
        return str(row["fingerprint"]) if row else None

    def latest_quotes(self, codes: Sequence[str] | None = None) -> list[dict[str, Any]]:
        condition = ""
        params: list[Any] = []
        if codes:
            condition = f"WHERE code IN ({','.join('?' for _ in codes)})"
            params.extend(codes)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER(PARTITION BY code ORDER BY captured_at DESC) AS rn
                    FROM quote_snapshots {condition}
                ) SELECT * FROM ranked WHERE rn=1
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_candidate(
        self,
        *,
        fingerprint: str,
        provisional_name: str,
        shared_tag: str,
        direction: str,
        discovered_at: str,
        day1_date: str,
        discovery_reason: str,
        members: Sequence[dict[str, Any]],
        admission_status: str = "awaiting_ai",
        cluster_method: str = "exact_tag",
        cluster_confidence: float = 0.0,
        cluster_aliases: Sequence[str] = (),
    ) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO candidate_themes(
                    fingerprint, provisional_name, shared_tag, direction,
                    discovered_at, day1_date, discovery_reason, admission_status,
                    cluster_method, cluster_confidence, cluster_aliases_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    discovery_reason=excluded.discovery_reason,
                    cluster_method=excluded.cluster_method,
                    cluster_confidence=excluded.cluster_confidence,
                    cluster_aliases_json=excluded.cluster_aliases_json,
                    admission_status=CASE
                        WHEN candidate_themes.status='pending' THEN excluded.admission_status
                        ELSE candidate_themes.admission_status
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    fingerprint,
                    provisional_name,
                    shared_tag,
                    direction,
                    discovered_at,
                    day1_date,
                    discovery_reason,
                    admission_status,
                    cluster_method,
                    max(0.0, min(100.0, float(cluster_confidence))),
                    json.dumps(list(dict.fromkeys(cluster_aliases)), ensure_ascii=False),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM candidate_themes WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            theme_id = int(row["id"])
            for member in members:
                connection.execute(
                    """
                    INSERT INTO theme_members(
                        theme_id, code, name, membership_source, evidence_json,
                        first_seen_at, last_seen_at, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(theme_id, code) DO UPDATE SET
                        name=excluded.name,
                        evidence_json=excluded.evidence_json,
                        last_seen_at=excluded.last_seen_at,
                        active=1
                    """,
                    (
                        theme_id,
                        member["code"],
                        member["name"],
                        str(member.get("membership_source") or "limit_touch_cluster"),
                        json.dumps(member.get("evidence", {}), ensure_ascii=False),
                        discovered_at,
                        discovered_at,
                    ),
                )
        return theme_id

    def list_themes(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM candidate_themes"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY pinned DESC, updated_at DESC"
        with self.connect() as connection:
            themes = [dict(row) for row in connection.execute(sql, params).fetchall()]
            for theme in themes:
                theme["cluster_aliases"] = json.loads(theme.pop("cluster_aliases_json") or "[]")
                baseline_at = str(theme.get("confirmed_at") or theme["discovered_at"])
                member_rows = connection.execute(
                    """
                    SELECT tm.code, tm.name, tm.membership_source, tm.evidence_json,
                           tm.first_seen_at, tm.last_seen_at, tm.active, tm.role,
                           (SELECT q.close FROM quote_snapshots q
                            WHERE q.code=tm.code ORDER BY q.captured_at DESC LIMIT 1)
                               AS current_price,
                           (SELECT q.pct_change FROM quote_snapshots q
                            WHERE q.code=tm.code ORDER BY q.captured_at DESC LIMIT 1)
                               AS current_pct,
                           (SELECT q.captured_at FROM quote_snapshots q
                            WHERE q.code=tm.code ORDER BY q.captured_at DESC LIMIT 1)
                               AS quote_captured_at,
                           (SELECT m.trade_date FROM stock_daily_metrics m
                            WHERE m.code=tm.code ORDER BY m.trade_date DESC LIMIT 1)
                               AS metric_trade_date,
                           (SELECT m.turnover_rate FROM stock_daily_metrics m
                            WHERE m.code=tm.code ORDER BY m.trade_date DESC LIMIT 1)
                               AS turnover_rate,
                           (SELECT m.volume_ratio FROM stock_daily_metrics m
                            WHERE m.code=tm.code ORDER BY m.trade_date DESC LIMIT 1)
                               AS volume_ratio,
                           (SELECT m.float_share FROM stock_daily_metrics m
                            WHERE m.code=tm.code ORDER BY m.trade_date DESC LIMIT 1)
                               AS float_share,
                           (SELECT m.total_mv FROM stock_daily_metrics m
                            WHERE m.code=tm.code ORDER BY m.trade_date DESC LIMIT 1)
                               AS total_mv,
                           (SELECT m.circ_mv FROM stock_daily_metrics m
                            WHERE m.code=tm.code ORDER BY m.trade_date DESC LIMIT 1)
                               AS circ_mv,
                           COALESCE(
                               (SELECT b.close FROM quote_snapshots b
                                WHERE b.code=tm.code AND b.captured_at<=?
                                ORDER BY b.captured_at DESC LIMIT 1),
                               (SELECT a.close FROM quote_snapshots a
                                WHERE a.code=tm.code AND a.captured_at>?
                                ORDER BY a.captured_at ASC LIMIT 1)
                           ) AS baseline_price
                    FROM theme_members tm WHERE tm.theme_id=?
                    """,
                    (baseline_at, baseline_at, theme["id"]),
                ).fetchall()
                theme["members"] = []
                for row in member_rows:
                    member = dict(row)
                    member["evidence"] = json.loads(member.pop("evidence_json"))
                    current = _number(member.get("current_price"))
                    baseline = _number(member.get("baseline_price"))
                    member["confirmed_return"] = (
                        round((current / baseline - 1.0) * 100.0, 3)
                        if current and baseline
                        else None
                    )
                    circ_mv = _number(member.get("circ_mv"))
                    member["circ_mv_billion"] = round(circ_mv / 10_000.0, 2) if circ_mv else None
                    theme["members"].append(member)
                self._attach_board_context(connection, theme)
                self._attach_theme_market_summary(theme)
                score = connection.execute(
                    """
                    SELECT * FROM theme_scores WHERE theme_id=?
                    ORDER BY calculated_at DESC LIMIT 1
                    """,
                    (theme["id"],),
                ).fetchone()
                theme["score"] = dict(score) if score else None
                if theme["score"]:
                    theme["score"]["details"] = json.loads(theme["score"].pop("details_json"))
                explanation = connection.execute(
                    """
                    SELECT created_at, model, suggested_name, explanation,
                           catalyst_summary, catalyst_duration, sources_json
                    FROM ai_explanations WHERE theme_id=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (theme["id"],),
                ).fetchone()
                theme["latest_explanation"] = dict(explanation) if explanation else None
                if theme["latest_explanation"]:
                    theme["latest_explanation"]["sources"] = json.loads(
                        theme["latest_explanation"].pop("sources_json")
                    )
                catalyst_rows = connection.execute(
                    """
                    SELECT title, summary, source_name, source_url, published_at,
                           catalyst_type, evidence_level, source_kind, captured_at
                    FROM theme_catalysts WHERE theme_id=?
                    ORDER BY COALESCE(published_at, captured_at) DESC, id DESC LIMIT 12
                    """,
                    (theme["id"],),
                ).fetchall()
                theme["catalysts"] = [dict(row) for row in catalyst_rows]
                admission = connection.execute(
                    """
                    SELECT created_at, model, is_new_theme, novelty_confidence,
                           catalyst_confidence, expected_duration_days,
                           leader_candidate_code, leader_upside_scenario_pct,
                           admitted, decision_level, decision_reason, historical_matches_json,
                           proposed_members_json, validated_members_json
                    FROM theme_admission_reviews WHERE theme_id=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (theme["id"],),
                ).fetchone()
                theme["admission_review"] = dict(admission) if admission else None
                if theme["admission_review"]:
                    for key in (
                        "historical_matches_json",
                        "proposed_members_json",
                        "validated_members_json",
                    ):
                        theme["admission_review"][key.removesuffix("_json")] = json.loads(
                            theme["admission_review"].pop(key)
                        )
        return themes

    def set_theme_pin(self, theme_id: int, pinned: bool) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE candidate_themes SET pinned=?, updated_at=? WHERE id=?",
                (int(pinned), utc_now_iso(), theme_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"Theme {theme_id} not found")

    def archive_theme(self, theme_id: int) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM candidate_themes WHERE id=?", (theme_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Theme {theme_id} not found")
            connection.execute(
                """
                UPDATE candidate_themes SET status='archived', archived_at=?, pinned=0,
                    updated_at=? WHERE id=?
                """,
                (now, now, theme_id),
            )
            connection.execute(
                """
                UPDATE fund_flow_updates SET status='stopped', updated_at=?
                WHERE owner_type='theme' AND owner_id=?
                  AND status IN ('pending','running','failed')
                """,
                (now, theme_id),
            )

    def restore_theme(self, theme_id: int) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE candidate_themes SET status=CASE theme_level
                        WHEN 'early_watch' THEN 'watching'
                        WHEN 'formal' THEN 'confirmed'
                        WHEN 'rejected' THEN 'rejected'
                        ELSE 'pending'
                    END,
                    archived_at=NULL,
                    updated_at=? WHERE id=? AND status='archived'
                """,
                (now, theme_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"Archived theme {theme_id} not found")

    def set_admission_status(
        self, theme_id: int, status: str, reason: str, evidence_grade: str | None = None
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE candidate_themes SET admission_status=?, admission_reason=?,
                    evidence_grade=COALESCE(?, evidence_grade),
                    admission_reviewed_at=?, updated_at=? WHERE id=?
                """,
                (
                    status,
                    reason[:2000],
                    evidence_grade,
                    utc_now_iso(),
                    utc_now_iso(),
                    theme_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(f"Theme {theme_id} not found")

    def add_validated_members(
        self, theme_id: int, members: Sequence[dict[str, Any]], seen_at: str
    ) -> int:
        inserted = 0
        with self.connect() as connection:
            for member in members:
                cursor = connection.execute(
                    """
                    INSERT INTO theme_members(
                        theme_id, code, name, membership_source, evidence_json,
                        first_seen_at, last_seen_at, active, role
                    ) VALUES (?, ?, ?, 'ai_proposed_deterministic_validated', ?, ?, ?, 1, ?)
                    ON CONFLICT(theme_id, code) DO UPDATE SET
                        name=excluded.name, evidence_json=excluded.evidence_json,
                        last_seen_at=excluded.last_seen_at, active=1,
                        role=COALESCE(excluded.role, theme_members.role)
                    """,
                    (
                        theme_id,
                        member["code"],
                        member["name"],
                        json.dumps(member.get("evidence", {}), ensure_ascii=False),
                        seen_at,
                        seen_at,
                        member.get("role"),
                    ),
                )
                inserted += int(cursor.rowcount > 0)
        return inserted

    def save_admission_review(
        self,
        theme_id: int,
        item: dict[str, Any],
        historical_matches: Sequence[dict[str, Any]],
        validated_members: Sequence[dict[str, Any]],
        admitted: bool,
        decision_level: str,
        decision_reason: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO theme_admission_reviews(
                    theme_id, created_at, model, is_new_theme, novelty_confidence,
                    catalyst_confidence, expected_duration_days, leader_candidate_code,
                    leader_upside_scenario_pct, admitted, decision_reason,
                    decision_level,
                    historical_matches_json, proposed_members_json,
                    validated_members_json, raw_response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    theme_id,
                    utc_now_iso(),
                    item.get("model", "unknown"),
                    int(bool(item.get("is_new_theme"))),
                    float(item.get("novelty_confidence") or 0),
                    float(item.get("catalyst_confidence") or 0),
                    int(item.get("expected_duration_days") or 0),
                    item.get("leader_candidate_code"),
                    float(item.get("leader_upside_scenario_pct") or 0),
                    int(admitted),
                    decision_reason,
                    decision_level,
                    json.dumps(list(historical_matches), ensure_ascii=False),
                    json.dumps(item.get("proposed_members", []), ensure_ascii=False),
                    json.dumps(list(validated_members), ensure_ascii=False),
                    json.dumps(item.get("raw", {}), ensure_ascii=False),
                ),
            )

    def reclassify_legacy_pending(self, minimum_touches: int) -> dict[str, int]:
        """Re-evaluate old pending themes using retained KPL evidence."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, shared_tag, day1_date FROM candidate_themes
                WHERE status='pending' AND admission_status='legacy'
                """
            ).fetchall()
        waiting = 0
        failed = 0
        for row in rows:
            compact = str(row["day1_date"]).replace("-", "")
            cluster = next(
                (
                    item
                    for item in self.kpl_theme_clusters(compact)
                    if item["tag"] == row["shared_tag"]
                ),
                None,
            )
            if cluster and int(cluster["touch_count"]) >= minimum_touches:
                self.set_admission_status(
                    int(row["id"]),
                    "awaiting_ai",
                    f"旧候选复核通过：当日{cluster['touch_count']}只股票曾触及涨停",
                )
                waiting += 1
            else:
                with self.connect() as connection:
                    connection.execute(
                        """
                        UPDATE candidate_themes SET status='rejected',
                            admission_status='legacy_gate_failed',
                            admission_reason='旧候选无法证明同题材至少4只股票曾触及涨停',
                            admission_reviewed_at=?, updated_at=? WHERE id=?
                        """,
                        (utc_now_iso(), utc_now_iso(), row["id"]),
                    )
                failed += 1
        return {"awaiting_ai": waiting, "failed": failed}

    def _attach_board_context(self, connection: sqlite3.Connection, theme: dict[str, Any]) -> None:
        members = theme["members"]
        codes = [str(member["code"]) for member in members]
        if not codes:
            return
        placeholders = ",".join("?" for _ in codes)
        rows = connection.execute(
            f"""
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER(
                    PARTITION BY code ORDER BY trade_date DESC, synced_at DESC
                ) AS rn
                FROM kpl_events WHERE code IN ({placeholders})
            )
            SELECT * FROM ranked WHERE rn<=8 ORDER BY code, trade_date DESC
            """,
            codes,
        ).fetchall()
        histories: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            raw = json.loads(item.pop("raw_json") or "{}")
            item["themes"] = json.loads(item.pop("themes_json"))
            item["board_height"] = _board_height(item.get("status"))
            item["limit_order"] = raw.get("limit_order")
            item["turnover_rate"] = raw.get("turnover_rate")
            histories.setdefault(str(item["code"]), []).append(item)

        anomaly_date_row = connection.execute(
            "SELECT MAX(trade_date) AS trade_date FROM anomaly_events"
        ).fetchone()
        anomaly_date = str(anomaly_date_row["trade_date"] or "")
        first_moves: dict[str, str] = {}
        if anomaly_date:
            move_rows = connection.execute(
                f"""
                SELECT code, MIN(captured_at) AS first_move_at
                FROM anomaly_events
                WHERE trade_date=? AND direction='positive'
                  AND code IN ({placeholders})
                GROUP BY code
                """,
                [anomaly_date, *codes],
            ).fetchall()
            first_moves = {
                str(row["code"]): str(row["first_move_at"])
                for row in move_rows
                if row["first_move_at"]
            }

        limit_members = []
        for member in members:
            history = histories.get(str(member["code"]), [])
            latest = {}
            if history:
                latest_date = str(history[0].get("trade_date") or "")
                same_day = [
                    item for item in history if str(item.get("trade_date") or "") == latest_date
                ]
                current_pct = _number(member.get("current_pct"))
                preferred_tag = (
                    "涨停"
                    if any(item.get("board_tag") == "涨停" for item in same_day)
                    else "创业板涨幅超10%"
                    if current_pct > 10
                    else "炸板"
                )
                latest = next(
                    (item for item in same_day if item.get("board_tag") == preferred_tag),
                    same_day[0],
                )
            member["board_status"] = latest.get("status")
            member["board_height"] = int(latest.get("board_height") or 0)
            member["latest_board_tag"] = latest.get("board_tag")
            member["latest_limit_trade_date"] = latest.get("trade_date")
            member["first_limit_time"] = latest.get("limit_up_time")
            member["last_limit_time"] = latest.get("last_limit_time")
            member["open_time"] = latest.get("open_time")
            member["limit_order"] = latest.get("limit_order")
            member["first_move_at"] = first_moves.get(str(member["code"]))
            member["signal_active"] = str(member["code"]) in first_moves
            member["board_history"] = [
                {
                    "trade_date": item.get("trade_date"),
                    "tag": item.get("board_tag"),
                    "status": item.get("status"),
                    "first_limit_time": item.get("limit_up_time"),
                }
                for item in _one_board_event_per_day(history)[:5]
            ]
            if latest.get("board_tag") == "涨停" and latest.get("limit_up_time"):
                limit_members.append(member)

        limit_members.sort(key=lambda item: _clock_sort(item.get("first_limit_time")))
        for sequence, member in enumerate(limit_members, 1):
            member["limit_sequence"] = sequence

        move_times = {code: _parse_datetime(value) for code, value in first_moves.items()}
        move_times = {code: value for code, value in move_times.items() if value}
        for member in members:
            moved_at = move_times.get(str(member["code"]))
            follower_count = 0
            if moved_at:
                follower_count = sum(
                    0 < (other - moved_at).total_seconds() <= 30 * 60
                    for code, other in move_times.items()
                    if code != str(member["code"])
                )
            member["follow_count_30m"] = follower_count
            member["leader_signal"] = round(
                int(member.get("board_height") or 0) * 25
                + max(0.0, _number(member.get("current_pct"))) * 2
                + follower_count * 3
                + max(0, 12 - int(member.get("limit_sequence") or 12)),
                2,
            )
        leader_order = sorted(
            members,
            key=lambda item: (
                _number(item.get("leader_signal")),
                _number(item.get("current_pct")),
            ),
            reverse=True,
        )
        for rank, member in enumerate(leader_order, 1):
            member["leader_rank"] = rank
        members.sort(
            key=lambda item: (
                int(item.get("active") or 0),
                _number(item.get("current_pct")),
                int(bool(item.get("signal_active"))),
                _number(item.get("leader_signal")),
            ),
            reverse=True,
        )

    def _attach_theme_market_summary(self, theme: dict[str, Any]) -> None:
        members = [member for member in theme["members"] if member.get("active", 1)]
        current_values = [
            _number(member["current_pct"])
            for member in members
            if member.get("current_pct") is not None
        ]
        confirmed_values = [
            _number(member["confirmed_return"])
            for member in members
            if member.get("confirmed_return") is not None
        ]
        current_limit_date = max(
            (str(member.get("latest_limit_trade_date") or "") for member in members),
            default="",
        )
        theme["market_summary"] = {
            "member_count": len(members),
            "current_average_pct": round(sum(current_values) / len(current_values), 3)
            if current_values
            else None,
            "confirmed_average_return": round(sum(confirmed_values) / len(confirmed_values), 3)
            if confirmed_values
            else None,
            "up_count": sum(value > 0 for value in current_values),
            "strong_count": sum(value >= 5 for value in current_values),
            "limit_up_count": sum(
                member.get("latest_board_tag") == "涨停"
                and str(member.get("latest_limit_trade_date") or "") == current_limit_date
                for member in members
            ),
            "chinext_growth_count": sum(
                member.get("latest_board_tag") == "创业板涨幅超10%"
                and str(member.get("latest_limit_trade_date") or "") == current_limit_date
                for member in members
            ),
            "failed_limit_count": sum(
                member.get("latest_board_tag") == "炸板"
                and str(member.get("latest_limit_trade_date") or "") == current_limit_date
                for member in members
            ),
            "leader_code": min(
                members,
                key=lambda item: int(item.get("leader_rank") or 9999),
                default={},
            ).get("code"),
            "market_data_at": max(
                (str(member.get("quote_captured_at") or "") for member in members),
                default="",
            ),
            "metric_trade_date": max(
                (str(member.get("metric_trade_date") or "") for member in members),
                default="",
            ),
        }

    def get_theme(self, theme_id: int) -> dict[str, Any] | None:
        for theme in self.list_themes():
            if int(theme["id"]) == theme_id:
                return theme
        return None

    def set_theme_status(
        self,
        theme_id: int,
        status: str,
        final_name: str | None = None,
        catalyst_strength: float | None = None,
        catalyst_duration: str | None = None,
    ) -> None:
        now = utc_now_iso()
        confirmed_at = now if status == "confirmed" else None
        observation_at = now if status == "watching" else None
        theme_level = {
            "pending": "candidate",
            "watching": "early_watch",
            "confirmed": "formal",
            "rejected": "rejected",
        }.get(status)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM candidate_themes WHERE id=?", (theme_id,)
            ).fetchone()
            if not existing:
                raise KeyError(f"Theme {theme_id} not found")
            connection.execute(
                """
                UPDATE candidate_themes SET
                    status=?,
                    theme_level=COALESCE(?, theme_level),
                    final_name=COALESCE(?, final_name, suggested_name, provisional_name),
                    confirmed_at=COALESCE(?, confirmed_at),
                    observation_at=COALESCE(?, observation_at),
                    catalyst_strength=COALESCE(?, catalyst_strength),
                    catalyst_duration=COALESCE(?, catalyst_duration),
                    updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    theme_level,
                    final_name,
                    confirmed_at,
                    observation_at,
                    catalyst_strength,
                    catalyst_duration,
                    now,
                    theme_id,
                ),
            )

    def set_suggested_name(self, theme_id: int, suggested_name: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE candidate_themes SET suggested_name=?, updated_at=? WHERE id=?",
                (suggested_name, utc_now_iso(), theme_id),
            )

    def merge_themes(self, target_id: int, source_ids: Sequence[int]) -> None:
        if target_id in source_ids:
            raise ValueError("Target theme cannot also be a merge source")
        now = utc_now_iso()
        with self.connect() as connection:
            target = connection.execute(
                "SELECT id FROM candidate_themes WHERE id=?", (target_id,)
            ).fetchone()
            if not target:
                raise KeyError(f"Target theme {target_id} not found")
            for source_id in source_ids:
                members = connection.execute(
                    "SELECT * FROM theme_members WHERE theme_id=?", (source_id,)
                ).fetchall()
                for row in members:
                    connection.execute(
                        """
                        INSERT INTO theme_members(
                            theme_id, code, name, membership_source, evidence_json,
                            first_seen_at, last_seen_at, active, role
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(theme_id, code) DO UPDATE SET
                            last_seen_at=MAX(last_seen_at, excluded.last_seen_at), active=1
                        """,
                        (
                            target_id,
                            row["code"],
                            row["name"],
                            "human_merge",
                            row["evidence_json"],
                            row["first_seen_at"],
                            row["last_seen_at"],
                            row["active"],
                            row["role"],
                        ),
                    )
                connection.execute(
                    """
                    UPDATE candidate_themes SET status='merged', merged_into_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (target_id, now, source_id),
                )

    def split_theme(
        self, source_id: int, member_codes: Sequence[str], new_name: str, fingerprint: str
    ) -> int:
        if not member_codes:
            raise ValueError("A split needs at least one member")
        now = utc_now_iso()
        with self.connect() as connection:
            source = connection.execute(
                "SELECT * FROM candidate_themes WHERE id=?", (source_id,)
            ).fetchone()
            if not source:
                raise KeyError(f"Source theme {source_id} not found")
            placeholders = ",".join("?" for _ in member_codes)
            members = connection.execute(
                f"SELECT * FROM theme_members WHERE theme_id=? AND code IN ({placeholders})",
                [source_id, *member_codes],
            ).fetchall()
            if len(members) != len(set(member_codes)):
                raise ValueError("One or more split members do not belong to the source theme")
            cursor = connection.execute(
                """
                INSERT INTO candidate_themes(
                    fingerprint, provisional_name, final_name, shared_tag, status,
                    direction, discovered_at, day1_date, discovery_reason, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    new_name,
                    new_name,
                    source["shared_tag"],
                    source["direction"],
                    now,
                    source["day1_date"],
                    f"人工从题材#{source_id}拆分；股票成员由人工选择",
                    now,
                ),
            )
            new_id = int(cursor.lastrowid)
            for member in members:
                connection.execute(
                    """
                    INSERT INTO theme_members(
                        theme_id, code, name, membership_source, evidence_json,
                        first_seen_at, last_seen_at, active, role
                    ) VALUES (?, ?, ?, 'human_split', ?, ?, ?, 1, ?)
                    """,
                    (
                        new_id,
                        member["code"],
                        member["name"],
                        member["evidence_json"],
                        member["first_seen_at"],
                        now,
                        member["role"],
                    ),
                )
                connection.execute(
                    "UPDATE theme_members SET active=0 WHERE theme_id=? AND code=?",
                    (source_id, member["code"]),
                )
            connection.execute(
                "UPDATE candidate_themes SET updated_at=? WHERE id=?", (now, source_id)
            )
        return new_id

    def save_score(self, theme_id: int, score: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO theme_scores(
                    theme_id, calculated_at, heat, persistence, entry_risk,
                    lifecycle, confidence, leader_code, leader_influence,
                    leader_theme_divergence, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    theme_id,
                    score["calculated_at"],
                    score["heat"],
                    score["persistence"],
                    score["entry_risk"],
                    score["lifecycle"],
                    score["confidence"],
                    score.get("leader_code"),
                    score.get("leader_influence"),
                    int(score.get("leader_theme_divergence", False)),
                    json.dumps(score["details"], ensure_ascii=False),
                ),
            )

    def record_cohort(
        self, theme_id: int, trade_date: str, members: Sequence[dict[str, Any]]
    ) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO theme_cohorts(
                    theme_id, trade_date, board_level, code, outcome, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_id, trade_date, code) DO UPDATE SET
                    board_level=excluded.board_level,
                    outcome=excluded.outcome,
                    recorded_at=excluded.recorded_at
                """,
                [
                    (
                        theme_id,
                        trade_date,
                        int(member.get("board_level", 0)),
                        member["code"],
                        member.get("outcome"),
                        now,
                    )
                    for member in members
                ],
            )

    def update_cohort_next_day_returns(
        self, previous_trade_date: str, returns: dict[str, float]
    ) -> int:
        updated = 0
        with self.connect() as connection:
            for code, value in returns.items():
                cursor = connection.execute(
                    """
                    UPDATE theme_cohorts SET next_day_return=?
                    WHERE trade_date=? AND code=?
                    """,
                    (value, previous_trade_date, code),
                )
                updated += cursor.rowcount
        return updated

    def cohort_stats(self, theme_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS observations,
                    AVG(next_day_return) AS avg_next_day_return,
                    AVG(CASE WHEN next_day_return <= -5 THEN 1.0 ELSE 0.0 END) AS loss_ratio,
                    AVG(CASE WHEN outcome='炸板' THEN 1.0 ELSE 0.0 END) AS failed_ratio
                FROM theme_cohorts WHERE theme_id=?
                """,
                (theme_id,),
            ).fetchone()
        return dict(row) if row else {}

    def save_ai_explanation(self, theme_id: int, item: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_explanations(
                    theme_id, created_at, model, suggested_name, explanation,
                    catalyst_summary, catalyst_duration, merge_suggestions_json,
                    sources_json, raw_response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    theme_id,
                    utc_now_iso(),
                    item["model"],
                    item.get("suggested_name"),
                    item.get("explanation", ""),
                    item.get("catalyst_summary"),
                    item.get("catalyst_duration"),
                    json.dumps(item.get("merge_suggestions", []), ensure_ascii=False),
                    json.dumps(item.get("sources", []), ensure_ascii=False),
                    json.dumps(item.get("raw", {}), ensure_ascii=False),
                ),
            )

    def save_theme_catalysts(self, theme_id: int, catalysts: Sequence[dict[str, Any]]) -> int:
        captured_at = utc_now_iso()
        inserted = 0
        with self.connect() as connection:
            for item in catalysts:
                title = str(item.get("title") or "").strip()
                summary = str(item.get("summary") or "").strip()
                url = str(item.get("url") or "").strip()
                if not title or not summary:
                    continue
                fingerprint = hashlib.sha256(
                    f"{url}|{title}|{str(item.get('published_at') or '')[:10]}".encode()
                ).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO theme_catalysts(
                        theme_id, fingerprint, title, summary, source_name,
                        source_url, published_at, catalyst_type, evidence_level,
                        source_kind, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        theme_id,
                        fingerprint,
                        title,
                        summary,
                        item.get("source"),
                        url or None,
                        item.get("published_at"),
                        str(item.get("catalyst_type") or "update"),
                        str(item.get("evidence_level") or "inference"),
                        str(item.get("source_kind") or "unknown"),
                        captured_at,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def has_ai_explanation(self, theme_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM ai_explanations WHERE theme_id=? LIMIT 1", (theme_id,)
            ).fetchone()
        return row is not None

    def create_alert(
        self,
        dedupe_key: str,
        category: str,
        severity: str,
        title: str,
        body: str,
        theme_id: int | None = None,
        code: str | None = None,
    ) -> int | None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO alerts(
                    dedupe_key, created_at, category, severity, title, body, theme_id, code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (dedupe_key, utc_now_iso(), category, severity, title, body, theme_id, code),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def mark_alert_pushed(self, alert_id: int, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE alerts SET pushed_wecom=?, push_error=? WHERE id=?",
                (int(error is None), error, alert_id),
            )

    def recent_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_level2_report(self, report: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO level2_reports(
                    code, trade_date, generated_at, is_partial, method, report_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                    generated_at=excluded.generated_at,
                    is_partial=excluded.is_partial,
                    method=excluded.method,
                    report_json=excluded.report_json
                """,
                (
                    report["code"],
                    report["trade_date"],
                    report["generated_at"],
                    int(bool(report.get("partial"))),
                    report["method"],
                    json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def get_level2_report(self, code: str, trade_date: str | None = None) -> dict[str, Any] | None:
        condition = "code=?"
        params: list[Any] = [code]
        if trade_date:
            condition += " AND trade_date=?"
            params.append(trade_date)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT report_json FROM level2_reports WHERE {condition} "
                "ORDER BY trade_date DESC LIMIT 1",
                params,
            ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def prepare_fund_flow_updates(
        self,
        targets: Sequence[dict[str, Any]],
        trade_date: str,
        slot: str,
        updated_at: str,
    ) -> int:
        values = [
            (
                str(item["owner_type"]),
                int(item["owner_id"]),
                str(item["code"]),
                str(item.get("name") or item["code"]),
                trade_date,
                slot,
                int(item.get("priority_rank") or 0) or None,
                updated_at,
            )
            for item in targets
        ]
        if not values:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO fund_flow_updates(
                    owner_type, owner_id, code, name, trade_date, slot,
                    priority_rank, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(owner_type, owner_id, code, trade_date, slot) DO UPDATE SET
                    name=excluded.name,
                    priority_rank=excluded.priority_rank,
                    status=CASE WHEN fund_flow_updates.status='completed'
                                THEN 'completed' ELSE 'pending' END,
                    started_at=CASE WHEN fund_flow_updates.status='completed'
                                    THEN fund_flow_updates.started_at ELSE NULL END,
                    error=CASE WHEN fund_flow_updates.status='completed'
                               THEN fund_flow_updates.error ELSE NULL END,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def mark_fund_flow_codes_running(
        self, codes: Sequence[str], trade_date: str, slot: str, started_at: str
    ) -> None:
        if not codes:
            return
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE fund_flow_updates
                SET status='running', started_at=?, completed_at=NULL,
                    error=NULL, updated_at=?
                WHERE trade_date=? AND slot=? AND code IN ({placeholders})
                  AND status!='completed'
                  AND (
                    (owner_type='theme' AND EXISTS (
                        SELECT 1 FROM candidate_themes theme
                        WHERE theme.id=fund_flow_updates.owner_id
                          AND theme.status IN ('watching','confirmed')
                    ))
                    OR
                    (owner_type='test_pool' AND EXISTS (
                        SELECT 1 FROM test_pool_entries entry
                        WHERE entry.id=fund_flow_updates.owner_id
                          AND entry.status IN ('awaiting_buy','awaiting_exit')
                    ))
                  )
                """,
                [started_at, started_at, trade_date, slot, *codes],
            )

    def finish_fund_flow_code(
        self,
        code: str,
        trade_date: str,
        slot: str,
        *,
        completed_at: str,
        report: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        status = "completed" if report else "failed"
        report_json = (
            json.dumps(report, ensure_ascii=False, separators=(",", ":")) if report else None
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE fund_flow_updates
                SET status=?, completed_at=?, error=?, report_json=?, updated_at=?
                WHERE code=? AND trade_date=? AND slot=?
                  AND (
                    (owner_type='theme' AND EXISTS (
                        SELECT 1 FROM candidate_themes theme
                        WHERE theme.id=fund_flow_updates.owner_id
                          AND theme.status IN ('watching','confirmed')
                    ))
                    OR
                    (owner_type='test_pool' AND EXISTS (
                        SELECT 1 FROM test_pool_entries entry
                        WHERE entry.id=fund_flow_updates.owner_id
                          AND entry.status IN ('awaiting_buy','awaiting_exit')
                    ))
                  )
                """,
                (
                    status,
                    completed_at,
                    (error or "")[:500] or None,
                    report_json,
                    completed_at,
                    code,
                    trade_date,
                    slot,
                ),
            )

    def attach_theme_fund_flows(
        self,
        themes: list[dict[str, Any]],
        trade_date: str,
        slot: str,
    ) -> None:
        owner_ids = [int(item["id"]) for item in themes]
        rows = self._fund_flow_rows("theme", owner_ids, trade_date, slot)
        by_owner_code = {(int(row["owner_id"]), str(row["code"])): row for row in rows}
        for theme in themes:
            active_tracking = str(theme.get("status")) in {"watching", "confirmed"}
            members = [item for item in theme.get("members", []) if item.get("active", 1)]
            top_members = sorted(
                members,
                key=lambda item: int(item.get("leader_rank") or 9999),
            )[:5]
            reports: list[dict[str, Any]] = []
            statuses: list[str] = []
            for rank, member in enumerate(top_members, 1):
                row = by_owner_code.get((int(theme["id"]), str(member["code"])))
                view = (
                    _fund_flow_view(row)
                    if active_tracking and row
                    else _pending_fund_flow_view(trade_date, slot)
                    if active_tracking
                    else {"status": "stopped", "trade_date": trade_date, "slot": slot}
                )
                view["priority_rank"] = rank
                statuses.append(str(view["status"]))
                if view.get("_report"):
                    reports.append(view.pop("_report"))
                member["fund_flow"] = view
            if not active_tracking:
                status = "stopped"
            elif any(value == "running" for value in statuses):
                status = "running"
            elif statuses and all(value == "completed" for value in statuses):
                status = "completed"
            else:
                status = "pending"
            theme["fund_flow"] = {
                "status": status,
                "trade_date": trade_date,
                "slot": slot,
                "target_count": len(top_members),
                "completed_count": sum(value == "completed" for value in statuses),
                "failed_count": sum(value == "failed" for value in statuses),
                "summary": _aggregate_fund_flow_reports(reports),
            }

    def attach_test_pool_fund_flows(
        self,
        entries: list[dict[str, Any]],
        trade_date: str,
        slot: str,
    ) -> None:
        owner_ids = [int(item["id"]) for item in entries]
        rows = self._fund_flow_rows("test_pool", owner_ids, trade_date, slot)
        by_owner = {int(row["owner_id"]): row for row in rows}
        for entry in entries:
            active_tracking = str(entry.get("status")) in {"awaiting_buy", "awaiting_exit"}
            row = by_owner.get(int(entry["id"]))
            if active_tracking and row:
                view = _fund_flow_view(row)
                view.pop("_report", None)
                entry["fund_flow"] = view
            elif active_tracking:
                entry["fund_flow"] = _pending_fund_flow_view(trade_date, slot)
            else:
                entry["fund_flow"] = {
                    "status": "stopped",
                    "trade_date": trade_date,
                    "slot": slot,
                }

    def _fund_flow_rows(
        self,
        owner_type: str,
        owner_ids: Sequence[int],
        trade_date: str,
        slot: str,
    ) -> list[dict[str, Any]]:
        if not owner_ids:
            return []
        result: list[dict[str, Any]] = []
        with self.connect() as connection:
            for start in range(0, len(owner_ids), 800):
                chunk = owner_ids[start : start + 800]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT * FROM fund_flow_updates
                    WHERE owner_type=? AND trade_date=? AND slot=?
                      AND owner_id IN ({placeholders})
                    """,
                    [owner_type, trade_date, slot, *chunk],
                ).fetchall()
                result.extend(dict(row) for row in rows)
        return result

    def add_test_pool_entry(
        self,
        *,
        code: str,
        name: str,
        signal_trade_date: str,
        planned_buy_date: str | None,
        planned_exit_date: str | None,
        source_theme: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        compact = signal_trade_date.replace("-", "")
        created_at = utc_now_iso()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM test_pool_entries WHERE code=? AND signal_trade_date=?",
                (code, compact),
            ).fetchone()
            if existing:
                sources = json.loads(existing["source_themes_json"] or "[]")
                by_id = {int(item["id"]): item for item in sources if item.get("id") is not None}
                by_id[int(source_theme["id"])] = source_theme
                connection.execute(
                    """
                    UPDATE test_pool_entries SET name=?, source_themes_json=?,
                        planned_buy_date=COALESCE(planned_buy_date, ?),
                        planned_exit_date=COALESCE(planned_exit_date, ?),
                        exit_attempt_date=COALESCE(exit_attempt_date, ?)
                    WHERE id=?
                    """,
                    (
                        name,
                        json.dumps(list(by_id.values()), ensure_ascii=False),
                        planned_buy_date,
                        planned_exit_date,
                        planned_exit_date,
                        existing["id"],
                    ),
                )
                entry_id = int(existing["id"])
                created = False
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO test_pool_entries(
                        code, name, signal_trade_date, created_at, source_themes_json,
                        planned_buy_date, planned_exit_date, exit_attempt_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        name,
                        compact,
                        created_at,
                        json.dumps([source_theme], ensure_ascii=False),
                        planned_buy_date,
                        planned_exit_date,
                        planned_exit_date,
                    ),
                )
                entry_id = int(cursor.lastrowid)
                created = True
            row = connection.execute(
                "SELECT * FROM test_pool_entries WHERE id=?", (entry_id,)
            ).fetchone()
        return _decode_test_pool_entry(row), created

    def list_test_pool_entries(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM test_pool_entries ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_decode_test_pool_entry(row) for row in rows]

    def update_test_pool_schedule(
        self, entry_id: int, planned_buy_date: str, planned_exit_date: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE test_pool_entries SET planned_buy_date=?, planned_exit_date=?
                    , exit_attempt_date=COALESCE(exit_attempt_date, ?)
                WHERE id=? AND (planned_buy_date IS NULL OR planned_exit_date IS NULL)
                """,
                (planned_buy_date, planned_exit_date, planned_exit_date, entry_id),
            )

    def update_test_pool_entry(self, entry_id: int, **values: Any) -> None:
        allowed = {
            "buy_open",
            "buy_confirmed_at",
            "buy_confirmation_source",
            "current_price",
            "current_return_pct",
            "current_high",
            "current_high_return_pct",
            "live_updated_at",
            "exit_open",
            "exit_high",
            "exit_attempt_date",
            "actual_exit_date",
            "exit_delay_trade_days",
            "standard_return_pct",
            "maximum_return_pct",
            "status",
            "status_reason",
            "settled_at",
        }
        fields = [(name, value) for name, value in values.items() if name in allowed]
        if not fields:
            return
        assignments = ", ".join(f"{name}=?" for name, _ in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE test_pool_entries SET {assignments} WHERE id=?",
                [*(value for _, value in fields), entry_id],
            )
            status = next((value for name, value in fields if name == "status"), None)
            if status not in {None, "awaiting_buy", "awaiting_exit"}:
                connection.execute(
                    """
                    UPDATE fund_flow_updates SET status='stopped', updated_at=?
                    WHERE owner_type='test_pool' AND owner_id=?
                      AND status IN ('pending','running','failed')
                    """,
                    (utc_now_iso(), entry_id),
                )

    def pending_test_pool_price_dates(self, ready_through: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date FROM (
                    SELECT planned_buy_date AS trade_date FROM test_pool_entries
                    WHERE status='awaiting_buy'
                    UNION
                    SELECT COALESCE(exit_attempt_date, planned_exit_date) AS trade_date
                    FROM test_pool_entries
                    WHERE status IN ('awaiting_exit', 'awaiting_settlement')
                )
                WHERE trade_date IS NOT NULL AND trade_date<=?
                ORDER BY trade_date
                """,
                (ready_through,),
            ).fetchall()
        return [str(row["trade_date"]) for row in rows]

    def test_pool_summary(self) -> dict[str, Any]:
        entries = self.list_test_pool_entries()
        completed = [item for item in entries if item["status"] in {"success", "failure", "flat"}]
        success_count = sum(item["status"] == "success" for item in completed)
        failure_count = sum(item["status"] == "failure" for item in completed)
        flat_count = sum(item["status"] == "flat" for item in completed)
        directional_count = success_count + failure_count
        standard = [float(item["standard_return_pct"]) for item in completed]
        maximum = [float(item["maximum_return_pct"]) for item in completed]
        return {
            "total_count": len(entries),
            "completed_count": len(completed),
            "success_count": success_count,
            "failure_count": failure_count,
            "flat_count": flat_count,
            "unfilled_count": sum(item["status"] == "unfilled" for item in entries),
            "invalid_count": sum(item["status"] == "invalid" for item in entries),
            "pending_count": sum(
                item["status"] in {"awaiting_buy", "awaiting_exit", "awaiting_settlement"}
                for item in entries
            ),
            "success_rate": round(success_count / directional_count * 100.0, 2)
            if directional_count
            else None,
            "average_standard_return_pct": round(sum(standard) / len(standard), 3)
            if standard
            else None,
            "average_maximum_return_pct": round(sum(maximum) / len(maximum), 3)
            if maximum
            else None,
            "flat_policy": "excluded_from_success_rate",
            "aggregation": "equal_weight_average",
        }

    def begin_run(self, job_name: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO service_runs(job_name, started_at, status) VALUES (?, ?, 'running')",
                (job_name, utc_now_iso()),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, row_count: int = 0, detail: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE service_runs SET finished_at=?, status=?, row_count=?, detail=? WHERE id=?
                """,
                (utc_now_iso(), status, row_count, detail[:2000], run_id),
            )

    def latest_run(self, job_name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM service_runs WHERE job_name=? ORDER BY started_at DESC LIMIT 1",
                (job_name,),
            ).fetchone()
        return dict(row) if row else None

    def archive_quotes_before(self, cutoff: date) -> list[Path]:
        """Archive old raw snapshots as gzipped JSONL before deleting them from SQLite."""
        with self.connect() as connection:
            dates = connection.execute(
                """
                SELECT DISTINCT trade_date FROM quote_snapshots
                WHERE trade_date < ? ORDER BY trade_date
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        archived: list[Path] = []
        for date_row in dates:
            trade_date = str(date_row["trade_date"])
            destination = self.archive_dir / f"quotes-{trade_date}.jsonl.gz"
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM quote_snapshots WHERE trade_date=? ORDER BY captured_at, code",
                    (trade_date,),
                ).fetchall()
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            temporary.replace(destination)
            with self.connect() as connection:
                connection.execute("DELETE FROM quote_snapshots WHERE trade_date=?", (trade_date,))
            archived.append(destination)
        return archived

    def backup(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.archive_dir / f"stocktopic-backup-{stamp}.sqlite3"
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        return destination

    def prune_backups(self, keep: int = 14) -> None:
        backups = sorted(self.archive_dir.glob("stocktopic-backup-*.sqlite3"), reverse=True)
        for path in backups[keep:]:
            path.unlink(missing_ok=True)

    def integrity_check(self) -> str:
        with self.connect() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return str(row[0])


def _decode_anomaly_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        item = dict(row)
        for source, target in (
            ("event_types_json", "event_types"),
            ("reasons_json", "reasons"),
            ("metrics_json", "metrics"),
        ):
            item[target] = json.loads(item.pop(source))
        result.append(item)
    return result


def _decode_test_pool_entry(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["source_themes"] = json.loads(item.pop("source_themes_json") or "[]")
    return item


def _pending_fund_flow_view(trade_date: str, slot: str) -> dict[str, Any]:
    return {
        "status": "pending",
        "trade_date": trade_date,
        "slot": slot,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "summary": None,
    }


def _fund_flow_view(row: dict[str, Any]) -> dict[str, Any]:
    report = json.loads(row.get("report_json") or "null")
    return {
        "status": str(row.get("status") or "pending"),
        "trade_date": str(row.get("trade_date") or ""),
        "slot": str(row.get("slot") or ""),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "error": row.get("error"),
        "summary": _fund_flow_report_summary(report) if report else None,
        "_report": report,
    }


def _fund_flow_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    tiers = {str(item.get("label")): item for item in report.get("thresholds", [])}
    large = tiers.get("50W+", {})
    super_large = tiers.get("100W+", {})
    coverage = report.get("coverage", {})
    return {
        "large_buy_ratio_pct": large.get("buy_ratio_pct"),
        "large_net_inflow": float(large.get("net_inflow") or 0),
        "super_buy_ratio_pct": super_large.get("buy_ratio_pct"),
        "super_net_inflow": float(super_large.get("net_inflow") or 0),
        "directional_coverage_pct": coverage.get("directional_amount_coverage_pct"),
        "order_id_coverage_pct": coverage.get("order_id_amount_coverage_pct"),
        "generated_at": report.get("generated_at"),
        "partial": bool(report.get("partial")),
    }


def _aggregate_fund_flow_reports(reports: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if not reports:
        return None
    buckets = {
        "50W+": {"buy": 0.0, "sell": 0.0},
        "100W+": {"buy": 0.0, "sell": 0.0},
    }
    for report in reports:
        for item in report.get("thresholds", []):
            label = str(item.get("label") or "")
            if label not in buckets:
                continue
            buckets[label]["buy"] += float(item.get("buy_amount") or 0)
            buckets[label]["sell"] += float(item.get("sell_amount") or 0)

    def summary(label: str) -> dict[str, Any]:
        buy = buckets[label]["buy"]
        sell = buckets[label]["sell"]
        total = buy + sell
        return {
            "buy_ratio_pct": round(buy / total * 100, 2) if total else None,
            "net_inflow": round(buy - sell, 2),
        }

    return {
        "large": summary("50W+"),
        "super_large": summary("100W+"),
        "report_count": len(reports),
    }


def _split_tags(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",").replace("/", ",")
    return list(dict.fromkeys(part.strip() for part in normalized.split(",") if part.strip()))


def _unique_tags(tags: Sequence[dict[str, Any]], tag_type: str) -> list[str]:
    ordered = sorted(
        (tag for tag in tags if str(tag.get("tag_type")) == tag_type),
        key=lambda item: float(item.get("confidence") or 0),
        reverse=True,
    )
    return list(dict.fromkeys(str(item["tag"]) for item in ordered if item.get("tag")))


def _high_confidence_theme_tags(tags: Sequence[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(item["tag"])
            for item in sorted(
                tags,
                key=lambda value: float(value.get("confidence") or 0),
                reverse=True,
            )
            if str(item.get("tag_type") or "") == "theme"
            and float(item.get("confidence") or 0) >= 0.9
            and item.get("tag")
        )
    )[:12]


def _is_chinext_code(code: str) -> bool:
    return bool(re.match(r"^(300|301)\d{3}\.SZ$", str(code)))


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _board_height(value: Any) -> int:
    text = str(value or "")
    if "首板" in text:
        return 1
    match = re.search(r"(\d+)\s*连板", text)
    return int(match.group(1)) if match else 0


def _clock_sort(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits.zfill(6) if digits else "999999"


def _parse_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _one_board_event_per_day(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for item in history:
        trade_date = str(item.get("trade_date") or "")
        existing = by_date.get(trade_date)
        if not existing or _board_signal_rank(item.get("board_tag")) < _board_signal_rank(
            existing.get("board_tag")
        ):
            by_date[trade_date] = item
    return list(by_date.values())


def _board_signal_rank(value: Any) -> int:
    return {"涨停": 0, "创业板涨幅超10%": 1, "炸板": 2}.get(str(value), 9)
