from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from digital_twin.domain.pot import Pot
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_BASELINE_IRRIGATION_STEP,
    DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.state.environment import StateEnvironment
from digital_twin.simulation.sensors.calibration import (
    latest_sensor_state_for_pot,
    sensor_lookup_time,
    sensor_reading_for_pot,
)
from digital_twin.simulation.sensors.context import (
    sensor_control_pots,
)
from digital_twin.simulation.valves.zones import (
    is_valve_managed_pot,
    pots_by_valve_zone,
)
from digital_twin.simulation.weather_model import SimulationWeatherRepository


class StateProjector:
    """Projects pot state from sensor anchors through weather and baseline warm-up."""

    def __init__(
        self,
        state_environment: StateEnvironment | None = None,
        irrigation_policy: Any | None = None,
        baseline_step: Any | None = None,
        valve_zone_executor: Any | None = None,
        weather_repository: SimulationWeatherRepository | None = None,
    ) -> None:
        self.state_environment = state_environment if state_environment is not None else StateEnvironment()
        self.irrigation_policy = irrigation_policy if irrigation_policy is not None else DEFAULT_IRRIGATION_POLICY
        self.baseline_step = baseline_step if baseline_step is not None else DEFAULT_BASELINE_IRRIGATION_STEP
        self.valve_zone_executor = (
            valve_zone_executor if valve_zone_executor is not None else DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR
        )
        self.weather_repository = weather_repository or SimulationWeatherRepository()

    def initialize_from_first_day_sensor_readings(
        self,
        pot_states: dict[int, PotState],
        pots: list[dict[str, Any]],
        sensor_context: dict[str, Any],
        start_date: date,
    ) -> dict[str, Any]:
        if not sensor_context.get("available"):
            return {"anchored_pots": 0, "anchor_date": start_date.isoformat(), "source": "initial_inventory_state"}

        candidate_slots = self._first_day_sensor_slots(sensor_context, start_date)
        if not candidate_slots:
            return {"anchored_pots": 0, "anchor_date": start_date.isoformat(), "source": "initial_inventory_state"}

        anchored_pots = 0
        anchor_times: list[str] = []
        for pot in pots:
            pot_id = int(pot["id"])
            for slot_time in candidate_slots:
                reading = sensor_reading_for_pot(sensor_context, pot, start_date, slot_time)
                if reading is None:
                    continue
                pot_states[pot_id].moisture = soil.clamp(
                    soil.number(reading["soil_moisture_pct"], pot_states[pot_id].moisture),
                    0.0,
                    100.0,
                )
                anchored_pots += 1
                anchor_times.append(slot_time.strftime("%H:%M"))
                break

        return {
            "anchored_pots": anchored_pots,
            "anchor_date": start_date.isoformat(),
            "anchor_times": sorted(set(anchor_times)),
            "source": "first_day_direct_sensor_readings",
        }

    def prime_future_states(
        self,
        pot_states: dict[int, PotState],
        pots: list[dict[str, Any]],
        sensor_context: dict[str, Any],
        start_date: date,
        weather_by_day: dict[date, list[dict[str, Any]]],
    ) -> None:
        latest_state_at = self._latest_future_sensor_state_time(sensor_context)
        if latest_state_at is None or start_date <= latest_state_at.date():
            return

        self._apply_latest_sensor_states(pot_states, pots, sensor_context)
        self._load_missing_warmup_weather(weather_by_day, latest_state_at.date(), start_date - timedelta(days=1))

        current = (latest_state_at + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        end = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
        warmup_day_profiles: dict[date, dict[str, Any]] = {}
        zone_pots = pots_by_valve_zone(pots)
        control_pots = sensor_control_pots(pots, sensor_context)

        while current < end:
            self._prime_warmup_hour(
                pot_states,
                pots,
                control_pots,
                sensor_context,
                weather_by_day,
                warmup_day_profiles,
                zone_pots,
                current,
            )
            current += timedelta(hours=1)

    @staticmethod
    def weather_for_hour(day_weather: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any] | None:
        for row in day_weather:
            if local_observed_at(row).hour == observed_at.hour:
                return row
        return day_weather[0] if day_weather else None

    @staticmethod
    def _first_day_sensor_slots(sensor_context: dict[str, Any], start_date: date) -> list[time]:
        lookup = sensor_context.get("lookup") or {}
        return sorted(
            {
                sensor_lookup_time(slot_time)
                for reading_date, slot_time, _sensor_id in lookup.keys()
                if reading_date == start_date
            }
        )

    @staticmethod
    def _latest_future_sensor_state_time(sensor_context: dict[str, Any]) -> datetime | None:
        if not sensor_context.get("future_dates"):
            return None
        if not sensor_context.get("latest_states"):
            return None

        latest_state_at = sensor_context.get("latest_state_at")
        if latest_state_at is None:
            return None
        return latest_state_at if latest_state_at.tzinfo else latest_state_at.replace(tzinfo=LOCAL_TZ)

    @staticmethod
    def _apply_latest_sensor_states(
        pot_states: dict[int, PotState],
        pots: list[dict[str, Any]],
        sensor_context: dict[str, Any],
    ) -> None:
        for pot in pots:
            pot_id = int(pot["id"])
            latest = latest_sensor_state_for_pot(sensor_context, pot)
            if latest:
                pot_states[pot_id].moisture = soil.clamp(
                    soil.number(latest["soil_moisture_pct"], pot_states[pot_id].moisture),
                    0.0,
                    100.0,
                )

    def _load_missing_warmup_weather(
        self,
        weather_by_day: dict[date, list[dict[str, Any]]],
        warmup_start: date,
        warmup_end: date,
    ) -> None:
        if warmup_end < warmup_start:
            return

        missing_days = [
            warmup_start + timedelta(days=offset)
            for offset in range((warmup_end - warmup_start).days + 1)
            if warmup_start + timedelta(days=offset) not in weather_by_day
        ]
        if not missing_days:
            return

        warmup_weather = self.weather_repository.load_weather(min(missing_days), max(missing_days))
        for day, rows in self.state_environment.group_weather_by_day(warmup_weather).items():
            weather_by_day.setdefault(day, rows)

    def _prime_warmup_hour(
        self,
        pot_states: dict[int, PotState],
        pots: list[dict[str, Any]],
        control_pots: list[dict[str, Any]],
        sensor_context: dict[str, Any],
        weather_by_day: dict[date, list[dict[str, Any]]],
        warmup_day_profiles: dict[date, dict[str, Any]],
        zone_pots: dict[str, list[dict[str, Any]]],
        current: datetime,
    ) -> None:
        current_day = current.date()
        day_weather = weather_by_day.get(current_day, [])
        hour_weather = self.weather_for_hour(day_weather, current)
        if hour_weather is None:
            return

        day_profile = self._warmup_day_profile(current_day, day_weather, weather_by_day, warmup_day_profiles)
        self._apply_warmup_environment(pot_states, pots, hour_weather, day_profile, current_day)

        slot = self.irrigation_policy.decision_slot(current_day, current, day_profile)
        if slot is None:
            return

        decision_by_pot_id, zone_trigger_decisions = self._warmup_baseline_decisions(
            pot_states,
            control_pots,
            sensor_context,
            current_day,
            hour_weather,
            day_profile,
            slot,
        )
        self._execute_warmup_baseline_zones(
            pot_states,
            zone_pots,
            current_day,
            hour_weather,
            decision_by_pot_id,
            zone_trigger_decisions,
        )

    def _warmup_day_profile(
        self,
        current_day: date,
        day_weather: list[dict[str, Any]],
        weather_by_day: dict[date, list[dict[str, Any]]],
        warmup_day_profiles: dict[date, dict[str, Any]],
    ) -> dict[str, Any]:
        day_profile = warmup_day_profiles.get(current_day)
        if day_profile is None:
            day_profile = self.state_environment.day_profile(current_day, day_weather, weather_by_day)
            warmup_day_profiles[current_day] = day_profile
        return day_profile

    def _apply_warmup_environment(
        self,
        pot_states: dict[int, PotState],
        pots: list[dict[str, Any]],
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        current_day: date,
    ) -> None:
        for pot in pots:
            pot_id = int(pot["id"])
            self.state_environment.apply_hourly_environment(
                pot_states[pot_id],
                pot,
                hour_weather,
                day_profile,
                current_day,
                rain_exposure_factor=Pot.from_mapping(pot).rain_exposure_factor(current_day),
            )

    def _warmup_baseline_decisions(
        self,
        pot_states: dict[int, PotState],
        control_pots: list[dict[str, Any]],
        sensor_context: dict[str, Any],
        current_day: date,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
    ) -> tuple[dict[int, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

        for pot in control_pots:
            pot_id = int(pot["id"])
            decision = self.baseline_step.make_decision(
                pot_states[pot_id],
                pot,
                hour_weather,
                day_profile,
                slot,
                sensor_context,
                current_day,
            )
            decision_by_pot_id[pot_id] = decision
            if decision["should_irrigate"] and is_valve_managed_pot(pot, current_day):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        return decision_by_pot_id, zone_trigger_decisions

    def _execute_warmup_baseline_zones(
        self,
        pot_states: dict[int, PotState],
        zone_pots: dict[str, list[dict[str, Any]]],
        current_day: date,
        hour_weather: dict[str, Any],
        decision_by_pot_id: dict[int, dict[str, Any]],
        zone_trigger_decisions: dict[str, list[dict[str, Any]]],
    ) -> None:
        for zone, trigger_decisions in zone_trigger_decisions.items():
            self.valve_zone_executor.execute(
                pot_states,
                zone_pots,
                zone,
                current_day,
                hour_weather,
                decision_by_pot_id,
                trigger_decisions,
            )
