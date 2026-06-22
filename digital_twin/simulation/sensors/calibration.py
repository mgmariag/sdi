from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any

from digital_twin.domain.sensor import SensorSource
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil

MORNING_SENSOR_CALIBRATION_TIME = time(5, 30)
EVENING_SENSOR_CALIBRATION_TIME = time(17, 30)
def apply_sensor_reading(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
) -> dict[str, Any] | None:
    reading = sensor_reading_for_pot(sensor_context, pot, experiment_date, observed_at)
    if reading is None:
        return None

    reading = dict(reading)
    if reading.get("source") != SensorSource.ACTUAL.value:
        return reading

    sensor_moisture = soil.number(reading["soil_moisture_pct"], state.moisture)
    if reading.get("association_source") == "associated_sensor":
        sensor_weight = associated_sensor_weight(observed_at)
        state.moisture = soil.clamp(sensor_moisture * sensor_weight + state.moisture * (1.0 - sensor_weight), 0.0, 100.0)
        reading["soil_moisture_pct"] = round(state.moisture, 2)
        reading["sensor_blend_weight"] = round(sensor_weight, 2)
    else:
        state.moisture = soil.clamp(sensor_moisture, 0.0, 100.0)
    return reading


def apply_sensor_calibration_marker(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
    day_profile: dict[str, Any],
) -> dict[str, Any] | None:
    marker_time = sensor_calibration_marker_time(observed_at, day_profile)
    if marker_time is None:
        return None
    marker_at = datetime.combine(experiment_date, marker_time, tzinfo=LOCAL_TZ)
    return apply_stored_sensor_calibration(state, pot, experiment_date, marker_at, sensor_context)


def sensor_calibration_marker_time(observed_at: datetime, day_profile: dict[str, Any]) -> time | None:
    if observed_at.hour == 6:
        return MORNING_SENSOR_CALIBRATION_TIME
    if (
        observed_at.hour == 18
        and soil.number(day_profile.get("max_temperature_c"), 20.0) > 32.0
    ):
        return EVENING_SENSOR_CALIBRATION_TIME
    return None


def sampling_calibration_at(experiment_date: date, observed_at: datetime, day_profile: dict[str, Any]) -> datetime:
    marker_time = sensor_calibration_marker_time(observed_at, day_profile)
    if marker_time is None:
        return observed_at
    return datetime.combine(experiment_date, marker_time, tzinfo=LOCAL_TZ)


def apply_stored_sensor_calibration(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
) -> dict[str, Any] | None:
    reading = sensor_reading_for_pot(sensor_context, pot, experiment_date, observed_at)
    if reading is None:
        return None

    return apply_calibration_reading(state, reading, observed_at)


def apply_calibration_reading(
    state: PotState,
    reading: dict[str, Any],
    observed_at: datetime,
) -> dict[str, Any]:
    reading = dict(reading)
    sensor_moisture = soil.number(reading["soil_moisture_pct"], state.moisture)
    state.moisture = soil.clamp(sensor_moisture, 0.0, 100.0)
    if reading.get("association_source") == "associated_sensor":
        reading["soil_moisture_pct"] = round(state.moisture, 2)
        reading["sensor_blend_weight"] = 1.0
    reading["calibration_marker_time"] = observed_at.time().replace(second=0, microsecond=0).isoformat()
    return reading


def associated_sensor_weight(observed_at: datetime) -> float:
    if reading_slot_label(observed_at) == "evening":
        return 0.95
    return 0.82


def reading_slot_label(observed_at: datetime) -> str | None:
    if observed_at.hour == 18:
        return "evening"
    if observed_at.hour == 6:
        return "morning"
    if observed_at.hour == 10:
        return "winter_check"
    return None


def sensor_reading_for_pot(
    sensor_context: dict[str, Any] | None,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime | time | int,
) -> dict[str, Any] | None:
    if not sensor_context or not sensor_context.get("available"):
        return None

    lookup = sensor_context.get("lookup") or {}
    pot_id = int(pot["id"])
    slot_time = sensor_lookup_time(observed_at)
    direct = lookup_sensor_reading(lookup, experiment_date, slot_time, pot_id)
    if direct is not None:
        return direct

    association = (sensor_context.get("associations") or {}).get(pot_id)
    if not association:
        return None
    sensor_id = int(association["sensor_id"])
    sensor_reading = lookup_sensor_reading(lookup, experiment_date, slot_time, sensor_id)
    if sensor_reading is None:
        return None
    sensor_pot = (sensor_context.get("sensor_pots") or {}).get(sensor_id)
    if sensor_pot is None:
        return sensor_reading
    return associated_sensor_reading(pot, sensor_pot, sensor_reading)


def forecast_sensor_reading_for_pot(
    pot_states: dict[int, PotState],
    sensor_context: dict[str, Any] | None,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime | time | int,
) -> dict[str, Any] | None:
    if not sensor_context or not sensor_context.get("available"):
        return None
    if not _sensor_slot_is_prediction(sensor_context, experiment_date, observed_at):
        return None

    pot_id = int(pot["id"])
    reference_state = pot_states.get(pot_id)
    if reference_state is None:
        return None

    association = (sensor_context.get("associations") or {}).get(pot_id) or {}
    physical_sensor_id = int(association.get("sensor_id", pot_id))
    slot_time = sensor_lookup_time(observed_at)
    reading = {
        "sensor_id": pot_id,
        "source": SensorSource.SPARSE_FORECAST.value,
        "soil_moisture_pct": round(reference_state.moisture, 2),
        "local_date": experiment_date,
        "local_time": slot_time,
        "recorded_at": datetime.combine(experiment_date, slot_time, tzinfo=LOCAL_TZ),
        "resolution": "forecast",
        "sample_count": 1,
        "association_source": "default_strategy_reference",
        "sensor_blend_weight": 1.0,
    }
    if physical_sensor_id != pot_id:
        reading["associated_sensor_id"] = physical_sensor_id
    return reading


def sensor_lookup_time(value: datetime | time | int) -> time:
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    return time(int(value), 0)


def lookup_sensor_reading(
    lookup: dict[tuple[Any, ...], dict[str, Any]],
    experiment_date: date,
    slot_time: time,
    sensor_id: int,
) -> dict[str, Any] | None:
    exact = lookup.get((experiment_date, slot_time, sensor_id))
    if exact is not None:
        return exact

    legacy = lookup.get((experiment_date, slot_time.hour, sensor_id))
    if legacy is not None:
        return legacy

    return None


def associated_sensor_reading(
    pot: dict[str, Any],
    sensor_pot: dict[str, Any],
    sensor_reading: dict[str, Any],
) -> dict[str, Any]:
    sensor_moisture = soil.number(sensor_reading["soil_moisture_pct"], pot["moisture_target_pct"])
    target_adjustment = (float(pot["moisture_target_pct"]) - float(sensor_pot["moisture_target_pct"])) * 0.45
    min_adjustment = (float(pot["moisture_min_pct"]) - float(sensor_pot["moisture_min_pct"])) * 0.2
    exposure_adjustment = (pot_exposure_index(sensor_pot) - pot_exposure_index(pot)) * 2.2
    retention_adjustment = (float(pot["retention_factor"]) - float(sensor_pot["retention_factor"])) * 4.0
    volume_adjustment = math.log(max(float(pot["volume_l"]), 0.1) / max(float(sensor_pot["volume_l"]), 0.1)) * 0.8
    inferred_moisture = soil.clamp(
        sensor_moisture
        + target_adjustment
        + min_adjustment
        + exposure_adjustment
        + retention_adjustment
        + volume_adjustment,
        0.0,
        100.0,
    )
    reading = dict(sensor_reading)
    reading["sensor_id"] = pot["id"]
    reading["associated_sensor_id"] = sensor_pot["id"]
    reading["association_source"] = "associated_sensor"
    reading["soil_moisture_pct"] = round(inferred_moisture, 2)
    return reading


def pot_exposure_index(pot: dict[str, Any]) -> float:
    rain = {
        "covered": 0.0,
        "partially_exposed": 0.5,
        "fully_exposed": 1.0,
    }.get(str(pot.get("rain_exposure") or "partially_exposed"), 0.5)
    return rain + (soil.sun_factor(pot) - 1.0) * 1.6 + (soil.wind_factor(pot) - 1.0) * 1.2


def latest_sensor_state_for_pot(sensor_context: dict[str, Any], pot: dict[str, Any]) -> dict[str, Any] | None:
    latest_states = sensor_context.get("latest_states") or {}
    pot_id = int(pot["id"])
    direct = latest_states.get(pot_id)
    if direct is not None:
        return direct

    association = (sensor_context.get("associations") or {}).get(pot_id)
    if not association:
        return None
    sensor_id = int(association["sensor_id"])
    latest = latest_states.get(sensor_id)
    sensor_pot = (sensor_context.get("sensor_pots") or {}).get(sensor_id)
    if latest is None or sensor_pot is None:
        return latest
    return associated_sensor_reading(pot, sensor_pot, latest)


def sensor_date_is_future(sensor_context: dict[str, Any], experiment_date: date) -> bool:
    return experiment_date in set(sensor_context.get("future_dates") or [])


def _sensor_hour_is_future(experiment_date: date, hour: int) -> bool:
    observed_at = datetime.combine(experiment_date, time(hour, 0), tzinfo=LOCAL_TZ)
    return observed_at > datetime.now(LOCAL_TZ)


def _sensor_slot_is_prediction(
    sensor_context: dict[str, Any],
    experiment_date: date,
    observed_at: datetime | time | int,
) -> bool:
    slot_time = sensor_lookup_time(observed_at)
    return sensor_date_is_future(sensor_context, experiment_date) or _sensor_hour_is_future(
        experiment_date,
        slot_time.hour,
    )

