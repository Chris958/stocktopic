from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class MarketState:
    now: datetime
    is_open_day: bool | None
    in_realtime_window: bool
    session: str
    slot: str | None
    reason: str


class MarketClock:
    """Fail-closed A-share clock: unknown calendar state never starts quote collection."""

    MORNING_AUCTION = (time(9, 15), time(9, 25))
    MORNING = (time(9, 30), time(11, 30))
    AFTERNOON = (time(13, 0), time(15, 0))

    @staticmethod
    def china_now() -> datetime:
        return datetime.now(SHANGHAI)

    @staticmethod
    def normalize(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=SHANGHAI)
        return value.astimezone(SHANGHAI)

    @classmethod
    def session_name(cls, value: datetime) -> str:
        current = cls.normalize(value).time().replace(tzinfo=None)
        if cls.MORNING_AUCTION[0] <= current <= cls.MORNING_AUCTION[1]:
            return "opening_auction"
        if cls.MORNING[0] <= current <= cls.MORNING[1]:
            return "morning"
        if cls.AFTERNOON[0] <= current <= cls.AFTERNOON[1]:
            return "afternoon"
        if time(9, 25) < current < time(9, 30):
            return "auction_gap"
        if time(11, 30) < current < time(13, 0):
            return "lunch_break"
        if current < time(9, 15):
            return "pre_market"
        return "closed"

    @classmethod
    def is_realtime_window(cls, value: datetime) -> bool:
        return cls.session_name(value) in {"opening_auction", "morning", "afternoon"}

    @classmethod
    def five_minute_slot(cls, value: datetime) -> str | None:
        local = cls.normalize(value)
        if not cls.is_realtime_window(local):
            return None
        minute = local.minute - (local.minute % 5)
        slot_time = local.replace(minute=minute, second=0, microsecond=0)
        # Do not manufacture 09:25-09:30 or lunch-break slots by flooring.
        if not cls.is_realtime_window(slot_time):
            return None
        return slot_time.strftime("%H:%M")

    @classmethod
    def state(cls, value: datetime, calendar_open: bool | None) -> MarketState:
        local = cls.normalize(value)
        session = cls.session_name(local)
        in_window = cls.is_realtime_window(local)
        if calendar_open is None:
            return MarketState(local, None, False, session, None, "calendar_unknown_fail_closed")
        if not calendar_open:
            return MarketState(local, False, False, session, None, "exchange_closed")
        if not in_window:
            return MarketState(local, True, False, session, None, session)
        return MarketState(
            local,
            True,
            True,
            session,
            cls.five_minute_slot(local),
            "realtime_window",
        )

    @staticmethod
    def compact_date(value: date | datetime) -> str:
        if isinstance(value, datetime):
            value = value.date()
        return value.strftime("%Y%m%d")
