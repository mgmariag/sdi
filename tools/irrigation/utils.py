from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tools.irrigation.models import LOCAL_TZ


def season(day: date) -> str:
    if day.month in {12, 1, 2}:
        return "winter"
    if day.month in {3, 4, 5}:
        return "spring"
    if day.month in {6, 7, 8}:
        return "summer"
    return "autumn"


def sun_factor(pot: dict[str, Any]) -> float:
    return {
        "shade": 0.75,
        "partial": 1.0,
        "full": 1.24,
        "reflected_heat": 1.42,
    }[pot["sun_exposure"]]


def wind_factor(pot: dict[str, Any]) -> float:
    return {
        "sheltered": 0.86,
        "moderate": 1.0,
        "gusty": 1.22,
    }[pot["wind_exposure"]]


def number(value, default: float | None = 0.0) -> float | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def local_observed_at(weather: dict[str, Any]) -> datetime:
    observed_at = weather["observed_at"]
    if observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=LOCAL_TZ)
    return observed_at.astimezone(LOCAL_TZ)
