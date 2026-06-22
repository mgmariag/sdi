from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from digital_twin.application.control_loop.sensing import SensingStage
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import ExperimentSnapshot, PotState
from digital_twin.domain.weather import local_observed_at
from digital_twin.simulation.state.environment import (
    StateEnvironment,
)
from digital_twin.simulation.state.projection import StateProjector
from digital_twin.simulation.weather_model import FutureWeatherEstimator, SimulationWeatherRepository


class ExperimentSnapshotLoader:
    """Builds the sensor, weather, and state snapshot used by experiment controllers."""

    cache_version: ClassVar[str] = "db-snapshot-v7-baseline-startup"

    def __init__(
        self,
        sensing: SensingStage | None = None,
        state_estimator: StateEstimator | None = None,
        state_environment: StateEnvironment | None = None,
        weather_repository: SimulationWeatherRepository | None = None,
        weather_estimator: FutureWeatherEstimator | None = None,
    ) -> None:
        self.state_environment = state_environment or StateEnvironment()
        self.weather_repository = weather_repository or SimulationWeatherRepository()
        self.weather_estimator = weather_estimator or FutureWeatherEstimator()
        self.sensing = sensing or SensingStage(weather_repository=self.weather_repository)
        self.state_estimator = state_estimator or StateEstimator(self.state_environment)

    def load(self, start_date: date, end_date: date) -> ExperimentSnapshot:
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")

        self.sensing.initialize_storage()
        pots = self.sensing.load_active_pots()
        if not pots:
            raise ValueError("No active pots found in the database")

        sensor_context = self.sensing.load_sensor_context(start_date, end_date, pots)
        weather_start = self._weather_start(start_date, sensor_context)
        weather_end = (
            end_date + timedelta(days=self.state_environment.winter_lookahead_days)
            if self._needs_winter_lookahead(start_date, end_date)
            else end_date
        )
        weather_rows = self.sensing.load_weather(weather_start, weather_end)
        self.weather_repository.raise_if_missing_historical_weather(weather_rows, start_date, end_date)
        weather_rows, estimated_weather_rows = self.weather_estimator.with_estimated_future_weather(
            weather_rows,
            weather_start,
            weather_end,
        )
        selected_weather_rows = [
            row for row in weather_rows
            if start_date <= local_observed_at(row).date() <= end_date
        ]
        estimated_selected_weather_rows = self._estimated_rows_in_range(selected_weather_rows)
        estimated_lookahead_weather_rows = self._estimated_lookahead_rows(weather_rows, start_date, end_date)
        if not selected_weather_rows:
            raise ValueError("No stored weather rows found for the selected date range")

        state_estimate = self.state_estimator.estimate(
            pots,
            weather_rows,
            sensor_context,
            start_date,
            end_date,
        )

        return ExperimentSnapshot(
            start_date=start_date,
            end_date=end_date,
            pot_count=len(pots),
            pots=pots,
            weather_rows=weather_rows,
            selected_weather_rows=selected_weather_rows,
            weather_by_day=state_estimate.weather_by_day,
            day_profiles=state_estimate.day_profiles,
            sensor_context=sensor_context,
            initial_pot_states=state_estimate.initial_states,
            estimated_weather_rows=estimated_weather_rows,
            estimated_selected_weather_rows=estimated_selected_weather_rows,
            estimated_lookahead_weather_rows=estimated_lookahead_weather_rows,
            loaded_at=datetime.now(LOCAL_TZ),
        )

    @staticmethod
    def _weather_start(start_date: date, sensor_context: dict[str, Any]) -> date:
        latest_state_at = sensor_context.get("latest_state_at")
        if sensor_context.get("future_dates") and latest_state_at:
            latest_state_date = latest_state_at.date()
            if latest_state_date < start_date:
                return latest_state_date
        return start_date

    @staticmethod
    def _needs_winter_lookahead(start_date: date, end_date: date) -> bool:
        current = start_date
        while current <= end_date:
            if current.month in {12, 1, 2, 3}:
                return True
            current += timedelta(days=1)
        return False

    @staticmethod
    def _estimated_rows_in_range(rows: list[dict[str, Any]]) -> int:
        return sum(1 for row in rows if row.get("source") == "estimated-weather")

    @staticmethod
    def _estimated_lookahead_rows(weather_rows: list[dict[str, Any]], start_date: date, end_date: date) -> int:
        return sum(
            1
            for row in weather_rows
            if row.get("source") == "estimated-weather"
            and not start_date <= local_observed_at(row).date() <= end_date
        )


@dataclass(frozen=True)
class StateEstimate:
    initial_states: dict[int, PotState]
    weather_by_day: dict[date, list[dict[str, Any]]]
    day_profiles: dict[date, dict[str, Any]]


class StateEstimator:
    """Builds the estimated state context used by experiment controllers."""

    def __init__(self, state_environment: StateEnvironment | None = None) -> None:
        self.state_environment = state_environment or StateEnvironment()
        self.state_projector = StateProjector(self.state_environment)

    def estimate(
        self,
        pots: list[dict[str, Any]],
        weather_rows: list[dict[str, Any]],
        sensor_context: dict[str, Any],
        start_date: date,
        end_date: date,
    ) -> StateEstimate:
        initial_states = self.state_environment.initial_pot_states(pots)
        weather_by_day = self.state_environment.group_weather_by_day(weather_rows)
        self.state_projector.prime_future_states(
            initial_states,
            pots,
            sensor_context,
            start_date,
            weather_by_day,
        )
        day_profiles = self.state_environment.day_profiles_for_range(start_date, end_date, weather_by_day)
        return StateEstimate(
            initial_states=initial_states,
            weather_by_day=weather_by_day,
            day_profiles=day_profiles,
        )
