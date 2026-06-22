from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from digital_twin.simulation.metrics import (
    daily_moisture_summary,
    new_daily_moisture_tracker,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.simulation.state.environment import StateEnvironment


def hourly_aggregate_entry(
    observed_at: datetime,
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    pot_states: dict[int, PotState],
    hourly_water_ml: float,
    hourly_events: int,
    hourly_decisions: int,
    hourly_alerts: int,
    extra: dict[str, Any] | None = None,
    state_environment: StateEnvironment | None = None,
) -> dict[str, Any]:
    state_environment = state_environment or StateEnvironment()
    moistures = [state.moisture for state in pot_states.values()]
    avg_moisture = sum(moistures) / max(len(moistures), 1)
    temperature = soil.number(weather["temperature_c"], day_profile["avg_temperature_c"])
    humidity = soil.number(weather["relative_humidity_pct"], day_profile["avg_humidity_pct"])
    cloud_cover = state_environment.weather_cloud_cover_pct(weather, day_profile["avg_cloud_cover_pct"])
    rain_amount = soil.number(weather["precipitation_mm"], 0.0)
    wind_gust = soil.number(weather["wind_gust_kmh"], soil.number(weather["wind_speed_kmh"], 0.0))
    valve_runs = max(0, int(hourly_events or 0))
    entry = {
        "date": observed_at.date().isoformat(),
        "timestamp": observed_at.isoformat(),
        "day_label": observed_at.strftime("%Y-%m-%d %H:%M"),
        "chart_label": observed_at.strftime("%m-%d %H:%M"),
        "hour": observed_at.strftime("%H:%M"),
        "moisture": round(avg_moisture, 2),
        "average_moisture": round(avg_moisture, 2),
        "min_moisture": round(min(moistures), 2),
        "max_moisture": round(max(moistures), 2),
        "temperature": round(temperature, 2),
        "max_temperature": round(temperature, 2),
        "min_temperature": round(day_profile["min_temperature_c"], 2),
        "humidity": round(humidity, 2),
        "cloud_cover_pct": round(cloud_cover, 2),
        "rain_prediction": rain_amount >= 0.5,
        "rain_amount": round(rain_amount, 2),
        "wind_gust_kmh": round(wind_gust, 2),
        "heatwave_day": day_profile["heatwave_day"],
        "freeze_risk": day_profile["freeze_risk"],
        "irrigation_active": valve_runs > 0,
        "irrigation_events": 1 if valve_runs > 0 else 0,
        "valve_runs": valve_runs,
        "irrigated_pots": valve_runs,
        "irrigation_decisions": hourly_decisions,
        "alerts": hourly_alerts,
        "water_usage_ml": round(hourly_water_ml, 2),
        "water_usage_l": round(hourly_water_ml / 1000.0, 2),
    }
    if extra:
        entry.update(extra)
    return entry


def daily_aggregate_entry(
    current_date: date,
    day_profile: dict[str, Any],
    pot_states: dict[int, PotState],
    daily_water_ml: float,
    daily_events: int,
    daily_decisions: int,
    daily_alerts: int,
    extra: dict[str, Any] | None = None,
    moisture_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    moisture = moisture_summary or daily_moisture_summary(new_daily_moisture_tracker(), pot_states)
    valve_runs = max(0, int(daily_events or 0))
    entry = {
        "date": current_date.isoformat(),
        "timestamp": datetime.combine(current_date, time(12, 0), tzinfo=LOCAL_TZ).isoformat(),
        "day_label": current_date.strftime("%Y-%m-%d"),
        "chart_label": current_date.strftime("%Y-%m-%d"),
        "moisture": moisture["moisture"],
        "average_moisture": moisture["average_moisture"],
        "min_moisture": moisture["min_moisture"],
        "max_moisture": moisture["max_moisture"],
        "moisture_sample_count": moisture.get("moisture_sample_count", 0),
        "moisture_sample_method": moisture.get("moisture_sample_method", "end_of_day"),
        "moisture_sample_labels": moisture.get("moisture_sample_labels", []),
        "pre_irrigation_moisture": moisture.get("pre_irrigation_moisture"),
        "post_irrigation_moisture": moisture.get("post_irrigation_moisture"),
        "temperature": round(day_profile["avg_temperature_c"], 2),
        "max_temperature": round(day_profile["max_temperature_c"], 2),
        "min_temperature": round(day_profile["min_temperature_c"], 2),
        "humidity": round(day_profile["avg_humidity_pct"], 2),
        "cloud_cover_pct": round(day_profile["avg_cloud_cover_pct"], 2),
        "rain_prediction": day_profile["precipitation_mm"] >= 0.5,
        "rain_amount": round(day_profile["precipitation_mm"], 2),
        "wind_gust_kmh": round(day_profile["max_wind_gust_kmh"], 2),
        "heatwave_day": day_profile["heatwave_day"],
        "freeze_risk": day_profile["freeze_risk"],
        "irrigation_active": valve_runs > 0,
        "irrigation_events": 1 if valve_runs > 0 else 0,
        "valve_runs": valve_runs,
        "irrigated_pots": valve_runs,
        "irrigation_decisions": daily_decisions,
        "alerts": daily_alerts,
        "water_usage_ml": round(daily_water_ml, 2),
        "water_usage_l": round(daily_water_ml / 1000.0, 2),
    }
    if extra:
        entry.update(extra)
    return entry


