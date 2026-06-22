from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at


def alert_row(pot: dict[str, Any], weather: dict[str, Any], alert_type: str, severity: str, title: str) -> dict[str, Any]:
    observed_local = local_observed_at(weather)
    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "raised_at": observed_local.isoformat(),
        "alert_type": alert_type,
        "severity": severity,
        "title": title,
        "detail": f"{pot['pot_code']} at {observed_local.isoformat()}",
    }


def upcoming_freeze(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 3) -> bool:
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day + timedelta(days=offset), [])
        if rows and min(soil.number(row["temperature_c"], 20.0) for row in rows) <= 0:
            return True
    return False


def precipitation_last_days(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 10) -> float:
    total = 0.0
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day - timedelta(days=offset), [])
        total += sum(soil.number(row["precipitation_mm"], 0.0) for row in rows)
    return total
