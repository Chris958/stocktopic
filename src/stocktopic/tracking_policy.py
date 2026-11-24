"""Local lifecycle and notification policy; no model or external data access."""
from datetime import datetime, timedelta

from .market_clock import MarketClock

ARCHIVED_TRACKING_DAYS = 14
MINIMUM_FORMAL_LIMIT_UPS = 4
OPPORTUNITY_ALERT_CATEGORIES = frozenset({"formal_theme", "high_value_opportunity"})


def archive_tracking_active(theme: dict, now: datetime) -> bool:
    if theme.get("status") != "archived":
        return True
    try:
        created = MarketClock.normalize(datetime.fromisoformat(str(theme["discovered_at"])))
    except (KeyError, TypeError, ValueError):
        return False  # Unknown original creation time cannot extend automatic tracking.
    return MarketClock.normalize(now) - created <= timedelta(days=ARCHIVED_TRACKING_DAYS)
