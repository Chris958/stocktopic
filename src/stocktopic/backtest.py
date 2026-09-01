from __future__ import annotations

from datetime import datetime
from typing import Any

from .db import Database, utc_now_iso
from .domain import Quote

FINAL_STATUSES = {"success", "failure", "flat", "unfilled", "invalid"}


class PaperTradeTracker:
    """Deterministic T+1-open to T+2-open/high paper-trade tracker."""

    def __init__(self, database: Database):
        self.database = database

    def add(self, theme_id: int, code: str, now: datetime) -> tuple[dict[str, Any], bool]:
        theme = self.database.get_theme(theme_id)
        if not theme:
            raise KeyError("Theme not found")
        member = next(
            (
                item
                for item in theme.get("members", [])
                if str(item.get("code")) == code and item.get("active", 1)
            ),
            None,
        )
        if not member:
            raise ValueError("Stock is not an active member of this theme")
        compact = now.strftime("%Y%m%d")
        open_dates = self.database.open_trade_dates(compact, 1)
        if not open_dates:
            raise RuntimeError("Trading calendar has no signal date")
        signal_date = open_dates[0]
        next_dates = self.database.next_open_trade_dates(signal_date, 2)
        planned_buy = next_dates[0] if len(next_dates) >= 1 else None
        planned_exit = next_dates[1] if len(next_dates) >= 2 else None
        title = (
            theme.get("final_name")
            or theme.get("suggested_name")
            or theme.get("provisional_name")
            or "未命名题材"
        )
        source = {
            "id": int(theme["id"]),
            "name": str(title),
            "level": str(theme.get("theme_level") or theme.get("status") or "candidate"),
        }
        return self.database.add_test_pool_entry(
            code=code,
            name=str(member.get("name") or code),
            signal_trade_date=signal_date,
            planned_buy_date=planned_buy,
            planned_exit_date=planned_exit,
            source_theme=source,
        )

    def settle(self) -> dict[str, int]:
        counts = {"scheduled": 0, "bought": 0, "settled": 0, "unfilled": 0, "delayed": 0}
        for entry in self.database.list_test_pool_entries():
            if entry["status"] in FINAL_STATUSES:
                continue
            if not entry.get("planned_buy_date") or not entry.get("planned_exit_date"):
                next_dates = self.database.next_open_trade_dates(entry["signal_trade_date"], 2)
                if len(next_dates) < 2:
                    continue
                self.database.update_test_pool_schedule(int(entry["id"]), *next_dates)
                entry["planned_buy_date"], entry["planned_exit_date"] = next_dates
                entry["exit_attempt_date"] = next_dates[1]
                counts["scheduled"] += 1

            if entry["status"] == "awaiting_buy":
                buy_date = str(entry["planned_buy_date"])
                if not self._date_synced(buy_date):
                    continue
                buy_bar = self.database.daily_bar(buy_date, str(entry["code"]))
                reason = _buy_unfilled_reason(buy_bar)
                if reason:
                    self.database.update_test_pool_entry(
                        int(entry["id"]),
                        status="unfilled",
                        status_reason=reason,
                        settled_at=utc_now_iso(),
                    )
                    counts["unfilled"] += 1
                    continue
                buy_open = float(buy_bar["open"])
                self.database.update_test_pool_entry(
                    int(entry["id"]),
                    buy_open=buy_open,
                    buy_confirmed_at=utc_now_iso(),
                    buy_confirmation_source="official_daily",
                    status="awaiting_exit",
                    status_reason=None,
                )
                entry["buy_open"] = buy_open
                entry["status"] = "awaiting_exit"
                counts["bought"] += 1

            if (
                entry["status"] in {"awaiting_exit", "awaiting_settlement"}
                and entry.get("buy_confirmation_source") == "realtime_rt_k"
                and self._date_synced(str(entry["planned_buy_date"]))
            ):
                buy_bar = self.database.daily_bar(
                    str(entry["planned_buy_date"]), str(entry["code"])
                )
                reason = _buy_unfilled_reason(buy_bar)
                if reason:
                    self.database.update_test_pool_entry(
                        int(entry["id"]),
                        status="unfilled",
                        status_reason=reason,
                        buy_confirmation_source="official_daily",
                        settled_at=utc_now_iso(),
                    )
                    counts["unfilled"] += 1
                    continue
                official_open = float(buy_bar["open"])
                current_price = float(entry.get("current_price") or 0)
                current_high = float(entry.get("current_high") or 0)
                self.database.update_test_pool_entry(
                    int(entry["id"]),
                    buy_open=official_open,
                    buy_confirmation_source="official_daily",
                    current_return_pct=(
                        round((current_price / official_open - 1.0) * 100.0, 3)
                        if current_price > 0
                        else None
                    ),
                    current_high_return_pct=(
                        round((current_high / official_open - 1.0) * 100.0, 3)
                        if current_high > 0
                        else None
                    ),
                    status_reason=None,
                )
                entry["buy_open"] = official_open
                entry["buy_confirmation_source"] = "official_daily"

            if entry["status"] not in {"awaiting_exit", "awaiting_settlement"}:
                continue
            attempt_date = str(entry.get("exit_attempt_date") or entry["planned_exit_date"])
            if not self._date_synced(attempt_date):
                continue
            exit_bar = self.database.daily_bar(attempt_date, str(entry["code"]))
            if _cannot_sell_at_open(exit_bar):
                following = self.database.next_open_trade_dates(attempt_date, 1)
                if not following:
                    continue
                self.database.update_test_pool_entry(
                    int(entry["id"]),
                    exit_attempt_date=following[0],
                    actual_exit_date=None,
                    exit_open=None,
                    exit_high=None,
                    standard_return_pct=None,
                    maximum_return_pct=None,
                    status="awaiting_exit",
                    status_reason=f"{attempt_date}无法按开盘卖出，顺延至下一可交易日",
                )
                counts["delayed"] += 1
                continue
            buy_open = float(entry["buy_open"])
            exit_open = float(exit_bar["open"])
            exit_high = float(exit_bar["high"])
            standard_return = round((exit_open / buy_open - 1.0) * 100.0, 3)
            maximum_return = round((exit_high / buy_open - 1.0) * 100.0, 3)
            status = "success" if exit_open > buy_open else "failure"
            if abs(exit_open - buy_open) < 0.000001:
                status = "flat"
            delay_days = max(
                0,
                self.database.count_open_days(str(entry["planned_exit_date"]), attempt_date) - 1,
            )
            self.database.update_test_pool_entry(
                int(entry["id"]),
                exit_open=exit_open,
                exit_high=exit_high,
                standard_return_pct=standard_return,
                maximum_return_pct=maximum_return,
                status=status,
                status_reason=(f"延迟{delay_days}个交易日退出" if delay_days else None),
                actual_exit_date=attempt_date,
                exit_delay_trade_days=delay_days,
                settled_at=utc_now_iso(),
            )
            counts["settled"] += 1
        return counts

    def update_realtime(
        self,
        quotes: list[Quote],
        trade_date: str,
        upper_limits: dict[str, float | None] | None = None,
        lower_limits: dict[str, float | None] | None = None,
    ) -> dict[str, int]:
        """Confirm T+1 buys, T+2 exits and live returns from an rt_k snapshot."""
        compact = trade_date.replace("-", "")
        quote_map = {quote.code: quote for quote in quotes}
        upper_limits = upper_limits or {}
        lower_limits = lower_limits or {}
        counts = {
            "bought": 0,
            "marked": 0,
            "limit_pending": 0,
            "sold": 0,
            "exit_pending": 0,
            "exit_marked": 0,
        }
        for entry in self.database.list_test_pool_entries():
            if entry["status"] not in {
                "awaiting_buy",
                "awaiting_exit",
                "awaiting_settlement",
            }:
                continue
            quote = quote_map.get(str(entry["code"]))
            if not quote or quote.open <= 0 or quote.close <= 0:
                continue

            if entry["status"] == "awaiting_buy":
                if str(entry.get("planned_buy_date") or "") != compact:
                    continue
                if quote.volume <= 0 or quote.trades <= 0:
                    continue
                upper = upper_limits.get(quote.code)
                opens_at_known_limit = bool(
                    upper and quote.open >= float(upper) - max(0.005, float(upper) * 0.0001)
                )
                opens_near_ten_percent = (
                    quote.pre_close > 0
                    and (quote.open / quote.pre_close - 1.0) * 100.0 >= 9.5
                )
                opens_at_limit = opens_at_known_limit or opens_near_ten_percent
                still_one_price = max(quote.open, quote.high, quote.low, quote.close) - min(
                    quote.open, quote.high, quote.low, quote.close
                ) <= 0.005
                if opens_at_limit and still_one_price:
                    self.database.update_test_pool_entry(
                        int(entry["id"]),
                        status_reason="开盘涨停且尚未打开，等待成交确认",
                        current_price=quote.close,
                        current_high=quote.high,
                        live_updated_at=quote.captured_at.isoformat(timespec="seconds"),
                    )
                    counts["limit_pending"] += 1
                    continue
                self.database.update_test_pool_entry(
                    int(entry["id"]),
                    buy_open=quote.open,
                    buy_confirmed_at=quote.captured_at.isoformat(timespec="seconds"),
                    buy_confirmation_source="realtime_rt_k",
                    status="awaiting_exit",
                    status_reason="rt_k盘中确认，收盘后由正式日线校准",
                )
                entry["buy_open"] = quote.open
                entry["status"] = "awaiting_exit"
                counts["bought"] += 1

            buy_open = float(entry.get("buy_open") or 0)
            if buy_open <= 0:
                continue

            attempt_date = str(entry.get("exit_attempt_date") or entry.get("planned_exit_date"))
            if entry["status"] == "awaiting_exit" and attempt_date == compact:
                if quote.volume <= 0 or quote.trades <= 0:
                    self.database.update_test_pool_entry(
                        int(entry["id"]),
                        status_reason="等待T+2正式开盘行情确认卖出",
                        live_updated_at=quote.captured_at.isoformat(timespec="seconds"),
                    )
                    continue
                lower = lower_limits.get(quote.code)
                opens_at_known_limit = bool(
                    lower and quote.open <= float(lower) + max(0.005, float(lower) * 0.0001)
                )
                opens_near_ten_percent = (
                    quote.pre_close > 0
                    and (quote.open / quote.pre_close - 1.0) * 100.0 <= -9.5
                )
                opens_at_limit = opens_at_known_limit or opens_near_ten_percent
                still_one_price = max(quote.open, quote.high, quote.low, quote.close) - min(
                    quote.open, quote.high, quote.low, quote.close
                ) <= 0.005
                if opens_at_limit and still_one_price:
                    self.database.update_test_pool_entry(
                        int(entry["id"]),
                        status_reason="开盘跌停且尚未打开，等待卖出确认",
                        current_price=quote.close,
                        current_high=max(float(entry.get("current_high") or 0), quote.high),
                        live_updated_at=quote.captured_at.isoformat(timespec="seconds"),
                    )
                    counts["exit_pending"] += 1
                    continue
                standard_return = round((quote.open / buy_open - 1.0) * 100.0, 3)
                maximum_return = round((quote.high / buy_open - 1.0) * 100.0, 3)
                self.database.update_test_pool_entry(
                    int(entry["id"]),
                    exit_open=quote.open,
                    exit_high=quote.high,
                    standard_return_pct=standard_return,
                    maximum_return_pct=maximum_return,
                    actual_exit_date=compact,
                    status="awaiting_settlement",
                    status_reason="rt_k盘中确认开盘卖出，收盘后由正式日线校准",
                    live_updated_at=quote.captured_at.isoformat(timespec="seconds"),
                )
                entry["status"] = "awaiting_settlement"
                entry["exit_high"] = quote.high
                counts["sold"] += 1

            if entry["status"] == "awaiting_settlement":
                if attempt_date != compact:
                    continue
                exit_high = max(float(entry.get("exit_high") or 0), quote.high)
                maximum_return = round((exit_high / buy_open - 1.0) * 100.0, 3)
                self.database.update_test_pool_entry(
                    int(entry["id"]),
                    exit_high=exit_high,
                    maximum_return_pct=maximum_return,
                    live_updated_at=quote.captured_at.isoformat(timespec="seconds"),
                )
                entry["exit_high"] = exit_high
                counts["exit_marked"] += 1
                continue

            if entry["status"] != "awaiting_exit":
                continue
            current_return = round((quote.close / buy_open - 1.0) * 100.0, 3)
            holding_high = max(float(entry.get("current_high") or 0), quote.high)
            high_return = round((holding_high / buy_open - 1.0) * 100.0, 3)
            self.database.update_test_pool_entry(
                int(entry["id"]),
                current_price=quote.close,
                current_return_pct=current_return,
                current_high=holding_high,
                current_high_return_pct=high_return,
                live_updated_at=quote.captured_at.isoformat(timespec="seconds"),
            )
            counts["marked"] += 1
        return counts

    def dashboard(self, now: datetime) -> dict[str, Any]:
        compact = now.strftime("%Y%m%d")
        dates = self.database.open_trade_dates(compact, 1)
        return {
            "current_signal_trade_date": dates[0] if dates else None,
            "entries": self.database.list_test_pool_entries(),
            "summary": self.database.test_pool_summary(),
            "rules": {
                "buy": "信号日后首个交易日开盘买入",
                "standard_exit": "再下一交易日开盘卖出",
                "maximum_exit": "再下一交易日最高价卖出（理论上限）",
                "unfilled": "买入日一字涨停、停牌或无有效开盘价不计样本",
                "delayed_exit": "退出日一字跌停、停牌或无有效开盘价顺延",
                "flat": "持平单列，不计入成功率分母",
                "aggregation": "每笔等金额，收益使用等权平均",
            },
        }

    def _date_synced(self, trade_date: str) -> bool:
        return self.database.get_metadata(f"daily_prices_synced:{trade_date}") == "true"


def _buy_unfilled_reason(bar: dict[str, Any] | None) -> str | None:
    if not bar:
        return "买入日停牌或无日线数据，未成交"
    if float(bar.get("open") or 0) <= 0:
        return "买入日没有有效开盘价，未成交"
    if _one_price_limit(bar, positive=True):
        return "买入日一字涨停，按不可成交处理"
    return None


def _cannot_sell_at_open(bar: dict[str, Any] | None) -> bool:
    if not bar:
        return True
    if float(bar.get("open") or 0) <= 0 or float(bar.get("high") or 0) <= 0:
        return True
    return _one_price_limit(bar, positive=False)


def _one_price_limit(bar: dict[str, Any], *, positive: bool) -> bool:
    values = [float(bar.get(field) or 0) for field in ("open", "high", "low", "close")]
    if min(values) <= 0 or max(values) - min(values) > 0.005:
        return False
    pct = float(bar.get("pct_change") or 0)
    return pct >= 9.5 if positive else pct <= -9.5
