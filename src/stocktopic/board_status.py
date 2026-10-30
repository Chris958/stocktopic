"""Parse consecutive boards separately from non-consecutive N-day M-board runs."""

from __future__ import annotations

import re
from typing import Any


def parse_board_status(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    window = re.search(r"(\d+)天(\d+)板", text)
    if window:
        days, boards = map(int, window.groups())
        if 0 < boards <= days <= 250:
            return {
                "label": text,
                "window_days": days,
                "limit_count": boards,
                "consecutive": boards if days == boards else None,
            }
        return {"label": text, "consecutive": None}
    if "首板" in text:
        height = 1
    else:
        match = re.fullmatch(r"(\d+)(?:连板|板)?", text)
        height = int(match.group(1)) if match else 0
    return {"label": text, "consecutive": height if 0 < height <= 250 else None}


def consecutive_boards(value: Any) -> int:
    return int(parse_board_status(value).get("consecutive") or 0)
