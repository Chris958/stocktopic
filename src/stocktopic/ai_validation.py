"""Validate confidence units without silently changing admission thresholds."""

from __future__ import annotations

import math


class ConfidenceScaleError(RuntimeError):
    pass


def confidence_scores(payload: dict, fields: tuple[str, ...]) -> dict[str, float]:
    values = {}
    for name in fields:
        raw = payload.get(name, 0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ConfidenceScaleError(f"{name}: invalid confidence") from None
        if isinstance(raw, bool) or not math.isfinite(value):
            raise ConfidenceScaleError(f"{name}: invalid confidence")
        values[name] = value
    scale = payload.get("confidence_scale")
    if scale not in (None, "0_100", "0_1"):
        raise ConfidenceScaleError("confidence_scale must be 0_100 or 0_1")
    if scale is None and any(0 < value < 1 for value in values.values()):
        raise ConfidenceScaleError("confidence_scale missing for fractional confidence")
    upper = 1 if scale == "0_1" else 100
    if any(value < 0 or value > upper for value in values.values()):
        raise ConfidenceScaleError("confidence outside declared scale")
    return {name: value * (100 if scale == "0_1" else 1) for name, value in values.items()}
