from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.application.control_loop.sensing import SensingStage
from digital_twin.application.control_loop.state_estimation import StateEstimationStage
from digital_twin.domain.weather import ESTIMATED_WEATHER_SOURCE
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import ExperimentSnapshot
from digital_twin.simulation.soil_model import local_observed_at
from digital_twin.simulation.state.environment import BASELINE_WINTER_LOOKAHEAD_DAYS
from digital_twin.simulation.weather_model import (
    raise_if_missing_historical_weather,
    with_estimated_future_weather,
)


class ExperimentSnapshotLoader:
    """Builds the sensor, weather, and state snapshot used by experiment controllers."""

    def __init__(
        self,
        sensing: SensingStage | None = None,
        state_estimation: StateEstimationStage | None = None,
    ) -> None:
        self.sensing = sensing or SensingStage()
        self.state_estimation = state_estimation or StateEstimationStage()

    def load(self, start_date: date, end_date: date) -> ExperimentSnapshot:
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")

        self.sensing.initialize_storage()
        pots = self.sensing.load_active_pots()
        if not pots:
            raise ValueError("No active pots found in the database")

        sensor_context = self.sensing.load_sensor_context(start_date, end_date, pots)
        weather_start = snapshot_weather_start(start_date, sensor_context)
        weather_end = (
            end_date + timedelta(days=BASELINE_WINTER_LOOKAHEAD_DAYS)
            if baseline_winter_lookahead_needed(start_date, end_date)
            else end_date
        )
        weather_rows = self.sensing.load_weather(weather_start, weather_end)
        raise_if_missing_historical_weather(weather_rows, start_date, end_date)
        weather_rows, estimated_weather_rows = with_estimated_future_weather(weather_rows, weather_start, weather_end)
        selected_weather_rows = [
            row for row in weather_rows
            if start_date <= local_observed_at(row).date() <= end_date
        ]
        estimated_selected_weather_rows = _estimated_rows_in_range(selected_weather_rows)
        estimated_lookahead_weather_rows = _estimated_lookahead_rows(weather_rows, start_date, end_date)
        if not selected_weather_rows:
            raise ValueError("No stored weather rows found for the selected date range")

        initial_states = self.state_estimation.initial_states(pots)
        weather_by_day = self.state_estimation.weather_by_day(weather_rows)
        self.state_estimation.prime_future_sensor_states(
            initial_states,
            pots,
            sensor_context,
            start_date,
            weather_by_day,
        )
        day_profiles = self.state_estimation.day_profiles(start_date, end_date, weather_by_day)

        return ExperimentSnapshot(
            start_date=start_date,
            end_date=end_date,
            pot_count=len(pots),
            pots=pots,
            weather_rows=weather_rows,
            selected_weather_rows=selected_weather_rows,
            weather_by_day=weather_by_day,
            day_profiles=day_profiles,
            sensor_context=sensor_context,
            initial_pot_states=initial_states,
            estimated_weather_rows=estimated_weather_rows,
            estimated_selected_weather_rows=estimated_selected_weather_rows,
            estimated_lookahead_weather_rows=estimated_lookahead_weather_rows,
            loaded_at=datetime.now(LOCAL_TZ),
        )


def load_experiment_snapshot(start_date: date, end_date: date) -> ExperimentSnapshot:
    return ExperimentSnapshotLoader().load(start_date, end_date)


def snapshot_weather_start(start_date: date, sensor_context: dict[str, Any]) -> date:
    latest_state_at = sensor_context.get("latest_state_at")
    if sensor_context.get("future_dates") and latest_state_at:
        latest_state_date = latest_state_at.date()
        if latest_state_date < start_date:
            return latest_state_date
    return start_date


def baseline_winter_lookahead_needed(start_date: date, end_date: date) -> bool:
    current = start_date
    while current <= end_date:
        if current.month in {12, 1, 2, 3}:
            return True
        current += timedelta(days=1)
    return False


def _estimated_rows_in_range(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if row.get("source") == ESTIMATED_WEATHER_SOURCE)


def _estimated_lookahead_rows(weather_rows: list[dict[str, Any]], start_date: date, end_date: date) -> int:
    return sum(
        1
        for row in weather_rows
        if row.get("source") == ESTIMATED_WEATHER_SOURCE
        and not start_date <= local_observed_at(row).date() <= end_date
    )
