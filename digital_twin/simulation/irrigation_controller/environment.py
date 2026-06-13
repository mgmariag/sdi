from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.soil_model import local_observed_at, number, season


def rain_exposure_factor(pot: dict[str, Any], day: date) -> float:
    if not is_outdoor(pot, day):
        return 0.0
    rain_exposure = str(pot.get("rain_exposure") or "")
    if rain_exposure:
        return {
            "covered": 0.0,
            "partially_exposed": 0.5,
            "fully_exposed": 1.0,
        }.get(rain_exposure, 0.5)

    zone = str(pot.get("balcony_zone") or "")
    if zone in {"north_shelter"}:
        return 0.0
    if zone in {"west_wall", "east_corner"}:
        return 0.5
    if zone in {"south_rail", "hanging_row"}:
        return 1.0
    if str(pot.get("sun_exposure") or "") in {"full", "reflected_heat"}:
        return 1.0
    return 0.5

def threshold_for_pot(pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
    if slot == "winter_check":
        return 10.0
    threshold = pot["moisture_min_pct"]
    if day_profile["heatwave_day"] and pot["plant_type_code"] in {"vegetables", "herbs"}:
        threshold += 4.0
    if day_profile["dry_windy_day"] and pot["size_class"] == "small":
        threshold += 3.0
    if slot == "evening":
        threshold = max(8.0, threshold - 3.0)
    return threshold

def is_emergency_dryness(state: PotState, pot: dict[str, Any], day: date, observed_at: datetime) -> bool:
    if season(day) == "summer" and 11 <= observed_at.hour <= 16:
        return state.moisture < max(8.0, pot["moisture_min_pct"] - 8.0)
    return False

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

def is_outdoor(pot: dict[str, Any], day: date) -> bool:
    return True

def _cold_month(day: date) -> bool:
    return day.month in {11, 12, 1, 2, 3}

def upcoming_freeze(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 3) -> bool:
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day + timedelta(days=offset), [])
        if rows and min(number(row["temperature_c"], 20.0) for row in rows) <= 0:
            return True
    return False

def precipitation_last_days(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 10) -> float:
    total = 0.0
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day - timedelta(days=offset), [])
        total += sum(number(row["precipitation_mm"], 0.0) for row in rows)
    return total
