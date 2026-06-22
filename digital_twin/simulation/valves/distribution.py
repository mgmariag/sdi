from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.irrigation_controller.delivery import apply_event_delivery
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.simulation.valves.zones import (
    is_valve_managed_pot,
    valve_managed_zone_pots,
)


def execute_valve_zone_distribution(
    pot_states: dict[int, PotState],
    zone_pots: dict[str, list[dict[str, Any]]],
    zone: str,
    current_date: date,
    hour_weather: dict[str, Any],
    decision_by_pot_id: dict[int, dict[str, Any]],
    decision_builder,
    request_builder,
    event_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    minimum_runtime_min = max(0.0, soil.number(event_metadata.get("minimum_valve_runtime_min"), 0.0))
    public_event_metadata = {
        key: value
        for key, value in event_metadata.items()
        if key != "minimum_valve_runtime_min"
    }
    request_items: list[tuple[dict[str, Any], PotState, dict[str, Any]]] = []
    for zone_pot in valve_managed_zone_pots(zone_pots, zone, current_date):
        zone_decision = decision_builder(zone_pot, dict(decision_by_pot_id[int(zone_pot["id"])]))
        request_event = _with_event_sensor_key(
            request_builder(zone_pot, hour_weather, zone_decision),
            zone_decision,
        )
        request_event.update(public_event_metadata)
        request_items.append((zone_pot, pot_states[int(zone_pot["id"])], request_event))

    runtime_request_sensor_ids = {
        int(sensor_id)
        for sensor_id in event_metadata.get("runtime_request_sensor_ids", [])
    }
    runtime_request_pot_ids = {
        int(pot_id)
        for pot_id in event_metadata.get("runtime_request_pot_ids", [])
    }
    if runtime_request_sensor_ids:
        runtime_request_items = [
            item
            for item in request_items
            if int(item[2].get("request_sensor_id", item[2].get("sensor_id", item[0]["id"]))) in runtime_request_sensor_ids
            and int(item[2].get("associated_pot_id", item[0]["id"])) == int(item[0]["id"])
            and int(item[2].get("sensor_id", item[0]["id"])) == int(item[0]["id"])
        ]
    elif runtime_request_pot_ids:
        runtime_request_items = [
            item
            for item in request_items
            if int(item[0]["id"]) in runtime_request_pot_ids
        ]
    else:
        runtime_request_items = request_items
    if (runtime_request_sensor_ids or runtime_request_pot_ids) and not runtime_request_items:
        runtime_request_items = request_items

    total_requested_ml = sum(float(event.get("requested_volume_ml", event.get("planned_volume_ml", 0.0)) or 0.0) for _, _, event in request_items)
    total_flow_ml_min = sum(float(event.get("flow_rate_ml_min") or 0.0) for _, _, event in request_items)
    runtime_requested_ml = sum(
        float(event.get("requested_volume_ml", event.get("planned_volume_ml", 0.0)) or 0.0)
        for _, _, event in runtime_request_items
    )
    runtime_request_flow_ml_min = sum(float(event.get("flow_rate_ml_min") or 0.0) for _, _, event in runtime_request_items)
    runtime_flow_ml_min = runtime_request_flow_ml_min if runtime_request_sensor_ids else total_flow_ml_min
    if runtime_requested_ml <= 0.0 or runtime_flow_ml_min <= 0.0 or total_flow_ml_min <= 0.0:
        return []

    runtime_min = runtime_requested_ml / runtime_flow_ml_min
    if runtime_min < minimum_runtime_min:
        return []
    delivered_total_ml = total_flow_ml_min * runtime_min
    events = []
    for zone_pot, state, event in request_items:
        flow_rate = max(float(event.get("flow_rate_ml_min") or zone_pot["drip_flow_ml_min"]), 1.0)
        delivered_ml = flow_rate * runtime_min
        event.update(
            {
                "zone_requested_volume_ml": round(total_requested_ml, 2),
                "zone_runtime_requested_volume_ml": round(runtime_requested_ml, 2),
                "zone_runtime_request_flow_ml_min": round(runtime_request_flow_ml_min, 2),
                "zone_runtime_request_sensor_ids": sorted(runtime_request_sensor_ids),
                "zone_runtime_request_pot_ids": [int(item[0]["id"]) for item in runtime_request_items],
                "zone_runtime_flow_ml_min": round(runtime_flow_ml_min, 2),
                "zone_delivered_volume_ml": round(delivered_total_ml, 2),
                "zone_total_flow_ml_min": round(total_flow_ml_min, 2),
                "valve_runtime_min": round(runtime_min, 3),
            }
        )
        events.append(apply_event_delivery(state, zone_pot, event, delivered_ml, runtime_min))
    return events


def _with_event_sensor_key(event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(event)
    sensor_id = int(decision.get("sensor_id", event["pot_id"]))
    enriched["sensor_id"] = sensor_id
    enriched["request_sensor_id"] = sensor_id
    if decision.get("associated_pot_id") is not None:
        enriched["associated_pot_id"] = int(decision["associated_pot_id"])
    return enriched

def zone_execution_decision_map(
    decision_by_pot_id: dict[int, dict[str, Any]],
    zone_pots: dict[str, list[dict[str, Any]]],
    zone: str,
    current_date: date,
    trigger_decisions: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    execution_decisions = dict(decision_by_pot_id)
    if not trigger_decisions:
        return execution_decisions

    template = max(
        trigger_decisions,
        key=lambda decision: float(decision.get("priority_score") or decision.get("predicted_probability") or 0.0),
    )
    source_pot_id = int(template["pot_id"])
    source_sensor_id = int(template.get("sensor_id", source_pot_id))
    for zone_pot in valve_managed_zone_pots(zone_pots, zone, current_date):
        pot_id = int(zone_pot["id"])
        if pot_id in execution_decisions:
            continue
        passive = dict(template)
        passive["pot_id"] = pot_id
        passive["pot_code"] = zone_pot.get("pot_code")
        passive["sensor_id"] = source_sensor_id
        if source_sensor_id != pot_id:
            passive["associated_pot_id"] = pot_id
        else:
            passive.pop("associated_pot_id", None)
        passive["should_irrigate"] = False
        passive["reason_code"] = "valve_zone_passive_delivery"
        passive["reason_detail"] = (
            f"Valve zone {zone} is controlled by sensor pot {template.get('pot_code') or source_pot_id}; "
            "this pot receives physical valve delivery but is not used as an independent decision input."
        )
        passive["controller_source_pot_id"] = source_pot_id
        passive["controller_source_sensor_id"] = source_sensor_id
        passive["controller_input_policy"] = "sensor_locations_only"
        execution_decisions[pot_id] = passive
    return execution_decisions


def apply_cold_month_indoor_skip(decision: dict[str, Any], pot: dict[str, Any], day: date) -> dict[str, Any]:
    if decision.get("should_irrigate") and not is_valve_managed_pot(pot, day):
        skipped = dict(decision)
        skipped["should_irrigate"] = False
        skipped["reason_code"] = "winter_indoor_not_valve_managed"
        skipped["reason_detail"] = (
            "Skipped because the pot is indoors from November through March; "
            "indoor irrigation is not implemented yet."
        )
        return skipped
    return decision


def baseline_zone_dose_factor(trigger_decisions: list[dict[str, Any]]) -> float:
    return max((float(decision.get("dose_factor") or 1.0) for decision in trigger_decisions), default=1.0)


def trigger_pot_ids(trigger_decisions: list[dict[str, Any]]) -> list[int]:
    return [int(decision["pot_id"]) for decision in trigger_decisions]


def trigger_sensor_ids(trigger_decisions: list[dict[str, Any]]) -> list[int]:
    return sorted({
        int(decision.get("sensor_id", decision["pot_id"]))
        for decision in trigger_decisions
    })


def trigger_pot_codes(trigger_decisions: list[dict[str, Any]]) -> list[str]:
    return [decision["pot_code"] for decision in trigger_decisions if decision.get("pot_code")]


def sparse_zone_dose_factor(
    trigger_decisions: list[dict[str, Any]],
    sample_interval_hours: int,
    sample_now: bool,
) -> float:
    baseline_factor = baseline_zone_dose_factor(trigger_decisions)
    if sample_now or sample_interval_hours <= 24:
        return baseline_factor
    if sample_interval_hours <= 48:
        return min(baseline_factor, 0.5)
    return min(baseline_factor, 0.75)

