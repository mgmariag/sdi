from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.state.environment import (
    day_profiles_for_range,
    group_weather_by_day,
    initial_pot_states,
)
from digital_twin.simulation.state.projection import prime_future_states


class StateEstimationStage:
    """Builds the estimated pot-state and weather context used by controllers."""

    def initial_states(self, pots: list[dict[str, Any]]) -> dict[int, PotState]:
        return initial_pot_states(pots)

    def weather_by_day(self, weather_rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
        return group_weather_by_day(weather_rows)

    def prime_future_sensor_states(
        self,
        states: dict[int, PotState],
        pots: list[dict[str, Any]],
        sensor_context: dict[str, Any],
        start_date: date,
        weather_by_day: dict[date, list[dict[str, Any]]],
    ) -> None:
        prime_future_states(states, pots, sensor_context, start_date, weather_by_day)

    def day_profiles(
        self,
        start_date: date,
        end_date: date,
        weather_by_day: dict[date, list[dict[str, Any]]],
    ) -> dict[date, dict[str, Any]]:
        return day_profiles_for_range(start_date, end_date, weather_by_day)
