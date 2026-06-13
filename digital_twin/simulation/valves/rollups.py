from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.domain.valves import VALVE_ZONE_DESIGN
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.valves.distribution import trigger_sensor_ids
from digital_twin.simulation.valves.zones import (
    pots_by_valve_zone,
    valve_managed_zone_pots,
    valve_number_for_zone,
)


def apply_valve_rollup_to_entries(
    entries: list[dict[str, Any]],
    detail_entries: list[dict[str, Any]],
    pots: list[dict[str, Any]],
    pot_decisions: list[dict[str, Any]],
    pot_events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    rollup = _valve_rollup(pots, pot_decisions, pot_events)
    apply_valve_counts(entries, rollup, hourly=False)
    apply_valve_counts(detail_entries, rollup, hourly=True)
    return rollup


def _valve_rollup(
    pots: list[dict[str, Any]],
    pot_decisions: list[dict[str, Any]],
    pot_events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    pot_by_id = {int(pot["id"]): pot for pot in pots}
    zone_pots = pots_by_valve_zone(pots)
    decision_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for decision in pot_decisions:
        pot = pot_by_id.get(int(decision["pot_id"]))
        if not pot:
            continue
        key = (
            decision["date"],
            decision["slot"],
            _local_timestamp_key(decision["decided_at"]),
            pot["balcony_zone"],
        )
        decision_groups.setdefault(key, []).append(decision)

    valve_decisions = [
        _valve_decision_from_group(key, group, pot_by_id, zone_pots)
        for key, group in decision_groups.items()
    ]
    valve_decisions.sort(key=lambda item: (item["decided_at"], item["valve_number"]))

    event_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for event in pot_events:
        pot = pot_by_id.get(int(event["pot_id"]))
        if not pot:
            continue
        key = (
            event["date"],
            event["slot"],
            _local_timestamp_key(event["scheduled_start_at"]),
            pot["balcony_zone"],
        )
        event_groups.setdefault(key, []).append(event)

    valve_events = [
        valve_event_from_group(key, group, pot_by_id, zone_pots)
        for key, group in event_groups.items()
    ]
    valve_events.sort(key=lambda item: (item["scheduled_start_at"], item["priority_rank"], item["valve_number"]))
    return {"decisions": valve_decisions, "events": valve_events}


def _valve_decision_from_group(
    key: tuple[str, str, str, str],
    group: list[dict[str, Any]],
    pot_by_id: dict[int, dict[str, Any]],
    zone_pots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    decision_date, slot, decided_key, zone = key
    should = [decision for decision in group if decision.get("should_irrigate")]
    relevant = should or group
    priority = max((_valve_decision_priority(decision, pot_by_id[int(decision["pot_id"])]) for decision in relevant), default=0.0)
    moisture_values = [float(decision.get("current_moisture_pct") or 0.0) for decision in group]
    target_values = [float(decision.get("target_moisture_pct") or 0.0) for decision in group]
    valve_number = valve_number_for_zone(zone)
    managed = valve_managed_zone_pots(zone_pots, zone, date.fromisoformat(decision_date))
    triggered_pot_ids = [int(decision["pot_id"]) for decision in should]
    triggered_sensor_ids = trigger_sensor_ids(should)
    triggered_pot_codes = [decision.get("pot_code") for decision in should if decision.get("pot_code")]
    affected_pot_ids = [int(pot["id"]) for pot in managed] if should else []
    affected_pot_codes = [pot["pot_code"] for pot in managed] if should else []
    reason_detail = (
        f"Valve V{valve_number} controls {zone}; {len(triggered_pot_ids)} of {len(group)} evaluated pots require irrigation, "
        f"so all {len(affected_pot_ids)} managed pots are watered."
        if should
        else f"Valve V{valve_number} controls {zone}; no evaluated pot requires irrigation."
    )
    return {
        "valve_number": valve_number,
        "valve_zone": zone,
        "decided_at": datetime.fromisoformat(decided_key).replace(tzinfo=LOCAL_TZ).isoformat(),
        "date": decision_date,
        "slot": slot,
        "should_irrigate": bool(should),
        "reason_code": "valve_zone_required" if should else "valve_zone_not_required",
        "reason_detail": reason_detail,
        "current_moisture_pct": round(min(moisture_values), 2) if moisture_values else None,
        "target_moisture_pct": round(sum(target_values) / max(len(target_values), 1), 2) if target_values else None,
        "weather_hourly_id": group[0].get("weather_hourly_id"),
        "managed_pots": len(managed),
        "evaluated_pots": len(group),
        "affected_pots": len(affected_pot_ids),
        "affected_pot_ids": affected_pot_ids,
        "affected_pot_codes": affected_pot_codes,
        "trigger_pots": len(triggered_pot_ids),
        "trigger_pot_ids": triggered_pot_ids,
        "trigger_sensor_ids": triggered_sensor_ids,
        "trigger_pot_codes": triggered_pot_codes,
        "priority_score": round(priority, 2),
        "decision_level": "valve_zone",
    }


def _average_event_field(group: list[dict[str, Any]], field: str) -> float | None:
    values = [
        float(event[field])
        for event in group
        if event.get(field) is not None
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _weighted_average_event_field(group: list[dict[str, Any]], field: str, weight_field: str = "affected_pots") -> float | None:
    weighted_total = 0.0
    weight_total = 0.0
    for event in group:
        if event.get(field) is None:
            continue
        weight = max(float(event.get(weight_field) or 1.0), 1.0)
        weighted_total += float(event[field]) * weight
        weight_total += weight
    if weight_total <= 0.0:
        return None
    return round(weighted_total / weight_total, 2)


def valve_event_from_group(
    key: tuple[str, str, str, str],
    group: list[dict[str, Any]],
    pot_by_id: dict[int, dict[str, Any]],
    zone_pots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    event_date, slot, scheduled_key, zone = key
    valve_number = valve_number_for_zone(zone)
    managed = valve_managed_zone_pots(zone_pots, zone, date.fromisoformat(event_date))
    flow_rate = sum(float(event.get("flow_rate_ml_min") or 0.0) for event in group)
    if flow_rate <= 0.0:
        flow_rate = sum(float(pot["drip_flow_ml_min"]) for pot in managed)
    planned_volume = sum(float(event.get("planned_volume_ml") or 0.0) for event in group)
    requested_volume = sum(float(event.get("requested_volume_ml", event.get("planned_volume_ml", 0.0)) or 0.0) for event in group)
    delivered_volume = sum(float(event.get("delivered_volume_ml", event.get("planned_volume_ml", 0.0)) or 0.0) for event in group)
    duration_min = max(
        (float(event.get("valve_runtime_min") or event.get("duration_min") or 0.0) for event in group),
        default=0.0,
    )
    if duration_min <= 0.0:
        duration_min = planned_volume / max(flow_rate, 1.0)
    scheduled_start = datetime.fromisoformat(scheduled_key).replace(tzinfo=LOCAL_TZ)
    scheduled_end = scheduled_start + timedelta(minutes=duration_min)
    affected_pots = [pot_by_id[int(event["pot_id"])] for event in group if int(event["pot_id"]) in pot_by_id]
    trigger_pot_ids = sorted({
        int(pot_id)
        for event in group
        for pot_id in event.get("zone_trigger_pot_ids", [])
    })
    trigger_sensor_ids = sorted({
        int(sensor_id)
        for event in group
        for sensor_id in event.get("zone_trigger_sensor_ids", event.get("zone_trigger_pot_ids", []))
    })
    trigger_pot_codes = [
        pot_by_id[pot_id]["pot_code"]
        for pot_id in trigger_pot_ids
        if pot_id in pot_by_id
    ]
    priority = max((_valve_event_priority(event, pot_by_id[int(event["pot_id"])]) for event in group), default=0.0)
    return {
        "valve_number": valve_number,
        "valve_zone": zone,
        "date": event_date,
        "slot": slot,
        "scheduled_start_at": scheduled_start.isoformat(),
        "scheduled_end_at": scheduled_end.isoformat(),
        "flow_rate_ml_min": round(flow_rate, 2),
        "flow_rate_l_min": round(flow_rate / 1000.0, 3),
        "planned_volume_ml": round(planned_volume, 2),
        "planned_volume_l": round(planned_volume / 1000.0, 3),
        "requested_volume_ml": round(requested_volume, 2),
        "requested_volume_l": round(requested_volume / 1000.0, 3),
        "delivered_volume_ml": round(delivered_volume, 2),
        "delivered_volume_l": round(delivered_volume / 1000.0, 3),
        "delivery_error_ml": round(delivered_volume - requested_volume, 2),
        "affected_pre_moisture_pct": _average_event_field(group, "pre_delivery_moisture_pct"),
        "affected_post_moisture_pct": _average_event_field(group, "post_delivery_moisture_pct"),
        "affected_moisture_gain_pct": _average_event_field(group, "delivery_moisture_gain_pct"),
        "duration_min": round(duration_min, 1),
        "physical_distribution_policy": "valve_runtime_x_pot_drip_flow",
        "per_pot_distribution": _valve_pot_distribution(group),
        "cycle_count": 1,
        "soak_pause_min": 0,
        "managed_pots": len(managed),
        "affected_pots": len(group),
        "affected_pot_ids": [int(pot["id"]) for pot in affected_pots],
        "affected_pot_codes": [pot["pot_code"] for pot in affected_pots],
        "trigger_pots": len(trigger_pot_ids),
        "trigger_pot_ids": trigger_pot_ids,
        "trigger_sensor_ids": trigger_sensor_ids,
        "trigger_pot_codes": trigger_pot_codes,
        "priority_rank": 0 if any(event.get("priority_rank") == 0 for event in group) else 1,
        "priority_score": round(priority, 2),
        "decision_level": "valve_zone",
    }


def _valve_pot_distribution(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    distribution = []
    for event in sorted(group, key=lambda item: int(item["pot_id"])):
        row = {
            "pot_id": int(event["pot_id"]),
            "sensor_id": int(event.get("sensor_id", event["pot_id"])),
            "request_sensor_id": int(event.get("request_sensor_id", event.get("sensor_id", event["pot_id"]))),
            "pot_code": event.get("pot_code"),
            "flow_rate_ml_min": round(float(event.get("flow_rate_ml_min") or 0.0), 2),
            "requested_volume_ml": round(float(event.get("requested_volume_ml", event.get("planned_volume_ml", 0.0)) or 0.0), 2),
            "delivered_volume_ml": round(float(event.get("delivered_volume_ml", event.get("planned_volume_ml", 0.0)) or 0.0), 2),
            "delivery_error_ml": round(float(event.get("delivery_error_ml") or 0.0), 2),
            "delivery_ratio": event.get("delivery_ratio"),
        }
        if event.get("pre_delivery_moisture_pct") is not None:
            row["pre_delivery_moisture_pct"] = round(float(event["pre_delivery_moisture_pct"]), 2)
        if event.get("post_delivery_moisture_pct") is not None:
            row["post_delivery_moisture_pct"] = round(float(event["post_delivery_moisture_pct"]), 2)
        if event.get("delivery_moisture_gain_pct") is not None:
            row["delivery_moisture_gain_pct"] = round(float(event["delivery_moisture_gain_pct"]), 2)
        distribution.append(row)
    return distribution


def apply_valve_counts(entries: list[dict[str, Any]], rollup: dict[str, list[dict[str, Any]]], hourly: bool) -> None:
    decisions_by_key: dict[str, list[dict[str, Any]]] = {}
    events_by_key: dict[str, list[dict[str, Any]]] = {}
    for decision in rollup["decisions"]:
        key = _local_timestamp_key(decision["decided_at"]) if hourly else decision["date"]
        decisions_by_key.setdefault(key, []).append(decision)
    for event in rollup["events"]:
        key = _local_timestamp_key(event["scheduled_start_at"]) if hourly else event["date"]
        events_by_key.setdefault(key, []).append(event)

    for entry in entries:
        key = _local_timestamp_key(entry["timestamp"]) if hourly else entry["date"]
        entry_decisions = decisions_by_key.get(key, [])
        entry_events = events_by_key.get(key, [])
        entry["irrigation_decisions"] = len(entry_decisions)
        entry["valve_runs"] = len(entry_events)
        entry["irrigation_events"] = len({_local_timestamp_key(event["scheduled_start_at"]) for event in entry_events})
        entry["irrigation_active"] = bool(entry_events)
        entry["irrigated_pots"] = sum(int(event.get("affected_pots") or 0) for event in entry_events)
        entry["activated_valve_numbers"] = _activated_valve_numbers(entry_events)
        entry["activated_valves"] = activated_valve_label(entry_events)
        entry["valves"] = entry_valves(entry_events)
        entry["decision_level"] = "valve_zone"
        if entry_events:
            entry["irrigation_start_at"] = min(str(event["scheduled_start_at"]) for event in entry_events)
            entry["irrigation_end_at"] = max(str(event["scheduled_end_at"]) for event in entry_events)
            entry["planned_volume_l"] = round(
                sum(float(event.get("planned_volume_ml") or 0.0) for event in entry_events) / 1000.0,
                2,
            )
            irrigated_pre_moisture = _weighted_average_event_field(entry_events, "affected_pre_moisture_pct")
            irrigated_post_moisture = _weighted_average_event_field(entry_events, "affected_post_moisture_pct")
            irrigated_gain = _weighted_average_event_field(entry_events, "affected_moisture_gain_pct")
            if irrigated_pre_moisture is not None:
                entry["irrigated_pre_moisture"] = irrigated_pre_moisture
            if irrigated_post_moisture is not None:
                entry["irrigated_post_moisture"] = irrigated_post_moisture
            if irrigated_gain is not None:
                entry["irrigated_moisture_gain"] = irrigated_gain


def comparison_window_fields(prefix: str, entry: dict[str, Any]) -> dict[str, Any]:
    fields = {}
    if entry.get("activated_valves") is not None:
        fields[f"{prefix}_activated_valves"] = entry["activated_valves"]
    if entry.get("activated_valve_numbers") is not None:
        fields[f"{prefix}_activated_valve_numbers"] = entry["activated_valve_numbers"]
    if entry.get("valves") is not None:
        fields[f"{prefix}_valves"] = entry["valves"]
    if entry.get("irrigation_start_at"):
        fields[f"{prefix}_irrigation_start_at"] = entry["irrigation_start_at"]
    if entry.get("irrigation_end_at"):
        fields[f"{prefix}_irrigation_end_at"] = entry["irrigation_end_at"]
    if entry.get("planned_volume_l") is not None:
        fields[f"{prefix}_planned_volume_l"] = entry["planned_volume_l"]
    if entry.get("pre_irrigation_moisture") is not None:
        fields[f"{prefix}_pre_irrigation_moisture"] = entry["pre_irrigation_moisture"]
    if entry.get("post_irrigation_moisture") is not None:
        fields[f"{prefix}_post_irrigation_moisture"] = entry["post_irrigation_moisture"]
    if entry.get("irrigated_pre_moisture") is not None:
        fields[f"{prefix}_irrigated_pre_moisture"] = entry["irrigated_pre_moisture"]
    if entry.get("irrigated_post_moisture") is not None:
        fields[f"{prefix}_irrigated_post_moisture"] = entry["irrigated_post_moisture"]
    if entry.get("irrigated_moisture_gain") is not None:
        fields[f"{prefix}_irrigated_moisture_gain"] = entry["irrigated_moisture_gain"]
    return fields


def _activated_valve_numbers(events: list[dict[str, Any]]) -> list[int]:
    numbers = {
        int(event["valve_number"])
        for event in events
        if event.get("valve_number") is not None
    }
    return sorted(numbers)


def entry_valves(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valves: dict[int, dict[str, Any]] = {}
    for event in events:
        valve_number = event.get("valve_number")
        if valve_number is None:
            continue
        number = int(valve_number)
        planned_volume_l = event.get("planned_volume_l")
        if planned_volume_l is None:
            planned_volume_l = float(event.get("planned_volume_ml") or 0.0) / 1000.0
        current = valves.setdefault(
            number,
            {
                "valve_number": number,
                "valve_zone": event.get("valve_zone"),
                "planned_volume_l": 0.0,
                "duration_min": 0.0,
            },
        )
        current["planned_volume_l"] += float(planned_volume_l or 0.0)
        current["duration_min"] += float(event.get("duration_min") or 0.0)

    return [
        {
            **valve,
            "planned_volume_l": round(float(valve["planned_volume_l"]), 2),
            "duration_min": round(float(valve["duration_min"]), 1),
        }
        for valve in sorted(valves.values(), key=lambda item: item["valve_number"])
    ]


def activated_valve_label(events: list[dict[str, Any]]) -> str:
    numbers = _activated_valve_numbers(events)
    if not numbers:
        return "none"

    configured_numbers = sorted(int(item["valve_number"]) for item in VALVE_ZONE_DESIGN)
    if numbers == configured_numbers:
        return "all"

    ranges = []
    start = numbers[0]
    previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(_valve_range_label(start, previous))
        start = previous = number
    ranges.append(_valve_range_label(start, previous))
    return ", ".join(ranges)


def _valve_range_label(start: int, end: int) -> str:
    return f"V{start}" if start == end else f"V{start}-V{end}"


def _valve_decision_priority(decision: dict[str, Any], pot: dict[str, Any]) -> float:
    moisture = float(decision.get("current_moisture_pct") or pot["moisture_target_pct"])
    target = float(decision.get("target_moisture_pct") or pot["moisture_target_pct"])
    return _valve_priority_score(pot, moisture, target)


def _valve_event_priority(event: dict[str, Any], pot: dict[str, Any]) -> float:
    target = float(pot["moisture_target_pct"])
    volume_bonus = min(20.0, float(event.get("planned_volume_ml") or 0.0) / 100.0)
    return _valve_priority_score(pot, target - 8.0, target) + volume_bonus


def _valve_priority_score(pot: dict[str, Any], moisture: float, target: float) -> float:
    min_moisture = float(pot["moisture_min_pct"])
    urgency = max(0.0, min_moisture - moisture)
    deficit = max(0.0, target - moisture)
    sun_bonus = {"reflected_heat": 6.0, "full": 4.0, "partial": 1.5, "shade": 0.0}.get(str(pot.get("sun_exposure") or "partial"), 1.5)
    water_need_bonus = {"high": 4.0, "medium": 2.0, "low": 0.0}.get(str(pot.get("water_need_level") or "medium"), 2.0)
    heat_bonus = 2.0 if pot.get("heat_sensitive") else 0.0
    return urgency * 4.0 + deficit + sun_bonus + water_need_bonus + heat_bonus

def result_pot_usage_l(result: dict[str, Any], field: str = "period_water_usage_l") -> dict[int, float]:
    usage: dict[int, float] = {}
    for pot in result.get("pots", []):
        pot_id = pot.get("pot_id")
        if pot_id is not None:
            usage[int(pot_id)] = float(pot.get(field, 0.0))
    return usage

def _local_timestamp_key(value: str | datetime) -> str:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value)
    else:
        local_value = value
    if local_value.tzinfo is not None:
        local_value = local_value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return local_value.replace(microsecond=0).isoformat()
