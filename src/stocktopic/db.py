from __future__ import annotations

import gzip
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

    def upsert_stocks(self, rows: Sequence[dict[str, Any]]) -> int:
        now = utc_now_iso()
        values: list[tuple[Any, ...]] = []
        tag_values: list[tuple[Any, ...]] = []
        active_codes: set[str] = set()
        for row in rows:
            code = str(row.get("ts_code", ""))
            name = str(row.get("name", ""))
            market = str(row.get("market", ""))
            is_main = bool(
                re.match(r"^(600|601|603|605)\d{3}\.SH$", code)
                or re.match(r"^(000|001|002|003)\d{3}\.SZ$", code)
            )
            excluded = ""
            if not is_main:
                excluded = "not_main_board"
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
                "SELECT code, name, list_date, industry FROM stocks WHERE active=1"
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
        return {
            str(row["code"]): (row["upper_limit"], row["lower_limit"])
            for row in rows
        }

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

    def anomalies_for_trade_date(
        self, trade_date: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM anomaly_events WHERE trade_date=?
                ORDER BY captured_at DESC, severity DESC LIMIT ?
                """,
                (trade_date, limit),
            ).fetchall()
        return _decode_anomaly_rows(rows)
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

    def upsert_kpl_concept_members(self, rows: Sequence[dict[str, Any]]) -> int:
        now = utc_now_iso()
        active = self.active_stock_map()
        values = []
        for row in rows:
            code = str(row.get("con_code") or "")
            tag = str(row.get("name") or "").strip()
            if code in active and tag:
                values.append((code, tag, "theme", "tushare_kpl_concept_cons", 0.9, now))
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO stock_tags(code, tag, tag_type, source, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, tag, source) DO UPDATE SET
                    confidence=excluded.confidence, updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def kpl_events_for_codes(
        self, trade_date: str, codes: Sequence[str]
    ) -> list[dict[str, Any]]:
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

    def recent_live_theme_fingerprint(
        self, shared_tag: str, direction: str, since: datetime
    ) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint FROM candidate_themes
                WHERE shared_tag=? AND direction=? AND status IN ('pending','confirmed')
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
    ) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO candidate_themes(
                    fingerprint, provisional_name, shared_tag, direction,
                    discovered_at, day1_date, discovery_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    discovery_reason=excluded.discovery_reason,
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
                        "deterministic_tag_cluster",
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
        sql += " ORDER BY updated_at DESC"
        with self.connect() as connection:
            themes = [dict(row) for row in connection.execute(sql, params).fetchall()]
            for theme in themes:
                member_rows = connection.execute(
                    """
                    SELECT code, name, membership_source, evidence_json,
                           first_seen_at, last_seen_at, active, role
                    FROM theme_members WHERE theme_id=? ORDER BY active DESC, code
                    """,
                    (theme["id"],),
                ).fetchall()
                theme["members"] = []
                for row in member_rows:
                    member = dict(row)
                    member["evidence"] = json.loads(member.pop("evidence_json"))
                    theme["members"].append(member)
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
        return themes

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
                    final_name=COALESCE(?, final_name, suggested_name, provisional_name),
                    confirmed_at=COALESCE(?, confirmed_at),
                    catalyst_strength=COALESCE(?, catalyst_strength),
                    catalyst_duration=COALESCE(?, catalyst_duration),
                    updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    final_name,
                    confirmed_at,
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


def _split_tags(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",").replace("/", ",")
    return list(dict.fromkeys(part.strip() for part in normalized.split(",") if part.strip()))
