from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.metrics import uses_hourly_chart
from digital_twin.simulation.state.lookback import (
    experiment_source,
    sensor_summary_fields,
)
from digital_twin.simulation.valves.rollups import result_pot_usage_l


def chart_entries_for_range(
    start_date: date,
    end_date: date,
    daily_entries: list[dict[str, Any]],
    hourly_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if uses_hourly_chart(start_date, end_date) and hourly_entries:
        return hourly_entries
    return daily_entries


def pot_info_entries(
    pots: list[dict[str, Any]],
    usage_by_field: dict[str, dict[int, float]] | None = None,
) -> list[dict[str, Any]]:
    usage_by_field = usage_by_field or {}
    return [
        _with_pot_usage_fields(
            {
                "pot_id": pot["id"],
                "pot_code": pot["pot_code"],
                "label": pot["label"],
                "size_class": pot["size_class"],
                "small_subtype": pot.get("small_subtype") or "",
                "plant_type_code": pot["plant_type_code"],
                "plant_type_label": pot.get("plant_type_label") or pot["plant_type_code"],
                "balcony_zone": pot["balcony_zone"],
                "rain_exposure": pot.get("rain_exposure", "partially_exposed"),
                "sun_exposure": pot["sun_exposure"],
                "wind_exposure": pot["wind_exposure"],
                "container_material": pot["container_material"],
                "soil_profile": pot["soil_profile"],
                "drip_flow_ml_min": float(pot["drip_flow_ml_min"]),
                "cycle_soak_enabled": bool(pot["cycle_soak_enabled"]),
                "moisture_min_pct": float(pot["moisture_min_pct"]),
                "moisture_target_pct": float(pot["moisture_target_pct"]),
                "moisture_max_pct": float(pot["moisture_max_pct"]),
            },
            pot["id"],
            usage_by_field,
        )
        for pot in pots
    ]


def _with_pot_usage_fields(
    row: dict[str, Any],
    pot_id: int,
    usage_by_field: dict[str, dict[int, float]],
) -> dict[str, Any]:
    for field, usage_by_pot in usage_by_field.items():
        row[field] = round(float(usage_by_pot.get(pot_id, 0.0)), 2)
    return row


def event_water_usage_l_by_pot(events: list[dict[str, Any]]) -> dict[int, float]:
    usage: dict[int, float] = {}
    for event in events:
        pot_id = int(event["pot_id"])
        usage[pot_id] = usage.get(pot_id, 0.0) + float(event.get("planned_volume_ml", 0.0)) / 1000.0
    return {pot_id: round(value, 2) for pot_id, value in usage.items()}


def experiment_result(
    *,
    entries: list[dict[str, Any]],
    chart_entries: list[dict[str, Any]],
    summary: dict[str, Any],
    pots: list[dict[str, Any]],
    valve_rollup: dict[str, list[dict[str, Any]]],
    decisions: list[dict[str, Any]],
    events: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": pot_info_entries(
            pots,
            {"period_water_usage_l": event_water_usage_l_by_pot(events)},
        ),
        "sampleDecisions": valve_rollup["decisions"][:200],
        "sampleEvents": valve_rollup["events"][:200],
        "samplePotDecisions": decisions[:200],
        "samplePotEvents": events[:200],
        "sampleAlerts": alerts[:200],
    }
    if extra_fields:
        result.update(extra_fields)
    return result


def average_moisture_from_state_payload(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    moistures = [
        float(item["moisture"])
        for item in payload.values()
        if isinstance(item, dict) and item.get("moisture") is not None
    ]
    if not moistures:
        return None
    return round(sum(moistures) / len(moistures), 2)


def comparison_pot_info_entries(
    pots: list[dict[str, Any]],
    baseline_result: dict[str, Any],
    comparison_result: dict[str, Any],
    comparison_field: str,
) -> list[dict[str, Any]]:
    comparison_usage = result_pot_usage_l(comparison_result)
    return pot_info_entries(
        pots,
        {
            "baseline_water_usage_l": result_pot_usage_l(baseline_result),
            comparison_field: comparison_usage,
            "period_water_usage_l": comparison_usage,
        },
    )


def daily_summary(
    entries: list[dict[str, Any]],
    pots: list[dict[str, Any]],
    weather_rows: list[dict[str, Any]],
    total_water_ml: float,
    total_irrigation_events: int,
    total_irrigation_decisions: int,
    alerts: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    sensor_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total_days = len(entries)
    summary = {
        "totalEntries": total_days,
        "daysAnalyzed": total_days,
        "potsAnalyzed": len(pots),
        "weatherRows": len(weather_rows),
        "irrigationEvents": sum(int(entry.get("irrigation_events") or 0) for entry in entries),
        "valveRuns": sum(int(entry.get("valve_runs", entry.get("irrigation_events", 0)) or 0) for entry in entries),
        "irrigationDecisions": total_irrigation_decisions,
        "totalWaterUsage": round(total_water_ml / 1000.0, 2),
        "averageDailyWaterUsage": round((total_water_ml / 1000.0) / max(total_days, 1), 2),
        "emergencyAlerts": len([alert for alert in alerts if alert["alert_type"] == "emergency_dryness"]),
        "wetAlerts": len([alert for alert in alerts if alert["alert_type"] == "too_wet_too_long"]),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "source": experiment_source(sensor_context),
    }
    if sensor_context is not None:
        summary.update(sensor_summary_fields(sensor_context))
    return summary
