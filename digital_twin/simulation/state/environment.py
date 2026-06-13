from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from digital_twin.simulation.irrigation_controller.environment import (
    is_outdoor,
    precipitation_last_days,
    upcoming_freeze,
)
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.soil_model import (
    apply_hourly_environment_moisture,
    clamp,
    hourly_reference_et_mm,
    local_observed_at,
    number,
    season,
)

BASELINE_WINTER_LOOKAHEAD_DAYS = 14
def initial_pot_states(pots: list[dict[str, Any]]) -> dict[int, PotState]:
    states = {}
    for pot in pots:
        rng = random.Random(2026 + pot["id"])
        target = pot["moisture_target_pct"]
        states[pot["id"]] = PotState(moisture=max(5.0, min(95.0, target + rng.uniform(-6.0, 4.0))))
    return states


def copy_pot_states(states: dict[int, PotState]) -> dict[int, PotState]:
    return {
        pot_id: PotState(moisture=state.moisture, too_wet_hours=state.too_wet_hours)
        for pot_id, state in states.items()
    }


def serialize_pot_states(states: dict[int, PotState]) -> dict[str, dict[str, float | int]]:
    return {
        str(pot_id): {
            "moisture": round(float(state.moisture), 4),
            "too_wet_hours": int(state.too_wet_hours),
        }
        for pot_id, state in states.items()
    }


def copy_pot_states_from_payload(
    payload: dict[str, Any],
    fallback_states: dict[int, PotState],
) -> dict[int, PotState]:
    states = copy_pot_states(fallback_states)
    if not isinstance(payload, dict):
        return states

    for raw_pot_id, raw_state in payload.items():
        try:
            pot_id = int(raw_pot_id)
        except (TypeError, ValueError):
            continue
        if pot_id not in states:
            continue

        fallback = states[pot_id]
        if isinstance(raw_state, dict):
            moisture = number(raw_state.get("moisture"), fallback.moisture)
            too_wet_hours = int(number(raw_state.get("too_wet_hours"), fallback.too_wet_hours))
        else:
            moisture = number(raw_state, fallback.moisture)
            too_wet_hours = fallback.too_wet_hours
        states[pot_id] = PotState(
            moisture=clamp(moisture, 0.0, 100.0),
            too_wet_hours=max(0, too_wet_hours),
        )
    return states


def group_weather_by_day(weather_rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in weather_rows:
        day = local_observed_at(row).date()
        grouped.setdefault(day, []).append(row)
    return grouped


def day_profiles_for_range(
    start_date: date,
    end_date: date,
    weather_by_day: dict[date, list[dict[str, Any]]],
) -> dict[date, dict[str, Any]]:
    profiles: dict[date, dict[str, Any]] = {}
    current_date = start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if day_weather:
            profiles[current_date] = day_profile(current_date, day_weather, weather_by_day)
        current_date += timedelta(days=1)
    return profiles


def day_profile(day: date, day_weather: list[dict[str, Any]], weather_by_day: dict[date, list[dict[str, Any]]]) -> dict[str, Any]:
    temperatures = [number(row["temperature_c"], 20.0) for row in day_weather]
    humidities = [number(row["relative_humidity_pct"], 60.0) for row in day_weather]
    cloud_covers = [weather_cloud_cover_pct(row, 0.0) for row in day_weather]
    radiation_values = [number(row.get("shortwave_radiation_w_m2"), 0.0) for row in day_weather]
    precipitation = sum(number(row["precipitation_mm"], 0.0) for row in day_weather)
    reference_et = sum(hourly_reference_et_mm(row) for row in day_weather)
    rain_probabilities = [number(row.get("precipitation_probability_pct"), 0.0) for row in day_weather]
    gusts = [number(row["wind_gust_kmh"], number(row["wind_speed_kmh"], 0.0)) for row in day_weather]
    lookahead_rows = [
        row
        for offset in range(BASELINE_WINTER_LOOKAHEAD_DAYS)
        for row in weather_by_day.get(day + timedelta(days=offset), [])
    ]
    lookahead_temperatures = [number(row["temperature_c"], 20.0) for row in lookahead_rows] or temperatures
    lookahead_precipitation = sum(number(row["precipitation_mm"], 0.0) for row in lookahead_rows)
    lookahead_probabilities = [
        number(row.get("precipitation_probability_pct"), 0.0)
        for row in lookahead_rows
    ]
    freeze_risk = min(temperatures) <= 0 or upcoming_freeze(day, weather_by_day)
    no_rain_10_days = precipitation_last_days(day, weather_by_day, days=10) < 1.0
    dry_streak_day_count = dry_streak_days(day, weather_by_day)

    max_temperature = max(temperatures)
    max_gust = max(gusts)
    avg_humidity = sum(humidities) / max(len(humidities), 1)
    avg_cloud_cover = sum(cloud_covers) / max(len(cloud_covers), 1)
    avg_radiation = sum(radiation_values) / max(len(radiation_values), 1)

    return {
        "season": season(day),
        "dormant_period": day.month in {12, 1, 2, 3},
        "avg_temperature_c": sum(temperatures) / max(len(temperatures), 1),
        "max_temperature_c": max_temperature,
        "min_temperature_c": min(temperatures),
        "avg_humidity_pct": avg_humidity,
        "avg_cloud_cover_pct": avg_cloud_cover,
        "avg_shortwave_radiation_w_m2": avg_radiation,
        "max_shortwave_radiation_w_m2": max(radiation_values) if radiation_values else 0.0,
        "precipitation_mm": precipitation,
        "precipitation_next_14_days_mm": lookahead_precipitation,
        "reference_evapotranspiration_mm": reference_et,
        "max_precipitation_probability_pct": max(rain_probabilities) if rain_probabilities else 0.0,
        "max_precipitation_probability_next_14_days_pct": max(lookahead_probabilities) if lookahead_probabilities else 0.0,
        "max_wind_gust_kmh": max_gust,
        "min_temperature_next_14_days_c": min(lookahead_temperatures),
        "heatwave_day": max_temperature >= 30.0,
        "dry_windy_day": max_gust >= 35.0 and avg_humidity <= 55.0,
        "freeze_risk": freeze_risk,
        "no_rain_10_days": no_rain_10_days,
        "dry_streak_days": dry_streak_day_count,
    }


def dry_streak_days(day: date, weather_by_day: dict[date, list[dict[str, Any]]]) -> int:
    streak = 0
    current = day
    while True:
        rows = weather_by_day.get(current)
        if not rows:
            return streak
        precipitation = sum(number(row.get("precipitation_mm"), 0.0) for row in rows)
        if precipitation >= 0.5:
            return streak
        streak += 1
        current -= timedelta(days=1)


def weather_cloud_cover_pct(weather: dict[str, Any], default: float) -> float:
    value = weather.get("cloud_cover_pct")
    if value is None:
        raw_payload = weather.get("raw_payload")
        if isinstance(raw_payload, dict):
            value = raw_payload.get("cloud_cover")
    return number(value, default)


def apply_hourly_environment(
    state: PotState,
    pot: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    local_day: date,
    rain_exposure_factor: float = 1.0,
) -> None:
    state.moisture = apply_hourly_environment_moisture(
        state.moisture,
        pot,
        weather,
        local_day,
        rain_exposure_factor=rain_exposure_factor,
        outdoor=is_outdoor(pot, local_day),
    )
