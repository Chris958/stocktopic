"""Calendar-age boundary, timezone normalization and fail-closed archive dates."""
from datetime import UTC, datetime, timedelta

import pytest

from stocktopic.market_clock import SHANGHAI
from stocktopic.tracking_policy import archive_tracking_active


@pytest.mark.parametrize("age,active", [
    (timedelta(days=13, hours=23), True),
    (timedelta(days=14), True),
    (timedelta(days=14, seconds=1), False),
])
def test_archive_age_boundary_and_timezone(age, active):
    created = datetime(2026, 10, 16, 10, tzinfo=SHANGHAI)
    theme = {"status": "archived", "discovered_at": created.astimezone(UTC).isoformat()}
    assert archive_tracking_active(theme, created + age) is active


@pytest.mark.parametrize("date", [None, "bad-date", ""])
def test_unknown_archive_creation_time_cannot_extend_tracking(date):
    assert not archive_tracking_active(
        {"status": "archived", "discovered_at": date}, datetime.now(SHANGHAI),
    )


def test_only_archived_pools_expire():
    for status in ("confirmed", "watching", "pending"):
        assert archive_tracking_active(
            {"status": status, "discovered_at": "2000-01-01"}, datetime.now(SHANGHAI),
        )
