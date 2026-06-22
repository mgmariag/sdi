from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from digital_twin.domain.pot import Pot
from digital_twin.simulation.irrigation_controller.environment import precipitation_last_days, upcoming_freeze
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at

BASELINE_WINTER_LOOKAHEAD_DAYS = 14
DEFAULT_INITIAL_STATE_SEED = 2026


@dataclass(frozen=True)
class StateEnvironment:
    """Environmental state policy used by irrigation simulations."""

    winter_lookahead_days: int = BASELINE_WINTER_LOOKAHEAD_DAYS
    initial_state_seed: int = DEFAULT_INITIAL_STATE_SEED

    def initial_pot_states(self, pots: list[dict[str, Any]]) -> dict[int, PotState]:
        states = {}
        for pot in pots:
            rng = random.Random(self.initial_state_seed + pot["id"])
            target = pot["moisture_target_pct"]
            states[pot["id"]] = PotState(moisture=max(5.0, min(95.0, target + rng.uniform(-6.0, 4.0))))
        return states

    def copy_pot_states(self, states: dict[int, PotState]) -> dict[int, PotState]:
        return {
            pot_id: PotState(moisture=state.moisture, too_wet_hours=state.too_wet_hours)
            for pot_id, state in states.items()
        }

    def serialize_pot_states(self, states: dict[int, PotState]) -> dict[str, dict[str, float | int]]:
        return {
            str(pot_id): {
                "moisture": round(float(state.moisture), 4),
                "too_wet_hours": int(state.too_wet_hours),
            }
            for pot_id, state in states.items()
        }

    def copy_pot_states_from_payload(
        self,
        payload: dict[str, Any],
        fallback_states: dict[int, PotState],
    ) -> dict[int, PotState]:
        states = self.copy_pot_states(fallback_states)
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
                moisture = soil.number(raw_state.get("moisture"), fallback.moisture)
                too_wet_hours = int(soil.number(raw_state.get("too_wet_hours"), fallback.too_wet_hours))
            else:
                moisture = soil.number(raw_state, fallback.moisture)
                too_wet_hours = fallback.too_wet_hours
            states[pot_id] = PotState(
                moisture=soil.clamp(moisture, 0.0, 100.0),
                too_wet_hours=max(0, too_wet_hours),
            )
        return states

    def group_weather_by_day(self, weather_rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
        grouped: dict[date, list[dict[str, Any]]] = {}
        for row in weather_rows:
            day = local_observed_at(row).date()
            grouped.setdefault(day, []).append(row)
        return grouped

    def day_profiles_for_range(
        self,
        start_date: date,
        end_date: date,
        weather_by_day: dict[date, list[dict[str, Any]]],
    ) -> dict[date, dict[str, Any]]:
        profiles: dict[date, dict[str, Any]] = {}
        current_date = start_date
        while current_date <= end_date:
            day_weather = weather_by_day.get(current_date, [])
            if day_weather:
                profiles[current_date] = self.day_profile(current_date, day_weather, weather_by_day)
            current_date += timedelta(days=1)
        return profiles

    def day_profile(
        self,
        day: date,
        day_weather: list[dict[str, Any]],
        weather_by_day: dict[date, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        temperatures = [soil.number(row["temperature_c"], 20.0) for row in day_weather]
        humidities = [soil.number(row["relative_humidity_pct"], 60.0) for row in day_weather]
        cloud_covers = [self.weather_cloud_cover_pct(row, 0.0) for row in day_weather]
        radiation_values = [soil.number(row.get("shortwave_radiation_w_m2"), 0.0) for row in day_weather]
        precipitation = sum(soil.number(row["precipitation_mm"], 0.0) for row in day_weather)
        reference_et = sum(soil.hourly_reference_et_mm(row) for row in day_weather)
        rain_probabilities = [soil.number(row.get("precipitation_probability_pct"), 0.0) for row in day_weather]
        gusts = [soil.number(row["wind_gust_kmh"], soil.number(row["wind_speed_kmh"], 0.0)) for row in day_weather]
        lookahead_rows = [
            row
            for offset in range(self.winter_lookahead_days)
            for row in weather_by_day.get(day + timedelta(days=offset), [])
        ]
        lookahead_temperatures = [soil.number(row["temperature_c"], 20.0) for row in lookahead_rows] or temperatures
        lookahead_precipitation = sum(soil.number(row["precipitation_mm"], 0.0) for row in lookahead_rows)
        lookahead_probabilities = [
            soil.number(row.get("precipitation_probability_pct"), 0.0)
            for row in lookahead_rows
        ]
        freeze_risk = min(temperatures) <= 0 or upcoming_freeze(day, weather_by_day)
        no_rain_10_days = precipitation_last_days(day, weather_by_day, days=10) < 1.0
        dry_streak_day_count = self.dry_streak_days(day, weather_by_day)

        max_temperature = max(temperatures)
        max_gust = max(gusts)
        avg_humidity = sum(humidities) / max(len(humidities), 1)
        avg_cloud_cover = sum(cloud_covers) / max(len(cloud_covers), 1)
        avg_radiation = sum(radiation_values) / max(len(radiation_values), 1)

        return {
            "season": soil.season(day),
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

    def dry_streak_days(self, day: date, weather_by_day: dict[date, list[dict[str, Any]]]) -> int:
        streak = 0
        current = day
        while True:
            rows = weather_by_day.get(current)
            if not rows:
                return streak
            precipitation = sum(soil.number(row.get("precipitation_mm"), 0.0) for row in rows)
            if precipitation >= 0.5:
                return streak
            streak += 1
            current -= timedelta(days=1)

    def weather_cloud_cover_pct(self, weather: dict[str, Any], default: float) -> float:
        value = weather.get("cloud_cover_pct")
        if value is None:
            raw_payload = weather.get("raw_payload")
            if isinstance(raw_payload, dict):
                value = raw_payload.get("cloud_cover")
        return soil.number(value, default)

    def apply_hourly_environment(
        self,
        state: PotState,
        pot: dict[str, Any],
        weather: dict[str, Any],
        day_profile: dict[str, Any],
        local_day: date,
        rain_exposure_factor: float = 1.0,
    ) -> None:
        state.moisture = soil.apply_hourly_environment_moisture(
            state.moisture,
            pot,
            weather,
            local_day,
            rain_exposure_factor=rain_exposure_factor,
            outdoor=Pot.from_mapping(pot).is_outdoor(local_day),
        )
