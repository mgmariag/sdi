from __future__ import annotations

import math
from datetime import date
from typing import Any

from psycopg.rows import dict_row

from digital_twin.application.sensor_history.readings.availability import load_sensor_readings_for_experiment
from digital_twin.application.sensor_history.readings.core import ensure_sensor_readings_for_experiment_range
from digital_twin.domain.sensors import DEFAULT_SENSOR_SOURCE
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.simulation.soil_model import clamp, number


def load_sensor_context(start_date: date, end_date: date, pots: list[dict[str, Any]]) -> dict[str, Any]:
    sensor_ids = [pot["id"] for pot in pots]
    try:
        ensure_sensor_readings_for_experiment_range(start_date, end_date, source=DEFAULT_SENSOR_SOURCE)
        sensor_context = load_sensor_readings_for_experiment(
            start_date=start_date,
            end_date=end_date,
            sensor_ids=sensor_ids,
            source=DEFAULT_SENSOR_SOURCE,
        )
        return with_sensor_associations(sensor_context, pots)
    except Exception as exc:
        return {
            "available": False,
            "source": DEFAULT_SENSOR_SOURCE,
            "lookup": {},
            "mapped_dates": {},
            "sensor_reading_dates": set(),
            "row_count": 0,
            "error": str(exc),
        }


def with_sensor_associations(sensor_context: dict[str, Any], pots: list[dict[str, Any]]) -> dict[str, Any]:
    if not sensor_context.get("available"):
        return sensor_context

    sensor_ids = {int(sensor_id) for sensor_id in sensor_context.get("sensor_ids") or []}
    pot_by_id = {int(pot["id"]): pot for pot in pots}
    sensor_pots = [pot_by_id[sensor_id] for sensor_id in sensor_ids if sensor_id in pot_by_id]
    if not sensor_pots:
        return sensor_context

    sensors_by_zone: dict[str, list[dict[str, Any]]] = {}
    for sensor_pot in sensor_pots:
        sensors_by_zone.setdefault(str(sensor_pot.get("balcony_zone") or ""), []).append(sensor_pot)

    associations = {}
    for pot in pots:
        pot_id = int(pot["id"])
        if pot_id in sensor_ids:
            associations[pot_id] = {"sensor_id": pot_id, "direct": True, "distance": 0.0}
            continue
        zone_sensors = sensors_by_zone.get(str(pot.get("balcony_zone") or "")) or sensor_pots
        sensor_pot = min(zone_sensors, key=lambda item: sensor_association_distance(pot, item))
        associations[pot_id] = {
            "sensor_id": int(sensor_pot["id"]),
            "direct": False,
            "distance": round(sensor_association_distance(pot, sensor_pot), 4),
        }

    enriched = dict(sensor_context)
    enriched["associations"] = associations
    enriched["sensor_pots"] = {int(pot["id"]): pot for pot in sensor_pots}
    enriched["associated_pot_count"] = len([item for item in associations.values() if not item["direct"]])
    sensor_thresholds = load_sensor_threshold_overrides(sensor_ids)
    if sensor_context.get("sensor_thresholds"):
        sensor_thresholds.update(sensor_context["sensor_thresholds"])
    if sensor_thresholds:
        enriched["sensor_thresholds"] = sensor_thresholds
    return enriched


def sensor_control_ids(sensor_context: dict[str, Any] | None, pots: list[dict[str, Any]]) -> set[int]:
    pot_ids = {int(pot["id"]) for pot in pots}
    sensor_ids = {
        int(sensor_id)
        for sensor_id in (sensor_context or {}).get("sensor_ids", [])
        if int(sensor_id) in pot_ids
    }
    if sensor_ids:
        return sensor_ids

    sensor_pots = (sensor_context or {}).get("sensor_pots") or {}
    sensor_ids = {int(sensor_id) for sensor_id in sensor_pots if int(sensor_id) in pot_ids}
    if sensor_ids:
        return sensor_ids

    return set(pot_ids)


def sensor_control_pots(pots: list[dict[str, Any]], sensor_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    control_ids = sensor_control_ids(sensor_context, pots)
    return [
        sensor_control_pot(pot, sensor_context)
        for pot in pots
        if int(pot["id"]) in control_ids
    ]


def sensor_control_pot(pot: dict[str, Any], sensor_context: dict[str, Any] | None) -> dict[str, Any]:
    overrides = sensor_threshold_overrides(sensor_context, int(pot["id"]))
    if not overrides:
        return pot

    enriched = dict(pot)
    enriched.update(overrides)
    enriched["sensor_threshold_override"] = dict(overrides)
    return enriched


def sensor_threshold_overrides(sensor_context: dict[str, Any] | None, sensor_id: int) -> dict[str, float]:
    if not sensor_context:
        return {}

    raw_configs = (
        sensor_context.get("sensor_thresholds")
        or sensor_context.get("sensor_comfort_thresholds")
        or sensor_context.get("sensor_configs")
        or {}
    )
    raw_config = raw_configs.get(sensor_id) or raw_configs.get(str(sensor_id)) or {}
    if not isinstance(raw_config, dict):
        return {}

    return normalize_sensor_threshold_config(raw_config)


def normalize_sensor_threshold_config(raw_config: dict[str, Any]) -> dict[str, float]:
    aliases = {
        "moisture_min_pct": "moisture_min_pct",
        "minimum_moisture_pct": "moisture_min_pct",
        "min_moisture_pct": "moisture_min_pct",
        "moisture_target_pct": "moisture_target_pct",
        "target_moisture_pct": "moisture_target_pct",
        "comfort_threshold_pct": "moisture_target_pct",
        "moisture_max_pct": "moisture_max_pct",
        "maximum_moisture_pct": "moisture_max_pct",
        "max_moisture_pct": "moisture_max_pct",
        "winter_moisture_target_pct": "winter_moisture_target_pct",
        "winter_comfort_threshold_pct": "winter_moisture_target_pct",
    }
    overrides: dict[str, float] = {}
    for source_key, target_key in aliases.items():
        if source_key not in raw_config:
            continue
        value = number(raw_config.get(source_key), None)
        if value is None:
            continue
        overrides[target_key] = round(clamp(value, 0.0, 100.0), 2)
    return overrides


def load_sensor_threshold_overrides(sensor_ids: set[int]) -> dict[int, dict[str, float]]:
    if not sensor_ids:
        return {}
    try:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT pot_id, criteria
                FROM sensor_location_recommendations
                WHERE pot_id = ANY(%(sensor_ids)s)
                """,
                {"sensor_ids": list(sensor_ids)},
            ).fetchall()
    except Exception:
        return {}

    thresholds: dict[int, dict[str, float]] = {}
    for row in rows:
        criteria = row.get("criteria") or {}
        if not isinstance(criteria, dict):
            continue
        overrides = normalize_sensor_threshold_config(criteria)
        if overrides:
            thresholds[int(row["pot_id"])] = overrides
    return thresholds


def sensor_control_summary_fields(
    pots: list[dict[str, Any]],
    sensor_context: dict[str, Any] | None,
) -> dict[str, Any]:
    control_ids = sensor_control_ids(sensor_context, pots)
    configured = bool(
        (sensor_context or {}).get("sensor_thresholds")
        or (sensor_context or {}).get("sensor_comfort_thresholds")
        or (sensor_context or {}).get("sensor_configs")
    )
    return {
        "controllerInputPolicy": "sensor_locations_only",
        "sensorDecisionPotCount": len(control_ids),
        "sensorThresholdPolicy": (
            "sensor_configurable_thresholds" if configured else "sensor_location_pot_thresholds"
        ),
    }


def with_sensor_key(record: dict[str, Any], pot: dict[str, Any], sensor_context: dict[str, Any]) -> dict[str, Any]:
    sensor_id = _sensor_id_for_pot(sensor_context, pot)
    enriched = dict(record)
    enriched["sensor_id"] = sensor_id
    threshold_overrides = sensor_threshold_overrides(sensor_context, sensor_id)
    if threshold_overrides:
        enriched["sensor_threshold_override"] = dict(threshold_overrides)
    if sensor_id != int(pot["id"]):
        enriched["associated_pot_id"] = int(pot["id"])
    return enriched


def _sensor_id_for_pot(sensor_context: dict[str, Any] | None, pot: dict[str, Any]) -> int:
    pot_id = int(pot["id"])
    association = (sensor_context or {}).get("associations", {}).get(pot_id)
    if association and association.get("sensor_id") is not None:
        return int(association["sensor_id"])
    return pot_id


def sensor_association_distance(pot: dict[str, Any], sensor_pot: dict[str, Any]) -> float:
    categorical_weights = {
        "plant_type_code": 3.0,
        "size_class": 1.8,
        "small_subtype": 0.8,
        "balcony_zone": 1.3,
        "rain_exposure": 1.3,
        "sun_exposure": 1.6,
        "wind_exposure": 1.2,
        "container_material": 0.8,
        "soil_profile": 1.0,
    }
    distance = 0.0
    for field, weight in categorical_weights.items():
        if pot.get(field) != sensor_pot.get(field):
            distance += weight

    distance += abs(float(pot["moisture_target_pct"]) - float(sensor_pot["moisture_target_pct"])) / 8.0
    distance += abs(float(pot["moisture_min_pct"]) - float(sensor_pot["moisture_min_pct"])) / 10.0
    distance += abs(float(pot["moisture_max_pct"]) - float(sensor_pot["moisture_max_pct"])) / 16.0
    distance += abs(math.log(max(float(pot["volume_l"]), 0.1) / max(float(sensor_pot["volume_l"]), 0.1))) * 0.9
    distance += abs(float(pot["evaporation_factor"]) - float(sensor_pot["evaporation_factor"])) * 2.0
    distance += abs(float(pot["retention_factor"]) - float(sensor_pot["retention_factor"])) * 2.0
    return distance
