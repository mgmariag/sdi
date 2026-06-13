from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from digital_twin.domain.sensors import ACTUAL_SENSOR_SOURCE, DEFAULT_SENSOR_SOURCE
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.soil_model import local_observed_at
from digital_twin.simulation.state.sensor_calibration import (
    _sensor_hour_is_future,
    lookup_sensor_reading,
    sensor_date_is_future,
    sensor_lookup_time,
)


def hourly_line_metadata(
    sensor_context: dict[str, Any],
    experiment_date: date,
    observed_local: datetime,
    weather: dict[str, Any],
) -> dict[str, Any]:
    metadata = _sensor_line_metadata_for_hour(sensor_context, experiment_date, observed_local)
    metadata["is_weather_prediction"] = _weather_row_is_prediction(weather)
    metadata["has_prediction_or_simulation"] = (
        metadata["is_weather_prediction"]
        or metadata["is_sensor_prediction"]
        or metadata["is_sensor_simulated"]
    )
    return metadata


def daily_line_metadata(
    sensor_context: dict[str, Any],
    experiment_date: date,
    day_weather: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata = _sensor_line_metadata_for_day(sensor_context, experiment_date)
    metadata["is_weather_prediction"] = _weather_day_is_prediction(experiment_date)
    metadata["has_prediction_or_simulation"] = (
        metadata["is_weather_prediction"]
        or metadata["is_sensor_prediction"]
        or metadata["is_sensor_simulated"]
    )
    return metadata


def _weather_row_is_prediction(weather: dict[str, Any]) -> bool:
    return local_observed_at(weather) > datetime.now(LOCAL_TZ)


def _weather_day_is_prediction(experiment_date: date) -> bool:
    return experiment_date > datetime.now(LOCAL_TZ).date()


def _sensor_line_metadata_for_hour(sensor_context: dict[str, Any], experiment_date: date, observed_at: datetime | time | int) -> dict[str, Any]:
    has_reading_for_day = has_sensor_reading_for_day(sensor_context, experiment_date)
    hour = observed_at.hour if isinstance(observed_at, (datetime, time)) else int(observed_at)
    if _sensor_hour_is_future(experiment_date, hour):
        return sensor_metadata(simulated=True, prediction=True, has_reading_for_day=has_reading_for_day)
    if sensor_date_is_future(sensor_context, experiment_date):
        return sensor_metadata(simulated=True, prediction=True, has_reading_for_day=has_reading_for_day)
    if not sensor_context.get("available"):
        return sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    sensor_ids = sensor_context.get("sensor_ids") or []
    lookup = sensor_context.get("lookup") or {}
    if not sensor_ids:
        return sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    slot_time = sensor_lookup_time(observed_at)
    for sensor_id in sensor_ids:
        reading = lookup_sensor_reading(lookup, experiment_date, slot_time, int(sensor_id))
        if reading is None or reading.get("source") != ACTUAL_SENSOR_SOURCE:
            return sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)
    return sensor_metadata(simulated=False, prediction=False, has_reading_for_day=has_reading_for_day)


def _sensor_line_metadata_for_day(sensor_context: dict[str, Any], experiment_date: date) -> dict[str, Any]:
    has_reading_for_day = has_sensor_reading_for_day(sensor_context, experiment_date)
    if sensor_date_is_future(sensor_context, experiment_date):
        return sensor_metadata(simulated=True, prediction=True, has_reading_for_day=has_reading_for_day)
    if not sensor_context.get("available"):
        return sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    sensor_ids = set(sensor_context.get("sensor_ids") or [])
    lookup = sensor_context.get("lookup") or {}
    if not sensor_ids:
        return sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    rows = [
        reading
        for (reading_date, _slot_time, sensor_id), reading in lookup.items()
        if reading_date == experiment_date and sensor_id in sensor_ids
    ]
    if not rows:
        return sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    actual_sensor_ids = {
        int(row["sensor_id"])
        for row in rows
        if row.get("source") == ACTUAL_SENSOR_SOURCE
    }
    has_non_actual = any(row.get("source") != ACTUAL_SENSOR_SOURCE for row in rows)
    simulated = has_non_actual or actual_sensor_ids != sensor_ids
    return sensor_metadata(simulated=simulated, prediction=False, has_reading_for_day=has_reading_for_day)


def has_sensor_reading_for_day(sensor_context: dict[str, Any], experiment_date: date) -> bool:
    return experiment_date in set(sensor_context.get("sensor_reading_dates") or [])


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


def sensor_metadata(simulated: bool, prediction: bool, has_reading_for_day: bool) -> dict[str, Any]:
    return {
        "is_sensor_simulated": simulated,
        "is_sensor_prediction": prediction,
        "has_sensor_reading_for_day": has_reading_for_day,
        "is_sensor_missing_reading": not has_reading_for_day,
    }


def combined_line_metadata(*entries: dict[str, Any]) -> dict[str, Any]:
    is_weather_prediction = any(bool(entry.get("is_weather_prediction")) for entry in entries if entry)
    is_sensor_prediction = any(bool(entry.get("is_sensor_prediction")) for entry in entries if entry)
    is_sensor_simulated = any(bool(entry.get("is_sensor_simulated")) for entry in entries if entry)
    is_sensor_missing_reading = any(bool(entry.get("is_sensor_missing_reading")) for entry in entries if entry)
    has_sensor_reading_for_day = any(bool(entry.get("has_sensor_reading_for_day")) for entry in entries if entry)
    return {
        "is_weather_prediction": is_weather_prediction,
        "is_sensor_prediction": is_sensor_prediction,
        "is_sensor_simulated": is_sensor_simulated,
        "has_sensor_reading_for_day": has_sensor_reading_for_day,
        "is_sensor_missing_reading": is_sensor_missing_reading,
        "has_prediction_or_simulation": is_weather_prediction or is_sensor_prediction or is_sensor_simulated,
    }


def sensor_summary_fields(sensor_context: dict[str, Any]) -> dict[str, Any]:
    future_dates = sensor_context.get("future_dates", [])
    fields = {
        "sensorDataUsed": bool(sensor_context.get("available")),
        "sensorSource": sensor_context.get("source", DEFAULT_SENSOR_SOURCE),
        "sensorRows": sensor_context.get("row_count", 0),
        "sensorLocationCount": len(sensor_context.get("sensor_ids", [])),
        "sensorAssociatedPotCount": sensor_context.get("associated_pot_count", 0),
        "latestStateRows": len(sensor_context.get("latest_states", {})),
        "sensorMappedDays": len(sensor_context.get("mapped_dates", {})),
        "futureStateEstimated": bool(future_dates),
        "futureEstimatedDays": len(future_dates),
    }
    if sensor_context.get("latest_state_at"):
        fields["latestKnownSoilStateAt"] = sensor_context["latest_state_at"].isoformat()
    if future_dates:
        fields["futureEstimatedDateRange"] = {
            "start": min(future_dates).isoformat(),
            "end": max(future_dates).isoformat(),
        }
    mapped_dates = sensor_context.get("mapped_dates", {})
    if mapped_dates:
        fields["sensorDateMappings"] = [
            {
                "experimentDate": experiment_date.isoformat(),
                "sensorDate": sensor_date.isoformat(),
            }
            for experiment_date, sensor_date in list(mapped_dates.items())[:10]
        ]
    if sensor_context.get("first_sensor_date"):
        fields["sensorFirstDate"] = sensor_context["first_sensor_date"].isoformat()
    if sensor_context.get("last_sensor_date"):
        fields["sensorLastDate"] = sensor_context["last_sensor_date"].isoformat()
    if sensor_context.get("error"):
        fields["sensorError"] = sensor_context["error"]
    return fields


def experiment_source(sensor_context: dict[str, Any] | None) -> str:
    if not sensor_context or not sensor_context.get("available"):
        return "database-weather-and-pot-inventory"
