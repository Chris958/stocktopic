from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from stocktopic.market_environment import build_market_environment


class FakeDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE kpl_events (
                trade_date TEXT,
                code TEXT,
                board_tag TEXT,
                status TEXT
            );
            CREATE TABLE quote_snapshots (
                trade_date TEXT,
                captured_at TEXT,
                code TEXT,
                pct_change REAL,
                high REAL,
                low REAL,
                close REAL,
                pre_close REAL
            );
            CREATE TABLE stock_daily_bars (
                trade_date TEXT,
                code TEXT,
                pct_change REAL,
                high REAL,
                low REAL,
                close REAL,
                pre_close REAL
            );
            CREATE TABLE daily_limits (
                trade_date TEXT,
                code TEXT,
                upper_limit REAL,
                lower_limit REAL
            );
            """
        )

    @contextmanager
    def connect(self):
        yield self.connection
        self.connection.commit()

    def previous_trade_date(self, trade_date: str):
        assert trade_date == "20260903"
        return "20260902"


def test_market_environment_v2_uses_real_cross_day_outcomes():
    db = FakeDatabase()
    db.connection.executemany(
        "INSERT INTO kpl_events VALUES (?, ?, ?, ?)",
        [
            ("20260902", "000001.SZ", "涨停", "首板"),
            ("20260902", "000002.SZ", "涨停", "2连板"),
            ("20260902", "000003.SZ", "涨停", "2连板"),
            ("20260903", "000001.SZ", "涨停", "2连板"),
            ("20260903", "000002.SZ", "炸板", ""),
            ("20260903", "000003.SZ", "涨停", "3连板"),
            ("20260903", "000004.SZ", "跌停", ""),
        ],
    )
    db.connection.executemany(
        "INSERT INTO quote_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("20260903", "2026-09-03T10:00:00+08:00", "000001.SZ", 10, 11, 10, 11, 10),
            ("20260903", "2026-09-03T10:00:00+08:00", "000002.SZ", -8, 21, 18.4, 18.4, 20),
            ("20260903", "2026-09-03T10:00:00+08:00", "000003.SZ", 10, 33, 30, 33, 30),
            ("20260903", "2026-09-03T10:00:00+08:00", "000005.SZ", 0, 11, 9, 10, 10),
        ],
    )
    db.connection.executemany(
        "INSERT INTO daily_limits VALUES (?, ?, ?, ?)",
        [
            ("20260903", "000001.SZ", 11, 9),
            ("20260903", "000002.SZ", 22, 18),
            ("20260903", "000003.SZ", 33, 27),
            ("20260903", "000005.SZ", 11, 9),
        ],
    )

    result = build_market_environment(db, "20260903")

    assert result["version"] == "v2-real-market-breadth"
    assert result["limit_up_count"] == 2
    assert result["failed_rate"] == pytest.approx(33.33, abs=0.01)
    assert result["max_board_height"] == 3
    assert result["promotion_rate"] == pytest.approx(66.67, abs=0.01)
    assert result["yesterday_limit_return"] == pytest.approx(4.0)
    assert result["board_trade_return"] == pytest.approx(10.0)
    assert result["board_trade_win_rate"] == pytest.approx(66.67, abs=0.01)
    assert result["nuclear_button_count"] == 1
    assert result["nuclear_button_ratio"] == pytest.approx(33.33, abs=0.01)
    assert result["break_board_count"] == 1
    assert result["break_board_return"] == pytest.approx(-8.0)
    assert result["earth_sky_count"] == 1
