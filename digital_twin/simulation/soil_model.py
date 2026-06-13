from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from digital_twin.simulation.shared.constants import LOCAL_TZ


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


def hourly_reference_et_mm(weather: dict[str, Any]) -> float:
    evap_mm = number(weather.get("evapotranspiration_mm"), None)
    if evap_mm is not None:
        return max(0.0, evap_mm)

    temp = number(weather.get("temperature_c"), 20.0)
    humidity = number(weather.get("relative_humidity_pct"), 60.0)
    wind = number(weather.get("wind_speed_kmh"), 5.0)
    return max(0.01, 0.025 + (temp / 38.0) * ((100.0 - humidity) / 100.0) * (1.0 + wind / 45.0))


def low_retention_drydown_multiplier(pot: dict[str, Any]) -> float:
    retention = clamp(number(pot.get("retention_factor"), 1.0), 0.1, 2.0)
    return 1.0 + max(0.0, 1.0 - retention) * 0.65


def indoor_hourly_moisture_loss(pot: dict[str, Any], local_day: date) -> float:
    if local_day.month in {11, 12, 1, 2, 3}:
        return 0.003 if pot["plant_type_code"] != "succulents" else 0.001
    return 0.018 if pot["plant_type_code"] != "succulents" else 0.006


def minimum_realistic_moisture(pot: dict[str, Any], local_day: date) -> float:
    if local_day.month in {11, 12, 1, 2, 3}:
        return max(8.0, float(pot["winter_moisture_target_pct"]) - 6.0)
    return max(7.0, float(pot["moisture_min_pct"]) - 8.0)


def apply_hourly_environment_moisture(
    moisture: float,
    pot: dict[str, Any],
    weather: dict[str, Any],
    local_day: date,
    hours: float = 1.0,
    rain_exposure_factor: float = 1.0,
    outdoor: bool = True,
) -> float:
    hours = max(0.0, float(hours))
    if outdoor:
        loss = (
            hourly_reference_et_mm(weather)
            * _pot_sun_factor(pot)
            * _pot_wind_factor(pot)
            * float(pot["evaporation_factor"])
            * hours
        )
        if pot["plant_type_code"] in {"vegetables", "herbs"}:
            loss *= 1.12
        elif pot["plant_type_code"] == "succulents":
            loss *= 0.48
        loss *= low_retention_drydown_multiplier(pot)

        effective_rain_mm = number(weather.get("precipitation_mm"), 0.0) * clamp(rain_exposure_factor, 0.0, 1.0)
        rain_gain = min(8.0, effective_rain_mm * 0.85 * hours)
        moisture += rain_gain - loss
    else:
        moisture -= indoor_hourly_moisture_loss(pot, local_day) * hours

    return clamp(moisture, minimum_realistic_moisture(pot, local_day), 100.0)


def _pot_sun_factor(pot: dict[str, Any]) -> float:
    if "_sun_factor" in pot:
        return float(pot["_sun_factor"])
    return sun_factor(pot)


def _pot_wind_factor(pot: dict[str, Any]) -> float:
    if "_wind_factor" in pot:
        return float(pot["_wind_factor"])
    return wind_factor(pot)


def local_observed_at(weather: dict[str, Any]) -> datetime:
    observed_local_at = weather.get("observed_local_at")
    if observed_local_at is not None:
        if observed_local_at.tzinfo is None:
            return observed_local_at.replace(tzinfo=LOCAL_TZ)
        return observed_local_at.astimezone(LOCAL_TZ)
    observed_at = weather["observed_at"]
    if observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=LOCAL_TZ)
    return observed_at.astimezone(LOCAL_TZ)

