from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from digital_twin.simulation.irrigation_controller.baseline_decision import (
    make_baseline_irrigation_decision,
)
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.environment import (
    rain_exposure_factor,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.soil_model import clamp, local_observed_at, number
from digital_twin.simulation.state.environment import (
    apply_hourly_environment,
    day_profile as build_day_profile,
    group_weather_by_day,
)
from digital_twin.simulation.state.sensor_calibration import (
    latest_sensor_state_for_pot,
    sensor_lookup_time,
    sensor_reading_for_pot,
)
from digital_twin.simulation.state.sensor_context import (
    sensor_control_pots,
    with_sensor_key,
)
from digital_twin.simulation.valves.distribution import (
    apply_cold_month_indoor_skip,
    baseline_zone_dose_factor,
    execute_valve_zone_distribution,
    trigger_pot_codes as collect_trigger_pot_codes,
    trigger_pot_ids as collect_trigger_pot_ids,
    trigger_sensor_ids as collect_trigger_sensor_ids,
    zone_execution_decision_map,
)
from digital_twin.simulation.valves.zones import (
    is_valve_managed_pot,
    pots_by_valve_zone,
)
from digital_twin.simulation.weather_model import load_weather


def initialize_states_from_first_day_sensor_readings(
    pot_states: dict[int, PotState],
    pots: list[dict[str, Any]],
    sensor_context: dict[str, Any],
    start_date: date,
) -> dict[str, Any]:
    if not sensor_context.get("available"):
        return {"anchored_pots": 0, "anchor_date": start_date.isoformat(), "source": "initial_inventory_state"}

    lookup = sensor_context.get("lookup") or {}
    candidate_slots = sorted(
        {
            sensor_lookup_time(slot_time)
            for reading_date, slot_time, _sensor_id in lookup.keys()
            if reading_date == start_date
        }
    )
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
            pot_states[pot_id].moisture = clamp(
                number(reading["soil_moisture_pct"], pot_states[pot_id].moisture),
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
    pot_states: dict[int, PotState],
    pots: list[dict[str, Any]],
    sensor_context: dict[str, Any],
    start_date: date,
    weather_by_day: dict[date, list[dict[str, Any]]],
) -> None:
    if not sensor_context.get("future_dates"):
        return

    latest_state_at = sensor_context.get("latest_state_at")
    latest_states = sensor_context.get("latest_states") or {}
    if latest_state_at is None or not latest_states:
        return

    latest_state_at = latest_state_at if latest_state_at.tzinfo else latest_state_at.replace(tzinfo=LOCAL_TZ)
    if start_date <= latest_state_at.date():
        return

    for pot in pots:
        latest = latest_sensor_state_for_pot(sensor_context, pot)
        if latest:
            pot_states[pot["id"]].moisture = clamp(number(latest["soil_moisture_pct"], pot_states[pot["id"]].moisture), 0.0, 100.0)

    warmup_start = latest_state_at.date()
    warmup_end = start_date - timedelta(days=1)
    if warmup_end >= warmup_start:
        missing_days = [
            warmup_start + timedelta(days=offset)
            for offset in range((warmup_end - warmup_start).days + 1)
            if warmup_start + timedelta(days=offset) not in weather_by_day
        ]
        if missing_days:
            warmup_weather = load_weather(min(missing_days), max(missing_days))
            for day, rows in group_weather_by_day(warmup_weather).items():
                weather_by_day.setdefault(day, rows)

    current = (latest_state_at + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    end = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    warmup_day_profiles: dict[date, dict[str, Any]] = {}
    zone_pots = pots_by_valve_zone(pots)
    control_pots = sensor_control_pots(pots, sensor_context)
    while current < end:
        day_weather = weather_by_day.get(current.date(), [])
        hour_weather = weather_for_hour(day_weather, current)
        if hour_weather is None:
            current += timedelta(hours=1)
            continue

        current_day = current.date()
        day_profile = warmup_day_profiles.get(current_day)
        if day_profile is None:
            day_profile = build_day_profile(current_day, day_weather, weather_by_day)
            warmup_day_profiles[current_day] = day_profile
        for pot in pots:
            state = pot_states[pot["id"]]
            apply_hourly_environment(
                state,
                pot,
                hour_weather,
                day_profile,
                current.date(),
                rain_exposure_factor=rain_exposure_factor(pot, current.date()),
            )
        slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current.date(), current, day_profile)
        if slot is None:
            current += timedelta(hours=1)
            continue

        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}
        for pot in control_pots:
            state = pot_states[pot["id"]]
            decision = make_baseline_irrigation_decision(state, pot, hour_weather, day_profile, slot)
            decision = with_sensor_key(decision, pot, sensor_context)
            decision = apply_cold_month_indoor_skip(decision, pot, current_day)
            decision_by_pot_id[int(pot["id"])] = decision
            if decision["should_irrigate"] and is_valve_managed_pot(pot, current_day):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        for zone, trigger_decisions in zone_trigger_decisions.items():
            trigger_pot_ids = collect_trigger_pot_ids(trigger_decisions)
            trigger_sensor_ids = collect_trigger_sensor_ids(trigger_decisions)
            trigger_pot_codes = collect_trigger_pot_codes(trigger_decisions)
            zone_dose_factor = baseline_zone_dose_factor(trigger_decisions)
            execution_decisions = zone_execution_decision_map(
                decision_by_pot_id,
                zone_pots,
                zone,
                current_day,
                trigger_decisions,
            )
            execute_valve_zone_distribution(
                pot_states,
                zone_pots,
                zone,
                current_day,
                hour_weather,
                execution_decisions,
                lambda zone_pot, zone_decision: {
                    **zone_decision,
                    "should_irrigate": True,
                    "dose_factor": zone_dose_factor,
                },
                DEFAULT_IRRIGATION_POLICY.irrigation_request,
                {
                    "zone_triggered": True,
                    "zone_trigger_sensor_ids": trigger_sensor_ids,
                    "zone_trigger_pot_ids": trigger_pot_ids,
                    "zone_trigger_pot_codes": trigger_pot_codes,
                    "runtime_request_sensor_ids": trigger_sensor_ids,
                    "zone_dose_factor": zone_dose_factor,
                    "zone": zone,
                    "zone_activation_policy": "sensor_pot_trigger",
                    "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
                },
            )
        current += timedelta(hours=1)


def weather_for_hour(day_weather: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any] | None:
    for row in day_weather:
        if local_observed_at(row).hour == observed_at.hour:
            return row
    return day_weather[0] if day_weather else None
