from __future__ import annotations

import random
import math
import time as perf_time
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_twin.db.connection import get_connection
from digital_twin.db.schema import initialize_database
from digital_twin.domain.irrigation_methods import VALVE_ZONE_DESIGN, VALVE_ZONE_ORDER
from digital_twin.experiments.anfis import ANFIS, probability_category
from digital_twin.simulation.dto import (
    ANFIS_DECISION_THRESHOLD,
    ExperimentSnapshot,
    HOURLY_CHART_MAX_RANGE_DAYS,
    LOCAL_TZ,
    PotState,
)
from digital_twin.simulation.soil_model import (
    clamp as _clamp,
    local_observed_at as _local_observed_at,
    number as _number,
    season as _season,
    sun_factor as _sun_factor,
    wind_factor as _wind_factor,
)
from digital_twin.simulation.weather_model import (
    _load_weather,
    _raise_if_missing_historical_weather,
    _with_estimated_future_weather,
)
from digital_twin.simulation.irrigation_controller import (
    _alert_row,
    _apply_fuzzy_prescribed_event,
    _apply_irrigation_event,
    _apply_planned_volume,
    _decision_slot,
    _is_emergency_dryness,
    _is_outdoor,
    _make_fao_pm_decision,
    _make_fuzzy_dt_decision,
    _make_irrigation_decision,
    _precipitation_last_days,
    _threshold_for_pot,
    _upcoming_freeze,
)
from digital_twin.services.sensor_readings import (
    ACTUAL_SENSOR_SOURCE,
    DEFAULT_SENSOR_SOURCE,
    ensure_sensor_readings_for_experiment_range,
    load_sensor_readings_for_experiment,
)


def load_experiment_snapshot(start_date: date, end_date: date) -> ExperimentSnapshot:
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    initialize_database()
    pots = _load_active_pots()
    if not pots:
        raise ValueError("No active pots found in the database")

    sensor_context = _load_sensor_context(start_date, end_date, pots)
    weather_start = _snapshot_weather_start(start_date, sensor_context)
    weather_rows = _load_weather(weather_start, end_date)
    _raise_if_missing_historical_weather(weather_rows, start_date, end_date)
    weather_rows, estimated_weather_rows = _with_estimated_future_weather(weather_rows, weather_start, end_date)
    selected_weather_rows = [
        row for row in weather_rows
        if start_date <= _local_observed_at(row).date() <= end_date
    ]
    if not selected_weather_rows:
        raise ValueError("No stored weather rows found for the selected date range")
    initial_pot_states = _initial_pot_states(pots)
    weather_by_day = _group_weather_by_day(weather_rows)
    _prime_future_states(
        initial_pot_states,
        pots,
        sensor_context,
        start_date,
        weather_by_day,
    )
    day_profiles = _day_profiles_for_range(start_date, end_date, weather_by_day)

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
        initial_pot_states=initial_pot_states,
        estimated_weather_rows=estimated_weather_rows,
        loaded_at=datetime.now(LOCAL_TZ),
    )


def _snapshot_weather_start(start_date: date, sensor_context: dict[str, Any]) -> date:
    latest_state_at = sensor_context.get("latest_state_at")
    if sensor_context.get("future_dates") and latest_state_at:
        latest_state_date = latest_state_at.date()
        if latest_state_date < start_date:
            return latest_state_date
    return start_date


def _resolve_snapshot(
    start_date: date,
    end_date: date,
    snapshot: ExperimentSnapshot | None = None,
) -> ExperimentSnapshot:
    if snapshot is None:
        return load_experiment_snapshot(start_date, end_date)
    if snapshot.start_date != start_date or snapshot.end_date != end_date:
        raise ValueError("Experiment snapshot does not match the requested configuration")
    return snapshot


def _uses_hourly_chart(start_date: date, end_date: date) -> bool:
    return (end_date - start_date).days < HOURLY_CHART_MAX_RANGE_DAYS


def _chart_entries_for_range(
    start_date: date,
    end_date: date,
    daily_entries: list[dict[str, Any]],
    hourly_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if _uses_hourly_chart(start_date, end_date) and hourly_entries:
        return hourly_entries
    return daily_entries


def _add_chart_summary(summary: dict[str, Any], chart_entries: list[dict[str, Any]], start_date: date, end_date: date) -> None:
    summary["chartGranularity"] = "hourly" if _uses_hourly_chart(start_date, end_date) and chart_entries else "daily"
    summary["chartEntryCount"] = len(chart_entries)


def run_daily_irrigation_experiment(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    """Run a database-backed daily irrigation experiment.

    The experiment reads stored weather and seeded pot inventory from Postgres.
    It returns daily aggregate rows for charting and keeps the previous synthetic
    simulation untouched.
    """
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    weather_rows = snapshot.selected_weather_rows
    pots = snapshot.pots
    sensor_context = snapshot.sensor_context
    weather_by_day = snapshot.weather_by_day
    pot_states = _copy_pot_states(snapshot.initial_pot_states)

    entries = []
    detail_entries = []
    decisions = []
    events = []
    alerts = []

    total_water_ml = 0.0
    total_irrigation_events = 0
    total_irrigation_decisions = 0

    current_date = start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        day_profile = snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0

            for pot in pots:
                state = pot_states[pot["id"]]
                _apply_hourly_environment(state, pot, hour_weather, day_profile, observed_local.date())
                _apply_sensor_reading(state, pot, current_date, observed_local, sensor_context)

                slot = _decision_slot(current_date, observed_local, day_profile)
                if slot is None:
                    if _is_emergency_dryness(state, pot, current_date, observed_local):
                        alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                        daily_alerts += 1
                        hourly_alerts += 1
                    continue

                decision = _with_sensor_key(
                    _make_irrigation_decision(state, pot, hour_weather, day_profile, slot),
                    pot,
                    sensor_context,
                )
                decisions.append(decision)
                daily_decisions += 1
                hourly_decisions += 1

                if decision["should_irrigate"]:
                    event = _with_event_sensor_key(_apply_irrigation_event(state, pot, hour_weather, decision), decision)
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]

                if state.moisture > pot["moisture_max_pct"]:
                    state.too_wet_hours += 1
                    if state.too_wet_hours == 24:
                        alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                        daily_alerts += 1
                        hourly_alerts += 1
                else:
                    state.too_wet_hours = 0

            if _uses_hourly_chart(start_date, end_date):
                detail_entries.append(
                    _hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        pot_states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        _hourly_line_metadata(sensor_context, current_date, observed_local, hour_weather),
                    )
                )

        moistures = [state.moisture for state in pot_states.values()]
        avg_moisture = sum(moistures) / max(len(moistures), 1)
        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions

        entries.append(
            {
                "date": current_date.isoformat(),
                "timestamp": datetime.combine(current_date, time(12, 0), tzinfo=LOCAL_TZ).isoformat(),
                "day_label": current_date.strftime("%Y-%m-%d"),
                "chart_label": current_date.strftime("%Y-%m-%d"),
                "moisture": round(avg_moisture, 2),
                "average_moisture": round(avg_moisture, 2),
                "min_moisture": round(min(moistures), 2),
                "max_moisture": round(max(moistures), 2),
                "temperature": round(day_profile["avg_temperature_c"], 2),
                "max_temperature": round(day_profile["max_temperature_c"], 2),
                "humidity": round(day_profile["avg_humidity_pct"], 2),
                "cloud_cover_pct": round(day_profile["avg_cloud_cover_pct"], 2),
                "rain_prediction": day_profile["precipitation_mm"] >= 0.5,
                "rain_amount": round(day_profile["precipitation_mm"], 2),
                "wind_gust_kmh": round(day_profile["max_wind_gust_kmh"], 2),
                "heatwave_day": day_profile["heatwave_day"],
                "freeze_risk": day_profile["freeze_risk"],
                "irrigation_active": daily_events > 0,
                "irrigation_events": 1 if daily_events > 0 else 0,
                "valve_runs": daily_events,
                "irrigation_decisions": daily_decisions,
                "irrigated_pots": len({event["pot_id"] for event in events if event["date"] == current_date.isoformat()}),
                "alerts": daily_alerts,
                "water_usage_ml": round(daily_water_ml, 2),
                "water_usage_l": round(daily_water_ml / 1000.0, 2),
                **_daily_line_metadata(sensor_context, current_date, day_weather),
            }
        )
        current_date += timedelta(days=1)

    valve_rollup = _apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

    if persist:
        _persist_daily_results("baseline", start_date, end_date, decisions, events, alerts)

    total_days = len(entries)
    total_pots = len(pots)
    summary = {
        "totalEntries": total_days,
        "daysAnalyzed": total_days,
        "potsAnalyzed": total_pots,
        "weatherRows": len(weather_rows),
        "irrigationEvents": sum(int(entry.get("irrigation_events") or 0) for entry in entries),
        "valveRuns": sum(int(entry.get("valve_runs", entry.get("irrigation_events", 0)) or 0) for entry in entries),
        "irrigationDecisions": len(valve_rollup["decisions"]),
        "potIrrigationDecisions": total_irrigation_decisions,
        "potIrrigationActions": len(events),
        "decisionLevel": "valve_zone",
        "totalWaterUsage": round(total_water_ml / 1000.0, 2),
        "averageDailyWaterUsage": round((total_water_ml / 1000.0) / max(total_days, 1), 2),
        "emergencyAlerts": len([alert for alert in alerts if alert["alert_type"] == "emergency_dryness"]),
        "wetAlerts": len([alert for alert in alerts if alert["alert_type"] == "too_wet_too_long"]),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "source": _experiment_source(sensor_context),
    }
    summary.update(_sensor_summary_fields(sensor_context))
    chart_entries = _chart_entries_for_range(start_date, end_date, entries, detail_entries)
    _add_chart_summary(summary, chart_entries, start_date, end_date)

    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _pot_info_entries(
            pots,
            {"period_water_usage_l": _event_water_usage_l_by_pot(events)},
        ),
        "sampleDecisions": valve_rollup["decisions"][:200],
        "sampleEvents": valve_rollup["events"][:200],
        "samplePotDecisions": decisions[:200],
        "samplePotEvents": events[:200],
        "sampleAlerts": alerts[:200],
    }


def run_daily_sampling_experiment(
    start_date: date,
    end_date: date,
    sample_interval_days: int = 3,
    sample_interval_hours: int | None = None,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    """Compare full daily decisions with a sparse sensor sampling strategy."""
    start_time = perf_time.perf_counter()
    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    sample_interval_days = max(1, int(sample_interval_days))
    if sample_interval_hours is None:
        sample_interval_hours = sample_interval_days * 24
    else:
        sample_interval_hours = max(1, int(sample_interval_hours))
        sample_interval_days = max(1, round(sample_interval_hours / 24))
    baseline = run_daily_irrigation_experiment(
        start_date=start_date,
        end_date=end_date,
        persist=False,
        snapshot=snapshot,
    )
    sparse = _run_sparse_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        sample_interval_hours=sample_interval_hours,
        persist=persist,
        snapshot=snapshot,
    )

    baseline_by_date = {entry["date"]: entry for entry in baseline["entries"]}
    entries = []
    matches = 0
    mismatches = 0

    for sparse_entry in sparse["entries"]:
        baseline_entry = baseline_by_date.get(sparse_entry["date"])
        if baseline_entry is None:
            continue

        baseline_active = baseline_entry["irrigation_events"] > 0
        sparse_active = sparse_entry["irrigation_events"] > 0
        if baseline_active == sparse_active:
            matches += 1
        else:
            mismatches += 1

        entries.append(
            {
                "date": sparse_entry["date"],
                "timestamp": sparse_entry["timestamp"],
                "day_label": sparse_entry["day_label"],
                "chart_label": sparse_entry.get("chart_label", sparse_entry["day_label"]),
                "baseline_moisture": baseline_entry["average_moisture"],
                "sparse_moisture": sparse_entry["average_moisture"],
                "temperature": sparse_entry["temperature"],
                "max_temperature": sparse_entry["max_temperature"],
                "humidity": sparse_entry["humidity"],
                "cloud_cover_pct": sparse_entry["cloud_cover_pct"],
                "rain_prediction": sparse_entry["rain_prediction"],
                "rain_amount": sparse_entry["rain_amount"],
                "baseline_irrigation_active": baseline_active,
                "sparse_irrigation_active": sparse_active,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "sparse_irrigation_events": sparse_entry["irrigation_events"],
                "baseline_valve_runs": baseline_entry.get("valve_runs", baseline_entry["irrigation_events"]),
                "sparse_valve_runs": sparse_entry.get("valve_runs", sparse_entry["irrigation_events"]),
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "sparse_water_usage_l": sparse_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "sparse_water_usage_ml": sparse_entry["water_usage_ml"],
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
                "alerts": sparse_entry["alerts"],
                **_combined_line_metadata(baseline_entry, sparse_entry),
            }
        )

    baseline_chart_by_timestamp = {entry["timestamp"]: entry for entry in baseline.get("chartEntries", baseline["entries"])}
    chart_entries = []
    for sparse_entry in sparse.get("chartEntries", sparse["entries"]):
        baseline_entry = baseline_chart_by_timestamp.get(sparse_entry["timestamp"]) or baseline_by_date.get(sparse_entry["date"])
        if baseline_entry is None:
            continue
        baseline_active = baseline_entry["irrigation_events"] > 0
        sparse_active = sparse_entry["irrigation_events"] > 0
        chart_entries.append(
            {
                "date": sparse_entry["date"],
                "timestamp": sparse_entry["timestamp"],
                "day_label": sparse_entry["day_label"],
                "chart_label": sparse_entry.get("chart_label", sparse_entry["day_label"]),
                "baseline_moisture": baseline_entry["average_moisture"],
                "sparse_moisture": sparse_entry["average_moisture"],
                "temperature": sparse_entry["temperature"],
                "max_temperature": sparse_entry["max_temperature"],
                "humidity": sparse_entry["humidity"],
                "cloud_cover_pct": sparse_entry["cloud_cover_pct"],
                "rain_prediction": sparse_entry["rain_prediction"],
                "rain_amount": sparse_entry["rain_amount"],
                "baseline_irrigation_active": baseline_active,
                "sparse_irrigation_active": sparse_active,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "sparse_irrigation_events": sparse_entry["irrigation_events"],
                "baseline_valve_runs": baseline_entry.get("valve_runs", baseline_entry["irrigation_events"]),
                "sparse_valve_runs": sparse_entry.get("valve_runs", sparse_entry["irrigation_events"]),
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "sparse_water_usage_l": sparse_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "sparse_water_usage_ml": sparse_entry["water_usage_ml"],
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
                "alerts": sparse_entry["alerts"],
                **_combined_line_metadata(baseline_entry, sparse_entry),
            }
        )

    total_days = len(entries)
    baseline_summary = baseline["summary"]
    sparse_summary = sparse["summary"]
    summary = {
        "totalEntries": total_days,
        "daysAnalyzed": total_days,
        "potsAnalyzed": sparse_summary["potsAnalyzed"],
        "sample_interval_days": sample_interval_days,
        "sample_interval_hours": sample_interval_hours,
        "sample_interval": sample_interval_days,
        "accuracy_percent": round(matches / max(total_days, 1) * 100.0, 2),
        "mismatch_days": mismatches,
        "mismatch_steps": mismatches,
        "baseline_total_water_usage_l": baseline_summary["totalWaterUsage"],
        "sparse_total_water_usage_l": sparse_summary["totalWaterUsage"],
        "baseline_irrigation_event_count": baseline_summary["irrigationEvents"],
        "sparse_irrigation_event_count": sparse_summary["irrigationEvents"],
        "baseline_valve_run_count": baseline_summary.get("valveRuns", baseline_summary["irrigationEvents"]),
        "sparse_valve_run_count": sparse_summary.get("valveRuns", sparse_summary["irrigationEvents"]),
        "baseline_irrigation_decisions": baseline_summary["irrigationDecisions"],
        "sparse_irrigation_decisions": sparse_summary["irrigationDecisions"],
        "sampledWeatherRows": sparse_summary.get("sampledWeatherRows", 0),
        "samplingDataPolicy": sparse_summary.get("samplingDataPolicy", "sensor-and-weather-sampled"),
        "decisionLevel": "valve_zone",
        "execution_time_seconds": round(perf_time.perf_counter() - start_time, 3),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "source": baseline_summary.get("source", "database-weather-and-pot-inventory"),
    }
    for key in (
        "sensorDataUsed",
        "sensorSource",
        "sensorRows",
        "latestStateRows",
        "sensorMappedDays",
        "sensorDateMappings",
        "sensorFirstDate",
        "sensorLastDate",
        "latestKnownSoilStateAt",
        "futureStateEstimated",
        "futureEstimatedDays",
        "futureEstimatedDateRange",
        "sensorError",
    ):
        if key in baseline_summary:
            summary[key] = baseline_summary[key]
    _add_chart_summary(summary, chart_entries, start_date, end_date)
    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _comparison_pot_info_entries(
            snapshot.pots,
            baseline,
            sparse,
            "sparse_water_usage_l",
        ),
    }


def run_daily_fuzzy_dt_experiment(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    """Run the paper-style digital-twin fuzzy irrigation recommendation.

    The controller follows the FIS layout described in the referenced paper:
    daily irrigation prescription from ETc, soil moisture, days after planting,
    rain forecast, and rain probability. Each active pot is treated as a
    homogeneous management zone.
    """
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    start_time = perf_time.perf_counter()
    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    baseline = _run_fuzzy_dt_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        persist=False,
        snapshot=snapshot,
        controller="fao_pm",
    )
    fuzzy = _run_fuzzy_dt_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        persist=persist,
        snapshot=snapshot,
    )

    baseline_by_date = {entry["date"]: entry for entry in baseline["entries"]}
    entries = []
    fuzzy_irrigation_days = 0
    baseline_irrigation_days = 0

    for fuzzy_entry in fuzzy["entries"]:
        baseline_entry = baseline_by_date.get(fuzzy_entry["date"])
        if baseline_entry is None:
            continue

        if baseline_entry["irrigation_events"] > 0:
            baseline_irrigation_days += 1
        if fuzzy_entry["irrigation_events"] > 0:
            fuzzy_irrigation_days += 1

        entries.append(_fuzzy_comparison_entry(baseline_entry, fuzzy_entry))

    baseline_chart_by_timestamp = {entry["timestamp"]: entry for entry in baseline.get("chartEntries", baseline["entries"])}
    chart_entries = []
    for fuzzy_entry in fuzzy.get("chartEntries", fuzzy["entries"]):
        baseline_entry = baseline_chart_by_timestamp.get(fuzzy_entry["timestamp"]) or baseline_by_date.get(fuzzy_entry["date"])
        if baseline_entry is None:
            continue
        chart_entries.append(_fuzzy_comparison_entry(baseline_entry, fuzzy_entry))

    total_days = len(entries)
    baseline_summary = baseline["summary"]
    fuzzy_summary = fuzzy["summary"]
    baseline_water = float(baseline_summary["totalWaterUsage"])
    fuzzy_water = float(fuzzy_summary["totalWaterUsage"])
    water_savings_l = baseline_water - fuzzy_water
    summary = {
        "totalEntries": total_days,
        "daysAnalyzed": total_days,
        "potsAnalyzed": fuzzy_summary["potsAnalyzed"],
        "fao_irrigation_days": baseline_irrigation_days,
        "baseline_irrigation_days": baseline_irrigation_days,
        "fuzzy_irrigation_days": fuzzy_irrigation_days,
        "fao_total_water_usage_l": baseline_water,
        "baseline_total_water_usage_l": baseline_water,
        "fuzzy_total_water_usage_l": fuzzy_water,
        "water_savings_l": round(water_savings_l, 2),
        "water_savings_percent": round((water_savings_l / baseline_water) * 100.0, 2) if baseline_water > 0 else 0.0,
        "fao_irrigation_event_count": baseline_summary["irrigationEvents"],
        "baseline_irrigation_event_count": baseline_summary["irrigationEvents"],
        "fuzzy_irrigation_event_count": fuzzy_summary["irrigationEvents"],
        "fao_valve_run_count": baseline_summary.get("valveRuns", baseline_summary["irrigationEvents"]),
        "baseline_valve_run_count": baseline_summary.get("valveRuns", baseline_summary["irrigationEvents"]),
        "fuzzy_valve_run_count": fuzzy_summary.get("valveRuns", fuzzy_summary["irrigationEvents"]),
        "fao_irrigation_decisions": baseline_summary["irrigationDecisions"],
        "baseline_irrigation_decisions": baseline_summary["irrigationDecisions"],
        "fuzzy_irrigation_decisions": fuzzy_summary["irrigationDecisions"],
        "average_prescription_mm": fuzzy_summary.get("averagePrescriptionMm", 0.0),
        "average_etc_mm": fuzzy_summary.get("averageEtcMm", 0.0),
        "fuzzyDataPolicy": fuzzy_summary.get("fuzzyDataPolicy", "daily-fis-prescription"),
        "decisionLevel": "valve_zone",
        "execution_time_seconds": round(perf_time.perf_counter() - start_time, 3),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "source": baseline_summary.get("source", "database-weather-and-pot-inventory"),
    }
    for key in (
        "sensorDataUsed",
        "sensorSource",
        "sensorRows",
        "latestStateRows",
        "sensorMappedDays",
        "sensorDateMappings",
        "sensorFirstDate",
        "sensorLastDate",
        "latestKnownSoilStateAt",
        "futureStateEstimated",
        "futureEstimatedDays",
        "futureEstimatedDateRange",
        "sensorError",
    ):
        if key in baseline_summary:
            summary[key] = baseline_summary[key]
    _add_chart_summary(summary, chart_entries, start_date, end_date)
    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _comparison_pot_info_entries(
            snapshot.pots,
            baseline,
            fuzzy,
            "fuzzy_water_usage_l",
        ),
    }


def run_daily_anfis_experiment(
    start_date: date,
    end_date: date,
    train_samples: int = 2000,
    test_samples: int = 800,
    seed: int | None = 2026,
    generations: int = 35,
    population: int = 24,
    parallel_workers: int | None = None,
    parallel_backend: str = "process",
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    """Apply an ANFIS controller to the same database weather/pot simulation."""
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    weather_rows = snapshot.selected_weather_rows
    pots = snapshot.pots
    sensor_context = snapshot.sensor_context

    start_time = perf_time.perf_counter()
    train_dataset = _generate_database_anfis_dataset(
        weather_rows,
        pots,
        train_samples,
        seed,
        sensor_context,
        snapshot.weather_by_day,
        snapshot.day_profiles,
    )
    test_dataset = _generate_database_anfis_dataset(
        weather_rows,
        pots,
        test_samples,
        (seed + 1) if seed is not None else None,
        sensor_context,
        snapshot.weather_by_day,
        snapshot.day_profiles,
    )

    model = ANFIS()
    model.fit(
        train_dataset,
        generations=generations,
        population=population,
        seed=seed,
        parallel=True,
        parallel_workers=parallel_workers,
        parallel_backend=parallel_backend,
    )
    evaluation = _evaluate_anfis_model(model, test_dataset)

    baseline = run_daily_irrigation_experiment(
        start_date=start_date,
        end_date=end_date,
        persist=False,
        snapshot=snapshot,
    )
    anfis = _run_anfis_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        model=model,
        persist=persist,
        snapshot=snapshot,
    )

    baseline_by_date = {entry["date"]: entry for entry in baseline["entries"]}
    entries = []
    predicted_probabilities = []
    baseline_irrigation_days = 0
    anfis_irrigation_days = 0

    for anfis_entry in anfis["entries"]:
        baseline_entry = baseline_by_date.get(anfis_entry["date"])
        if baseline_entry is None:
            continue

        baseline_active = baseline_entry["irrigation_events"] > 0
        anfis_active = anfis_entry["irrigation_events"] > 0
        if baseline_active:
            baseline_irrigation_days += 1
        if anfis_active:
            anfis_irrigation_days += 1

        predicted_probability = anfis_entry["predicted_probability"]
        predicted_probabilities.append(predicted_probability)

        entries.append(
            {
                "date": anfis_entry["date"],
                "timestamp": anfis_entry["timestamp"],
                "day_label": anfis_entry["day_label"],
                "chart_label": anfis_entry.get("chart_label", anfis_entry["day_label"]),
                "moisture": baseline_entry["average_moisture"],
                "baseline_moisture": baseline_entry["average_moisture"],
                "anfis_moisture": anfis_entry["average_moisture"],
                "temperature": anfis_entry["temperature"],
                "max_temperature": anfis_entry["max_temperature"],
                "humidity": anfis_entry["humidity"],
                "cloud_cover_pct": anfis_entry["cloud_cover_pct"],
                "rain_prediction": anfis_entry["rain_prediction"],
                "rain_amount": anfis_entry["rain_amount"],
                "predicted_probability": predicted_probability,
                "predicted_probability_percent": round(predicted_probability * 100.0, 2),
                "predicted_category": probability_category(predicted_probability),
                "baseline_irrigation_active": baseline_active,
                "anfis_irrigation_active": anfis_active,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "anfis_irrigation_events": anfis_entry["irrigation_events"],
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "anfis_water_usage_l": anfis_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "anfis_water_usage_ml": anfis_entry["water_usage_ml"],
                "alerts": anfis_entry["alerts"],
                **_combined_line_metadata(baseline_entry, anfis_entry),
            }
        )

    baseline_chart_by_timestamp = {entry["timestamp"]: entry for entry in baseline.get("chartEntries", baseline["entries"])}
    chart_entries = []
    for anfis_entry in anfis.get("chartEntries", anfis["entries"]):
        baseline_entry = baseline_chart_by_timestamp.get(anfis_entry["timestamp"]) or baseline_by_date.get(anfis_entry["date"])
        if baseline_entry is None:
            continue

        predicted_probability = anfis_entry.get("predicted_probability")
        predicted_probability_percent = anfis_entry.get("predicted_probability_percent")
        if predicted_probability is not None and predicted_probability_percent is None:
            predicted_probability_percent = round(predicted_probability * 100.0, 2)

        chart_entries.append(
            {
                "date": anfis_entry["date"],
                "timestamp": anfis_entry["timestamp"],
                "day_label": anfis_entry["day_label"],
                "chart_label": anfis_entry.get("chart_label", anfis_entry["day_label"]),
                "moisture": baseline_entry["average_moisture"],
                "baseline_moisture": baseline_entry["average_moisture"],
                "anfis_moisture": anfis_entry["average_moisture"],
                "temperature": anfis_entry["temperature"],
                "max_temperature": anfis_entry["max_temperature"],
                "humidity": anfis_entry["humidity"],
                "cloud_cover_pct": anfis_entry["cloud_cover_pct"],
                "rain_prediction": anfis_entry["rain_prediction"],
                "rain_amount": anfis_entry["rain_amount"],
                "predicted_probability": predicted_probability,
                "predicted_probability_percent": predicted_probability_percent,
                "predicted_category": probability_category(predicted_probability) if predicted_probability is not None else "not_applicable",
                "baseline_irrigation_active": baseline_entry["irrigation_events"] > 0,
                "anfis_irrigation_active": anfis_entry["irrigation_events"] > 0,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "anfis_irrigation_events": anfis_entry["irrigation_events"],
                "baseline_valve_runs": baseline_entry.get("valve_runs", baseline_entry["irrigation_events"]),
                "anfis_valve_runs": anfis_entry.get("valve_runs", anfis_entry["irrigation_events"]),
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "anfis_water_usage_l": anfis_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "anfis_water_usage_ml": anfis_entry["water_usage_ml"],
                "alerts": anfis_entry["alerts"],
                **_combined_line_metadata(baseline_entry, anfis_entry),
            }
        )

    execution_time_seconds = round(perf_time.perf_counter() - start_time, 3)
    pred_prob_mean = round(sum(predicted_probabilities) / max(len(predicted_probabilities), 1), 4) if predicted_probabilities else 0.0
    pred_prob_min = round(min(predicted_probabilities), 4) if predicted_probabilities else 0.0
    pred_prob_max = round(max(predicted_probabilities), 4) if predicted_probabilities else 0.0

    baseline_summary = baseline["summary"]
    anfis_summary = anfis["summary"]
    summary = {
        "totalEntries": len(entries),
        "daysAnalyzed": len(entries),
        "potsAnalyzed": anfis_summary["potsAnalyzed"],
        "baseline_irrigation_days": baseline_irrigation_days,
        "anfis_irrigation_days": anfis_irrigation_days,
        "baseline_irrigation_event_count": baseline_summary["irrigationEvents"],
        "anfis_irrigation_event_count": anfis_summary["irrigationEvents"],
        "baseline_valve_run_count": baseline_summary.get("valveRuns", baseline_summary["irrigationEvents"]),
        "anfis_valve_run_count": anfis_summary.get("valveRuns", anfis_summary["irrigationEvents"]),
        "baseline_total_water_usage_l": baseline_summary["totalWaterUsage"],
        "anfis_total_water_usage_l": anfis_summary["totalWaterUsage"],
        "anfis_probability_threshold": ANFIS_DECISION_THRESHOLD,
        "predicted_probability_mean": pred_prob_mean,
        "predicted_probability_min": pred_prob_min,
        "predicted_probability_max": pred_prob_max,
        "train_samples": train_samples,
        "test_samples": test_samples,
        "parallel_workers": parallel_workers,
        "parallel_backend": parallel_backend,
        "decisionLevel": "valve_zone",
        "execution_time_seconds": execution_time_seconds,
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "source": "database-weather-and-pot-inventory",
        **evaluation,
    }
    summary.update(_sensor_summary_fields(sensor_context))
    summary["source"] = _experiment_source(sensor_context)
    _add_chart_summary(summary, chart_entries, start_date, end_date)
    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _comparison_pot_info_entries(
            snapshot.pots,
            baseline,
            anfis,
            "anfis_water_usage_l",
        ),
    }



def _load_active_pots() -> list[dict[str, Any]]:
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT
                p.*,
                pt.label AS plant_type_label,
                pt.water_need_level,
                pt.heat_sensitive,
                pt.allows_second_watering,
                ps.volume_l,
                ps.evaporation_factor,
                ps.retention_factor
            FROM pots p
            JOIN plant_types pt ON pt.code = p.plant_type_code
            JOIN pot_size_profiles ps
              ON ps.code = CASE
                    WHEN p.size_class = 'small' THEN 'small_' || p.small_subtype
                    ELSE p.size_class
                 END
            WHERE p.active = true
            ORDER BY p.id
            """
        ).fetchall()
        return [_prepare_pot_row(row) for row in rows]


def _prepare_pot_row(row: dict[str, Any]) -> dict[str, Any]:
    pot = dict(row)
    for field in (
        "drip_flow_ml_min",
        "moisture_min_pct",
        "moisture_target_pct",
        "moisture_max_pct",
        "winter_moisture_target_pct",
        "volume_l",
        "evaporation_factor",
        "retention_factor",
    ):
        if pot.get(field) is not None:
            pot[field] = float(pot[field])
    pot["_sun_factor"] = _sun_factor(pot)
    pot["_wind_factor"] = _wind_factor(pot)
    pot["_outdoor_by_season"] = {
        "winter": pot["winter_location"] == "outdoor",
        "spring": pot["default_location"] == "outdoor",
        "summer": pot["default_location"] == "outdoor",
        "autumn": pot["default_location"] == "outdoor",
    }
    return pot


def _pot_info_entries(
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
                "default_location": pot["default_location"],
                "winter_location": pot["winter_location"],
                "balcony_zone": pot["balcony_zone"],
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


def _event_water_usage_l_by_pot(events: list[dict[str, Any]]) -> dict[int, float]:
    usage: dict[int, float] = {}
    for event in events:
        pot_id = int(event["pot_id"])
        usage[pot_id] = usage.get(pot_id, 0.0) + float(event.get("planned_volume_ml", 0.0)) / 1000.0
    return {pot_id: round(value, 2) for pot_id, value in usage.items()}


def _apply_valve_rollup_to_entries(
    entries: list[dict[str, Any]],
    detail_entries: list[dict[str, Any]],
    pots: list[dict[str, Any]],
    pot_decisions: list[dict[str, Any]],
    pot_events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    rollup = _valve_rollup(pots, pot_decisions, pot_events)
    _apply_valve_counts(entries, rollup, hourly=False)
    _apply_valve_counts(detail_entries, rollup, hourly=True)
    return rollup


def _valve_rollup(
    pots: list[dict[str, Any]],
    pot_decisions: list[dict[str, Any]],
    pot_events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    pot_by_id = {int(pot["id"]): pot for pot in pots}
    zone_pots = _pots_by_valve_zone(pots)
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
        _valve_event_from_group(key, group, pot_by_id, zone_pots)
        for key, group in event_groups.items()
    ]
    valve_events.sort(key=lambda item: (item["scheduled_start_at"], item["priority_rank"], item["valve_number"]))
    return {"decisions": valve_decisions, "events": valve_events}


def _pots_by_valve_zone(pots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    zones: dict[str, list[dict[str, Any]]] = {}
    for pot in pots:
        zones.setdefault(pot["balcony_zone"], []).append(pot)
    return zones


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
    valve_number = _valve_number_for_zone(zone)
    affected_pot_ids = [int(decision["pot_id"]) for decision in should]
    affected_pot_codes = [decision.get("pot_code") for decision in should if decision.get("pot_code")]
    reason_detail = (
        f"Valve V{valve_number} controls {zone}; {len(affected_pot_ids)} of {len(group)} evaluated pots require irrigation."
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
        "managed_pots": len(zone_pots.get(zone, [])),
        "evaluated_pots": len(group),
        "affected_pots": len(affected_pot_ids),
        "affected_pot_ids": affected_pot_ids,
        "affected_pot_codes": affected_pot_codes,
        "priority_score": round(priority, 2),
        "decision_level": "valve_zone",
    }


def _valve_event_from_group(
    key: tuple[str, str, str, str],
    group: list[dict[str, Any]],
    pot_by_id: dict[int, dict[str, Any]],
    zone_pots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    event_date, slot, scheduled_key, zone = key
    valve_number = _valve_number_for_zone(zone)
    managed = zone_pots.get(zone, [])
    flow_rate = sum(float(pot["drip_flow_ml_min"]) for pot in managed)
    planned_volume = sum(float(event.get("planned_volume_ml") or 0.0) for event in group)
    duration_min = planned_volume / max(flow_rate, 1.0)
    scheduled_start = datetime.fromisoformat(scheduled_key).replace(tzinfo=LOCAL_TZ)
    scheduled_end = scheduled_start + timedelta(minutes=duration_min)
    affected_pots = [pot_by_id[int(event["pot_id"])] for event in group if int(event["pot_id"]) in pot_by_id]
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
        "duration_min": round(duration_min, 1),
        "cycle_count": 1,
        "soak_pause_min": 0,
        "managed_pots": len(managed),
        "affected_pots": len(group),
        "affected_pot_ids": [int(pot["id"]) for pot in affected_pots],
        "affected_pot_codes": [pot["pot_code"] for pot in affected_pots],
        "priority_rank": 0 if any(event.get("priority_rank") == 0 for event in group) else 1,
        "priority_score": round(priority, 2),
        "decision_level": "valve_zone",
    }


def _apply_valve_counts(entries: list[dict[str, Any]], rollup: dict[str, list[dict[str, Any]]], hourly: bool) -> None:
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
        entry["decision_level"] = "valve_zone"


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


def _valve_number_for_zone(zone: str) -> int:
    if zone in VALVE_ZONE_ORDER:
        return VALVE_ZONE_ORDER[zone]
    return len(VALVE_ZONE_DESIGN) + 1


def _result_pot_usage_l(result: dict[str, Any], field: str = "period_water_usage_l") -> dict[int, float]:
    usage: dict[int, float] = {}
    for pot in result.get("pots", []):
        pot_id = pot.get("pot_id")
        if pot_id is not None:
            usage[int(pot_id)] = float(pot.get(field, 0.0))
    return usage


def _comparison_pot_info_entries(
    pots: list[dict[str, Any]],
    baseline_result: dict[str, Any],
    comparison_result: dict[str, Any],
    comparison_field: str,
) -> list[dict[str, Any]]:
    comparison_usage = _result_pot_usage_l(comparison_result)
    return _pot_info_entries(
        pots,
        {
            "baseline_water_usage_l": _result_pot_usage_l(baseline_result),
            comparison_field: comparison_usage,
            "period_water_usage_l": comparison_usage,
        },
    )


def _fuzzy_comparison_entry(baseline_entry: dict[str, Any], fuzzy_entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": fuzzy_entry["date"],
        "timestamp": fuzzy_entry["timestamp"],
        "day_label": fuzzy_entry["day_label"],
        "chart_label": fuzzy_entry.get("chart_label", fuzzy_entry["day_label"]),
        "moisture": baseline_entry["average_moisture"],
        "baseline_moisture": baseline_entry["average_moisture"],
        "fuzzy_moisture": fuzzy_entry["average_moisture"],
        "temperature": fuzzy_entry["temperature"],
        "max_temperature": fuzzy_entry["max_temperature"],
        "humidity": fuzzy_entry["humidity"],
        "cloud_cover_pct": fuzzy_entry["cloud_cover_pct"],
        "rain_prediction": fuzzy_entry["rain_prediction"],
        "rain_amount": fuzzy_entry["rain_amount"],
        "etc_mm": fuzzy_entry.get("etc_mm", 0.0),
        "fuzzy_prescription_mm": fuzzy_entry.get("fuzzy_prescription_mm", fuzzy_entry.get("avg_prescription_mm", 0.0)),
        "avg_prescription_mm": fuzzy_entry.get("avg_prescription_mm", fuzzy_entry.get("fuzzy_prescription_mm", 0.0)),
        "baseline_irrigation_active": baseline_entry["irrigation_events"] > 0,
        "fuzzy_irrigation_active": fuzzy_entry["irrigation_events"] > 0,
        "baseline_irrigation_events": baseline_entry["irrigation_events"],
        "fuzzy_irrigation_events": fuzzy_entry["irrigation_events"],
        "baseline_valve_runs": baseline_entry.get("valve_runs", baseline_entry["irrigation_events"]),
        "fuzzy_valve_runs": fuzzy_entry.get("valve_runs", fuzzy_entry["irrigation_events"]),
        "baseline_water_usage_l": baseline_entry["water_usage_l"],
        "fuzzy_water_usage_l": fuzzy_entry["water_usage_l"],
        "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
        "fuzzy_water_usage_ml": fuzzy_entry["water_usage_ml"],
        "alerts": fuzzy_entry["alerts"],
        **_combined_line_metadata(baseline_entry, fuzzy_entry),
    }


def _load_sensor_context(start_date: date, end_date: date, pots: list[dict[str, Any]]) -> dict[str, Any]:
    sensor_ids = [pot["id"] for pot in pots]
    try:
        ensure_sensor_readings_for_experiment_range(start_date, end_date, source=DEFAULT_SENSOR_SOURCE)
        sensor_context = load_sensor_readings_for_experiment(
            start_date=start_date,
            end_date=end_date,
            sensor_ids=sensor_ids,
            source=DEFAULT_SENSOR_SOURCE,
        )
        return _with_sensor_associations(sensor_context, pots)
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


def _with_sensor_associations(sensor_context: dict[str, Any], pots: list[dict[str, Any]]) -> dict[str, Any]:
    if not sensor_context.get("available"):
        return sensor_context

    sensor_ids = {int(sensor_id) for sensor_id in sensor_context.get("sensor_ids") or []}
    pot_by_id = {int(pot["id"]): pot for pot in pots}
    sensor_pots = [pot_by_id[sensor_id] for sensor_id in sensor_ids if sensor_id in pot_by_id]
    if not sensor_pots:
        return sensor_context

    associations = {}
    for pot in pots:
        pot_id = int(pot["id"])
        if pot_id in sensor_ids:
            associations[pot_id] = {"sensor_id": pot_id, "direct": True, "distance": 0.0}
            continue
        sensor_pot = min(sensor_pots, key=lambda item: _sensor_association_distance(pot, item))
        associations[pot_id] = {
            "sensor_id": int(sensor_pot["id"]),
            "direct": False,
            "distance": round(_sensor_association_distance(pot, sensor_pot), 4),
        }

    enriched = dict(sensor_context)
    enriched["associations"] = associations
    enriched["sensor_pots"] = {int(pot["id"]): pot for pot in sensor_pots}
    enriched["associated_pot_count"] = len([item for item in associations.values() if not item["direct"]])
    return enriched


def _with_sensor_key(record: dict[str, Any], pot: dict[str, Any], sensor_context: dict[str, Any]) -> dict[str, Any]:
    sensor_id = _sensor_id_for_pot(sensor_context, pot)
    enriched = dict(record)
    enriched["sensor_id"] = sensor_id
    if sensor_id != int(pot["id"]):
        enriched["associated_pot_id"] = int(pot["id"])
    return enriched


def _with_event_sensor_key(event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(event)
    enriched["sensor_id"] = int(decision.get("sensor_id", event["pot_id"]))
    if decision.get("associated_pot_id") is not None:
        enriched["associated_pot_id"] = int(decision["associated_pot_id"])
    return enriched


def _sensor_id_for_pot(sensor_context: dict[str, Any] | None, pot: dict[str, Any]) -> int:
    pot_id = int(pot["id"])
    association = (sensor_context or {}).get("associations", {}).get(pot_id)
    if association and association.get("sensor_id") is not None:
        return int(association["sensor_id"])
    return pot_id


def _sensor_association_distance(pot: dict[str, Any], sensor_pot: dict[str, Any]) -> float:
    categorical_weights = {
        "plant_type_code": 3.0,
        "size_class": 1.8,
        "small_subtype": 0.8,
        "default_location": 2.2,
        "winter_location": 1.0,
        "balcony_zone": 1.3,
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


def _apply_sensor_reading(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
) -> dict[str, Any] | None:
    reading = _sensor_reading_for_pot(sensor_context, pot, experiment_date, observed_at.hour)
    if reading is None:
        return None

    reading = dict(reading)
    sensor_moisture = _number(reading["soil_moisture_pct"], state.moisture)
    if reading.get("association_source") == "associated_sensor":
        state.moisture = _clamp(sensor_moisture * 0.82 + state.moisture * 0.18, 0.0, 100.0)
        reading["soil_moisture_pct"] = round(state.moisture, 2)
    else:
        state.moisture = _clamp(sensor_moisture, 0.0, 100.0)
    return reading


def _sensor_reading_for_pot(
    sensor_context: dict[str, Any] | None,
    pot: dict[str, Any],
    experiment_date: date,
    hour: int,
) -> dict[str, Any] | None:
    if not sensor_context or not sensor_context.get("available"):
        return None

    lookup = sensor_context.get("lookup") or {}
    pot_id = int(pot["id"])
    direct = lookup.get((experiment_date, hour, pot_id))
    if direct is not None:
        return direct

    association = (sensor_context.get("associations") or {}).get(pot_id)
    if not association:
        return None
    sensor_id = int(association["sensor_id"])
    sensor_reading = lookup.get((experiment_date, hour, sensor_id))
    if sensor_reading is None:
        return None
    sensor_pot = (sensor_context.get("sensor_pots") or {}).get(sensor_id)
    if sensor_pot is None:
        return sensor_reading
    return _associated_sensor_reading(pot, sensor_pot, sensor_reading)


def _associated_sensor_reading(
    pot: dict[str, Any],
    sensor_pot: dict[str, Any],
    sensor_reading: dict[str, Any],
) -> dict[str, Any]:
    sensor_moisture = _number(sensor_reading["soil_moisture_pct"], pot["moisture_target_pct"])
    target_adjustment = (float(pot["moisture_target_pct"]) - float(sensor_pot["moisture_target_pct"])) * 0.45
    min_adjustment = (float(pot["moisture_min_pct"]) - float(sensor_pot["moisture_min_pct"])) * 0.2
    exposure_adjustment = (_pot_exposure_index(sensor_pot) - _pot_exposure_index(pot)) * 2.2
    retention_adjustment = (float(pot["retention_factor"]) - float(sensor_pot["retention_factor"])) * 4.0
    volume_adjustment = math.log(max(float(pot["volume_l"]), 0.1) / max(float(sensor_pot["volume_l"]), 0.1)) * 0.8
    inferred_moisture = _clamp(
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


def _pot_exposure_index(pot: dict[str, Any]) -> float:
    outdoor = 1.0 if pot.get("default_location") == "outdoor" else 0.0
    return outdoor + (_sun_factor(pot) - 1.0) * 1.6 + (_wind_factor(pot) - 1.0) * 1.2


def _latest_sensor_state_for_pot(sensor_context: dict[str, Any], pot: dict[str, Any]) -> dict[str, Any] | None:
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
    return _associated_sensor_reading(pot, sensor_pot, latest)


def _prime_future_states(
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
        latest = _latest_sensor_state_for_pot(sensor_context, pot)
        if latest:
            pot_states[pot["id"]].moisture = _clamp(_number(latest["soil_moisture_pct"], pot_states[pot["id"]].moisture), 0.0, 100.0)

    warmup_start = latest_state_at.date()
    warmup_end = start_date - timedelta(days=1)
    if warmup_end >= warmup_start:
        missing_days = [
            warmup_start + timedelta(days=offset)
            for offset in range((warmup_end - warmup_start).days + 1)
            if warmup_start + timedelta(days=offset) not in weather_by_day
        ]
        if missing_days:
            warmup_weather = _load_weather(min(missing_days), max(missing_days))
            for day, rows in _group_weather_by_day(warmup_weather).items():
                weather_by_day.setdefault(day, rows)

    current = (latest_state_at + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    end = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    warmup_day_profiles: dict[date, dict[str, Any]] = {}
    while current < end:
        day_weather = weather_by_day.get(current.date(), [])
        hour_weather = _weather_for_hour(day_weather, current)
        if hour_weather is None:
            current += timedelta(hours=1)
            continue

        current_day = current.date()
        day_profile = warmup_day_profiles.get(current_day)
        if day_profile is None:
            day_profile = _day_profile(current_day, day_weather, weather_by_day)
            warmup_day_profiles[current_day] = day_profile
        for pot in pots:
            state = pot_states[pot["id"]]
            _apply_hourly_environment(state, pot, hour_weather, day_profile, current.date())
            slot = _decision_slot(current.date(), current, day_profile)
            if slot is None:
                continue

            decision = _make_irrigation_decision(state, pot, hour_weather, day_profile, slot)
            if decision["should_irrigate"]:
                _apply_irrigation_event(state, pot, hour_weather, decision)
        current += timedelta(hours=1)


def _weather_for_hour(day_weather: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any] | None:
    for row in day_weather:
        if _local_observed_at(row).hour == observed_at.hour:
            return row
    return day_weather[0] if day_weather else None


def _weather_snapshot_for_time(sampled_weather: dict[str, Any], current_weather: dict[str, Any]) -> dict[str, Any]:
    weather = dict(sampled_weather)
    weather["id"] = current_weather["id"]
    weather["observed_at"] = current_weather["observed_at"]
    return weather


def _sampled_day_profile_for_date(day: date, sampled_day_profile: dict[str, Any]) -> dict[str, Any]:
    profile = dict(sampled_day_profile)
    profile["season"] = _season(day)
    return profile


def _hourly_line_metadata(
    sensor_context: dict[str, Any],
    experiment_date: date,
    observed_local: datetime,
    weather: dict[str, Any],
) -> dict[str, Any]:
    metadata = _sensor_line_metadata_for_hour(sensor_context, experiment_date, observed_local.hour)
    metadata["is_weather_prediction"] = _weather_row_is_prediction(weather)
    metadata["has_prediction_or_simulation"] = (
        metadata["is_weather_prediction"]
        or metadata["is_sensor_prediction"]
        or metadata["is_sensor_simulated"]
    )
    return metadata


def _daily_line_metadata(
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
    return _local_observed_at(weather) > datetime.now(LOCAL_TZ)


def _weather_day_is_prediction(experiment_date: date) -> bool:
    return experiment_date > datetime.now(LOCAL_TZ).date()


def _sensor_line_metadata_for_hour(sensor_context: dict[str, Any], experiment_date: date, hour: int) -> dict[str, Any]:
    has_reading_for_day = _has_sensor_reading_for_day(sensor_context, experiment_date)
    if _sensor_hour_is_future(experiment_date, hour):
        return _sensor_metadata(simulated=True, prediction=True, has_reading_for_day=has_reading_for_day)
    if _sensor_date_is_future(sensor_context, experiment_date):
        return _sensor_metadata(simulated=True, prediction=True, has_reading_for_day=has_reading_for_day)
    if not sensor_context.get("available"):
        return _sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    sensor_ids = sensor_context.get("sensor_ids") or []
    lookup = sensor_context.get("lookup") or {}
    if not sensor_ids:
        return _sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    for sensor_id in sensor_ids:
        reading = lookup.get((experiment_date, hour, sensor_id))
        if reading is None or reading.get("source") != ACTUAL_SENSOR_SOURCE:
            return _sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)
    return _sensor_metadata(simulated=False, prediction=False, has_reading_for_day=has_reading_for_day)


def _sensor_line_metadata_for_day(sensor_context: dict[str, Any], experiment_date: date) -> dict[str, Any]:
    has_reading_for_day = _has_sensor_reading_for_day(sensor_context, experiment_date)
    if _sensor_date_is_future(sensor_context, experiment_date):
        return _sensor_metadata(simulated=True, prediction=True, has_reading_for_day=has_reading_for_day)
    if not sensor_context.get("available"):
        return _sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    sensor_ids = set(sensor_context.get("sensor_ids") or [])
    lookup = sensor_context.get("lookup") or {}
    if not sensor_ids:
        return _sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    rows = [
        reading
        for (reading_date, _hour, sensor_id), reading in lookup.items()
        if reading_date == experiment_date and sensor_id in sensor_ids
    ]
    if not rows:
        return _sensor_metadata(simulated=True, prediction=False, has_reading_for_day=has_reading_for_day)

    actual_sensor_ids = {
        int(row["sensor_id"])
        for row in rows
        if row.get("source") == ACTUAL_SENSOR_SOURCE
    }
    has_non_actual = any(row.get("source") != ACTUAL_SENSOR_SOURCE for row in rows)
    simulated = has_non_actual or actual_sensor_ids != sensor_ids
    return _sensor_metadata(simulated=simulated, prediction=False, has_reading_for_day=has_reading_for_day)


def _has_sensor_reading_for_day(sensor_context: dict[str, Any], experiment_date: date) -> bool:
    return experiment_date in set(sensor_context.get("sensor_reading_dates") or [])


def _sensor_date_is_future(sensor_context: dict[str, Any], experiment_date: date) -> bool:
    return experiment_date in set(sensor_context.get("future_dates") or [])


def _sensor_hour_is_future(experiment_date: date, hour: int) -> bool:
    observed_at = datetime.combine(experiment_date, time(hour, 0), tzinfo=LOCAL_TZ)
    return observed_at > datetime.now(LOCAL_TZ)


def _sensor_metadata(simulated: bool, prediction: bool, has_reading_for_day: bool) -> dict[str, Any]:
    return {
        "is_sensor_simulated": simulated,
        "is_sensor_prediction": prediction,
        "has_sensor_reading_for_day": has_reading_for_day,
        "is_sensor_missing_reading": not has_reading_for_day,
    }


def _combined_line_metadata(*entries: dict[str, Any]) -> dict[str, Any]:
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


def _anfis_inputs(state: PotState, weather: dict[str, Any], sensor_reading: dict[str, Any] | None) -> dict[str, float]:
    if sensor_reading:
        return {
            "moisture": state.moisture,
            "humidity": _number(sensor_reading["air_humidity_pct"], _number(weather["relative_humidity_pct"], 60.0)),
            "temperature": _number(sensor_reading["air_temperature_c"], _number(weather["temperature_c"], 20.0)),
        }
    return {
        "moisture": state.moisture,
        "humidity": _number(weather["relative_humidity_pct"], 60.0),
        "temperature": _number(weather["temperature_c"], 20.0),
    }


def _sensor_summary_fields(sensor_context: dict[str, Any]) -> dict[str, Any]:
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


def _experiment_source(sensor_context: dict[str, Any] | None) -> str:
    if not sensor_context or not sensor_context.get("available"):
        return "database-weather-and-pot-inventory"
    if sensor_context.get("future_dates"):
        return "database-weather-pot-inventory-sensor-readings-and-estimated-dt-state"
    return "database-weather-pot-inventory-and-sensor-readings"


def _run_sparse_daily_irrigation(
    start_date: date,
    end_date: date,
    sample_interval_hours: int,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    weather_rows = snapshot.selected_weather_rows
    pots = snapshot.pots
    sensor_context = snapshot.sensor_context
    weather_by_day = snapshot.weather_by_day
    actual_states = _copy_pot_states(snapshot.initial_pot_states)
    controller_states = _copy_pot_states(snapshot.initial_pot_states)

    entries = []
    detail_entries = []
    decisions = []
    events = []
    alerts = []

    total_water_ml = 0.0
    total_irrigation_events = 0
    total_irrigation_decisions = 0
    hour_index = 0
    sampled_weather = None
    sampled_day_profile = None
    sampled_weather_count = 0

    current_date = start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        day_profile = snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            sample_now = hour_index % sample_interval_hours == 0
            if sample_now or sampled_weather is None or sampled_day_profile is None:
                sampled_weather = hour_weather
                sampled_day_profile = day_profile
                sampled_weather_count += 1

            controller_weather = _weather_snapshot_for_time(sampled_weather, hour_weather)
            controller_day_profile = _sampled_day_profile_for_date(current_date, sampled_day_profile)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0

            for pot in pots:
                actual_state = actual_states[pot["id"]]
                controller_state = controller_states[pot["id"]]
                _apply_hourly_environment(actual_state, pot, hour_weather, day_profile, observed_local.date())
                _apply_sensor_reading(actual_state, pot, current_date, observed_local, sensor_context)

                if sample_now:
                    controller_state.moisture = actual_state.moisture
                else:
                    _apply_hourly_environment(controller_state, pot, controller_weather, controller_day_profile, observed_local.date())

                slot = _decision_slot(current_date, observed_local, controller_day_profile)
                if slot is None:
                    if _is_emergency_dryness(actual_state, pot, current_date, observed_local):
                        alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                        daily_alerts += 1
                        hourly_alerts += 1
                    continue

                decision = _with_sensor_key(
                    _make_irrigation_decision(controller_state, pot, controller_weather, controller_day_profile, slot),
                    pot,
                    sensor_context,
                )
                decisions.append(decision)
                daily_decisions += 1
                hourly_decisions += 1

                if decision["should_irrigate"]:
                    event = _with_event_sensor_key(_apply_irrigation_event(controller_state, pot, controller_weather, decision), decision)
                    _apply_planned_volume(actual_state, pot, event["planned_volume_ml"])
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]

                if actual_state.moisture > pot["moisture_max_pct"]:
                    actual_state.too_wet_hours += 1
                    if actual_state.too_wet_hours == 24:
                        alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                        daily_alerts += 1
                        hourly_alerts += 1
                else:
                    actual_state.too_wet_hours = 0
            if _uses_hourly_chart(start_date, end_date):
                detail_entries.append(
                    _hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        actual_states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        _hourly_line_metadata(sensor_context, current_date, observed_local, hour_weather),
                    )
                )
            hour_index += 1

        entries.append(
            _daily_aggregate_entry(
                current_date,
                day_profile,
                actual_states,
                daily_water_ml,
                daily_events,
                daily_decisions,
                daily_alerts,
                _daily_line_metadata(sensor_context, current_date, day_weather),
            )
        )
        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions
        current_date += timedelta(days=1)

    valve_rollup = _apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

    summary = _daily_summary(
        entries=entries,
        pots=pots,
        weather_rows=weather_rows,
        total_water_ml=total_water_ml,
        total_irrigation_events=len(valve_rollup["events"]),
        total_irrigation_decisions=len(valve_rollup["decisions"]),
        alerts=alerts,
        start_date=start_date,
        end_date=end_date,
        sensor_context=sensor_context,
    )
    summary["potIrrigationDecisions"] = total_irrigation_decisions
    summary["potIrrigationActions"] = len(events)
    summary["decisionLevel"] = "valve_zone"
    summary["sampledWeatherRows"] = sampled_weather_count
    summary["samplingDataPolicy"] = "sensor-and-weather-sampled"
    if persist:
        _persist_daily_results("sampling", start_date, end_date, decisions, events, alerts)
    chart_entries = _chart_entries_for_range(start_date, end_date, entries, detail_entries)
    _add_chart_summary(summary, chart_entries, start_date, end_date)
    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _pot_info_entries(
            pots,
            {"period_water_usage_l": _event_water_usage_l_by_pot(events)},
        ),
        "sampleDecisions": valve_rollup["decisions"][:200],
        "sampleEvents": valve_rollup["events"][:200],
        "samplePotDecisions": decisions[:200],
        "samplePotEvents": events[:200],
        "sampleAlerts": alerts[:200],
    }


def _run_fuzzy_dt_daily_irrigation(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    controller: str = "fuzzy_dt",
) -> dict[str, Any]:
    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    weather_rows = snapshot.selected_weather_rows
    pots = snapshot.pots
    sensor_context = snapshot.sensor_context
    weather_by_day = snapshot.weather_by_day
    pot_states = _copy_pot_states(snapshot.initial_pot_states)

    entries = []
    detail_entries = []
    decisions = []
    events = []
    alerts = []

    total_water_ml = 0.0
    total_irrigation_events = 0
    total_irrigation_decisions = 0
    prescription_sum = 0.0
    prescription_count = 0
    etc_sum = 0.0
    etc_count = 0

    current_date = start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        day_profile = snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_prescription_sum = 0.0
        daily_prescription_count = 0
        daily_etc_sum = 0.0
        daily_etc_count = 0

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            hourly_prescription_sum = 0.0
            hourly_prescription_count = 0
            hourly_etc_sum = 0.0
            hourly_etc_count = 0

            for pot in pots:
                state = pot_states[pot["id"]]
                _apply_hourly_environment(state, pot, hour_weather, day_profile, observed_local.date())
                _apply_sensor_reading(state, pot, current_date, observed_local, sensor_context)

                if observed_local.hour != 8:
                    if _is_emergency_dryness(state, pot, current_date, observed_local):
                        alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                        daily_alerts += 1
                        hourly_alerts += 1
                    continue

                if controller == "fao_pm":
                    decision = _make_fao_pm_decision(state, pot, hour_weather, day_profile)
                else:
                    decision = _make_fuzzy_dt_decision(state, pot, hour_weather, day_profile)
                decision = _with_sensor_key(decision, pot, sensor_context)
                decisions.append(decision)
                daily_decisions += 1
                hourly_decisions += 1
                total_irrigation_decisions += 1

                prescription_mm = float(decision.get("prescription_mm", 0.0))
                etc_mm = float(decision.get("etc_mm", 0.0))
                prescription_sum += prescription_mm
                prescription_count += 1
                daily_prescription_sum += prescription_mm
                daily_prescription_count += 1
                hourly_prescription_sum += prescription_mm
                hourly_prescription_count += 1
                etc_sum += etc_mm
                etc_count += 1
                daily_etc_sum += etc_mm
                daily_etc_count += 1
                hourly_etc_sum += etc_mm
                hourly_etc_count += 1

                if decision["should_irrigate"]:
                    event = _with_event_sensor_key(_apply_fuzzy_prescribed_event(state, pot, hour_weather, decision), decision)
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]
                    total_irrigation_events += 1

                if state.moisture > pot["moisture_max_pct"]:
                    state.too_wet_hours += 1
                    if state.too_wet_hours == 24:
                        alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                        daily_alerts += 1
                        hourly_alerts += 1
                else:
                    state.too_wet_hours = 0

            if _uses_hourly_chart(start_date, end_date):
                detail_entries.append(
                    _hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        pot_states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        {
                            **_hourly_line_metadata(sensor_context, current_date, observed_local, hour_weather),
                            "fuzzy_prescription_mm": round(hourly_prescription_sum / max(hourly_prescription_count, 1), 2),
                            "avg_prescription_mm": round(hourly_prescription_sum / max(hourly_prescription_count, 1), 2),
                            "etc_mm": round(hourly_etc_sum / max(hourly_etc_count, 1), 2),
                        },
                    )
                )

        entries.append(
            _daily_aggregate_entry(
                current_date,
                day_profile,
                pot_states,
                daily_water_ml,
                daily_events,
                daily_decisions,
                daily_alerts,
                {
                    **_daily_line_metadata(sensor_context, current_date, day_weather),
                    "fuzzy_prescription_mm": round(daily_prescription_sum / max(daily_prescription_count, 1), 2),
                    "avg_prescription_mm": round(daily_prescription_sum / max(daily_prescription_count, 1), 2),
                    "etc_mm": round(daily_etc_sum / max(daily_etc_count, 1), 2),
                },
            )
        )
        total_water_ml += daily_water_ml
        current_date += timedelta(days=1)

    valve_rollup = _apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

    summary = _daily_summary(
        entries=entries,
        pots=pots,
        weather_rows=weather_rows,
        total_water_ml=total_water_ml,
        total_irrigation_events=len(valve_rollup["events"]),
        total_irrigation_decisions=len(valve_rollup["decisions"]),
        alerts=alerts,
        start_date=start_date,
        end_date=end_date,
        sensor_context=sensor_context,
    )
    summary["potIrrigationDecisions"] = total_irrigation_decisions
    summary["potIrrigationActions"] = len(events)
    summary["decisionLevel"] = "valve_zone"
    summary["averagePrescriptionMm"] = round(prescription_sum / max(prescription_count, 1), 2)
    summary["averageEtcMm"] = round(etc_sum / max(etc_count, 1), 2)
    summary["fuzzyDataPolicy"] = "daily-fis-prescription" if controller == "fuzzy_dt" else "daily-fao-penman-monteith-prescription"
    if persist:
        _persist_daily_results(controller, start_date, end_date, decisions, events, alerts)
    chart_entries = _chart_entries_for_range(start_date, end_date, entries, detail_entries)
    _add_chart_summary(summary, chart_entries, start_date, end_date)
    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _pot_info_entries(
            pots,
            {"period_water_usage_l": _event_water_usage_l_by_pot(events)},
        ),
        "sampleDecisions": valve_rollup["decisions"][:200],
        "sampleEvents": valve_rollup["events"][:200],
        "samplePotDecisions": decisions[:200],
        "samplePotEvents": events[:200],
        "sampleAlerts": alerts[:200],
    }


def _run_anfis_daily_irrigation(
    start_date: date,
    end_date: date,
    model: ANFIS,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    weather_rows = snapshot.selected_weather_rows
    pots = snapshot.pots
    sensor_context = snapshot.sensor_context
    weather_by_day = snapshot.weather_by_day
    pot_states = _copy_pot_states(snapshot.initial_pot_states)

    entries = []
    detail_entries = []
    decisions = []
    events = []
    alerts = []

    total_water_ml = 0.0
    total_irrigation_events = 0
    total_irrigation_decisions = 0

    current_date = start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        day_profile = snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        probability_sum = 0.0
        probability_count = 0

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            hourly_probability_sum = 0.0
            hourly_probability_count = 0

            for pot in pots:
                state = pot_states[pot["id"]]
                _apply_hourly_environment(state, pot, hour_weather, day_profile, observed_local.date())
                sensor_reading = _apply_sensor_reading(state, pot, current_date, observed_local, sensor_context)

                slot = _decision_slot(current_date, observed_local, day_profile)
                if slot is None:
                    if _is_emergency_dryness(state, pot, current_date, observed_local):
                        alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                        daily_alerts += 1
                        hourly_alerts += 1
                    continue

                rule_decision = _make_irrigation_decision(state, pot, hour_weather, day_profile, slot)
                predicted_probability = model.predict(_anfis_inputs(state, hour_weather, sensor_reading))
                probability_sum += predicted_probability
                probability_count += 1
                hourly_probability_sum += predicted_probability
                hourly_probability_count += 1

                hard_stop = rule_decision["reason_code"] in {
                    "freeze_risk",
                    "winter_conditions_not_met",
                    "rain_sufficient",
                    "second_watering_not_needed",
                }
                should_irrigate = (
                    predicted_probability >= ANFIS_DECISION_THRESHOLD
                    and not hard_stop
                    and state.moisture < rule_decision["target_moisture_pct"]
                )

                decision = dict(rule_decision)
                decision = _with_sensor_key(decision, pot, sensor_context)
                decision["should_irrigate"] = should_irrigate
                decision["predicted_probability"] = round(predicted_probability, 4)
                decision["predicted_category"] = probability_category(predicted_probability)
                if should_irrigate:
                    decision["reason_code"] = "anfis_probability_high"
                    decision["reason_detail"] = f"ANFIS probability {predicted_probability:.2f} is above threshold {ANFIS_DECISION_THRESHOLD:.2f}."
                elif not hard_stop:
                    decision["reason_code"] = "anfis_probability_low"
                    decision["reason_detail"] = f"ANFIS probability {predicted_probability:.2f} is below threshold {ANFIS_DECISION_THRESHOLD:.2f}."

                decisions.append(decision)
                daily_decisions += 1
                hourly_decisions += 1

                if should_irrigate:
                    event = _with_event_sensor_key(_apply_irrigation_event(state, pot, hour_weather, decision), decision)
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]

                if state.moisture > pot["moisture_max_pct"]:
                    state.too_wet_hours += 1
                    if state.too_wet_hours == 24:
                        alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                        daily_alerts += 1
                        hourly_alerts += 1
                else:
                    state.too_wet_hours = 0

            if _uses_hourly_chart(start_date, end_date):
                hourly_probability = hourly_probability_sum / hourly_probability_count if hourly_probability_count else 0.0
                detail_entries.append(
                    _hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        pot_states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        {
                            **_hourly_line_metadata(sensor_context, current_date, observed_local, hour_weather),
                            "predicted_probability": round(hourly_probability, 4),
                            "predicted_probability_percent": round(hourly_probability * 100.0, 2),
                        },
                    )
                )

        predicted_probability = probability_sum / max(probability_count, 1)
        entries.append(
            _daily_aggregate_entry(
                current_date,
                day_profile,
                pot_states,
                daily_water_ml,
                daily_events,
                daily_decisions,
                daily_alerts,
                {
                    **_daily_line_metadata(sensor_context, current_date, day_weather),
                    "predicted_probability": round(predicted_probability, 4),
                },
            )
        )
        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions
        current_date += timedelta(days=1)

    valve_rollup = _apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

    summary = _daily_summary(
        entries=entries,
        pots=pots,
        weather_rows=weather_rows,
        total_water_ml=total_water_ml,
        total_irrigation_events=len(valve_rollup["events"]),
        total_irrigation_decisions=len(valve_rollup["decisions"]),
        alerts=alerts,
        start_date=start_date,
        end_date=end_date,
        sensor_context=sensor_context,
    )
    summary["potIrrigationDecisions"] = total_irrigation_decisions
    summary["potIrrigationActions"] = len(events)
    summary["decisionLevel"] = "valve_zone"
    if persist:
        _persist_daily_results("anfis", start_date, end_date, decisions, events, alerts)
    chart_entries = _chart_entries_for_range(start_date, end_date, entries, detail_entries)
    _add_chart_summary(summary, chart_entries, start_date, end_date)
    return {
        "entries": entries,
        "chartEntries": chart_entries,
        "summary": summary,
        "pots": _pot_info_entries(
            pots,
            {"period_water_usage_l": _event_water_usage_l_by_pot(events)},
        ),
        "sampleDecisions": valve_rollup["decisions"][:200],
        "sampleEvents": valve_rollup["events"][:200],
        "samplePotDecisions": decisions[:200],
        "samplePotEvents": events[:200],
        "sampleAlerts": alerts[:200],
    }


def _initial_pot_states(pots: list[dict[str, Any]]) -> dict[int, PotState]:
    states = {}
    for pot in pots:
        rng = random.Random(2026 + pot["id"])
        target = pot["moisture_target_pct"]
        states[pot["id"]] = PotState(moisture=max(5.0, min(95.0, target + rng.uniform(-6.0, 4.0))))
    return states


def _copy_pot_states(states: dict[int, PotState]) -> dict[int, PotState]:
    return {
        pot_id: PotState(moisture=state.moisture, too_wet_hours=state.too_wet_hours)
        for pot_id, state in states.items()
    }


def _group_weather_by_day(weather_rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in weather_rows:
        day = _local_observed_at(row).date()
        grouped.setdefault(day, []).append(row)
    return grouped


def _day_profiles_for_range(
    start_date: date,
    end_date: date,
    weather_by_day: dict[date, list[dict[str, Any]]],
) -> dict[date, dict[str, Any]]:
    profiles: dict[date, dict[str, Any]] = {}
    current_date = start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if day_weather:
            profiles[current_date] = _day_profile(current_date, day_weather, weather_by_day)
        current_date += timedelta(days=1)
    return profiles


def _hourly_aggregate_entry(
    observed_at: datetime,
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    pot_states: dict[int, PotState],
    hourly_water_ml: float,
    hourly_events: int,
    hourly_decisions: int,
    hourly_alerts: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    moistures = [state.moisture for state in pot_states.values()]
    avg_moisture = sum(moistures) / max(len(moistures), 1)
    temperature = _number(weather["temperature_c"], day_profile["avg_temperature_c"])
    humidity = _number(weather["relative_humidity_pct"], day_profile["avg_humidity_pct"])
    cloud_cover = _weather_cloud_cover_pct(weather, day_profile["avg_cloud_cover_pct"])
    rain_amount = _number(weather["precipitation_mm"], 0.0)
    wind_gust = _number(weather["wind_gust_kmh"], _number(weather["wind_speed_kmh"], 0.0))
    valve_runs = max(0, int(hourly_events or 0))
    entry = {
        "date": observed_at.date().isoformat(),
        "timestamp": observed_at.isoformat(),
        "day_label": observed_at.strftime("%Y-%m-%d %H:%M"),
        "chart_label": observed_at.strftime("%m-%d %H:%M"),
        "hour": observed_at.strftime("%H:%M"),
        "moisture": round(avg_moisture, 2),
        "average_moisture": round(avg_moisture, 2),
        "min_moisture": round(min(moistures), 2),
        "max_moisture": round(max(moistures), 2),
        "temperature": round(temperature, 2),
        "max_temperature": round(temperature, 2),
        "humidity": round(humidity, 2),
        "cloud_cover_pct": round(cloud_cover, 2),
        "rain_prediction": rain_amount >= 0.5,
        "rain_amount": round(rain_amount, 2),
        "wind_gust_kmh": round(wind_gust, 2),
        "heatwave_day": day_profile["heatwave_day"],
        "freeze_risk": day_profile["freeze_risk"],
        "irrigation_active": valve_runs > 0,
        "irrigation_events": 1 if valve_runs > 0 else 0,
        "valve_runs": valve_runs,
        "irrigated_pots": valve_runs,
        "irrigation_decisions": hourly_decisions,
        "alerts": hourly_alerts,
        "water_usage_ml": round(hourly_water_ml, 2),
        "water_usage_l": round(hourly_water_ml / 1000.0, 2),
    }
    if extra:
        entry.update(extra)
    return entry


def _daily_aggregate_entry(
    current_date: date,
    day_profile: dict[str, Any],
    pot_states: dict[int, PotState],
    daily_water_ml: float,
    daily_events: int,
    daily_decisions: int,
    daily_alerts: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    moistures = [state.moisture for state in pot_states.values()]
    avg_moisture = sum(moistures) / max(len(moistures), 1)
    valve_runs = max(0, int(daily_events or 0))
    entry = {
        "date": current_date.isoformat(),
        "timestamp": datetime.combine(current_date, time(12, 0), tzinfo=LOCAL_TZ).isoformat(),
        "day_label": current_date.strftime("%Y-%m-%d"),
        "chart_label": current_date.strftime("%Y-%m-%d"),
        "moisture": round(avg_moisture, 2),
        "average_moisture": round(avg_moisture, 2),
        "min_moisture": round(min(moistures), 2),
        "max_moisture": round(max(moistures), 2),
        "temperature": round(day_profile["avg_temperature_c"], 2),
        "max_temperature": round(day_profile["max_temperature_c"], 2),
        "humidity": round(day_profile["avg_humidity_pct"], 2),
        "cloud_cover_pct": round(day_profile["avg_cloud_cover_pct"], 2),
        "rain_prediction": day_profile["precipitation_mm"] >= 0.5,
        "rain_amount": round(day_profile["precipitation_mm"], 2),
        "wind_gust_kmh": round(day_profile["max_wind_gust_kmh"], 2),
        "heatwave_day": day_profile["heatwave_day"],
        "freeze_risk": day_profile["freeze_risk"],
        "irrigation_active": valve_runs > 0,
        "irrigation_events": 1 if valve_runs > 0 else 0,
        "valve_runs": valve_runs,
        "irrigated_pots": valve_runs,
        "irrigation_decisions": daily_decisions,
        "alerts": daily_alerts,
        "water_usage_ml": round(daily_water_ml, 2),
        "water_usage_l": round(daily_water_ml / 1000.0, 2),
    }
    if extra:
        entry.update(extra)
    return entry


def _daily_summary(
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
        "source": _experiment_source(sensor_context),
    }
    if sensor_context is not None:
        summary.update(_sensor_summary_fields(sensor_context))
    return summary


def _day_profile(day: date, day_weather: list[dict[str, Any]], weather_by_day: dict[date, list[dict[str, Any]]]) -> dict[str, Any]:
    temperatures = [_number(row["temperature_c"], 20.0) for row in day_weather]
    humidities = [_number(row["relative_humidity_pct"], 60.0) for row in day_weather]
    cloud_covers = [_weather_cloud_cover_pct(row, 0.0) for row in day_weather]
    precipitation = sum(_number(row["precipitation_mm"], 0.0) for row in day_weather)
    reference_et = sum(_hourly_reference_et_mm(row) for row in day_weather)
    rain_probabilities = [_number(row.get("precipitation_probability_pct"), 0.0) for row in day_weather]
    gusts = [_number(row["wind_gust_kmh"], _number(row["wind_speed_kmh"], 0.0)) for row in day_weather]
    freeze_risk = min(temperatures) <= 0 or _upcoming_freeze(day, weather_by_day)
    no_rain_10_days = _precipitation_last_days(day, weather_by_day, days=10) < 1.0

    max_temperature = max(temperatures)
    max_gust = max(gusts)
    avg_humidity = sum(humidities) / max(len(humidities), 1)
    avg_cloud_cover = sum(cloud_covers) / max(len(cloud_covers), 1)

    return {
        "season": _season(day),
        "avg_temperature_c": sum(temperatures) / max(len(temperatures), 1),
        "max_temperature_c": max_temperature,
        "min_temperature_c": min(temperatures),
        "avg_humidity_pct": avg_humidity,
        "avg_cloud_cover_pct": avg_cloud_cover,
        "precipitation_mm": precipitation,
        "reference_evapotranspiration_mm": reference_et,
        "max_precipitation_probability_pct": max(rain_probabilities) if rain_probabilities else 0.0,
        "max_wind_gust_kmh": max_gust,
        "heatwave_day": max_temperature >= 30.0,
        "dry_windy_day": max_gust >= 35.0 and avg_humidity <= 55.0,
        "freeze_risk": freeze_risk,
        "no_rain_10_days": no_rain_10_days,
    }


def _weather_cloud_cover_pct(weather: dict[str, Any], default: float) -> float:
    value = weather.get("cloud_cover_pct")
    if value is None:
        raw_payload = weather.get("raw_payload")
        if isinstance(raw_payload, dict):
            value = raw_payload.get("cloud_cover")
    return _number(value, default)


def _hourly_reference_et_mm(weather: dict[str, Any]) -> float:
    evap_mm = _number(weather.get("evapotranspiration_mm"), None)
    if evap_mm is not None:
        return max(0.0, evap_mm)

    temp = _number(weather.get("temperature_c"), 20.0)
    humidity = _number(weather.get("relative_humidity_pct"), 60.0)
    wind = _number(weather.get("wind_speed_kmh"), 5.0)
    return max(0.01, 0.025 + (temp / 38.0) * ((100.0 - humidity) / 100.0) * (1.0 + wind / 45.0))


def _apply_hourly_environment(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any], local_day: date) -> None:
    outdoor_by_season = pot.get("_outdoor_by_season")
    outdoor = bool(outdoor_by_season.get(day_profile["season"])) if outdoor_by_season is not None else _is_outdoor(pot, local_day)
    if outdoor:
        evap_mm = _number(weather["evapotranspiration_mm"], None)
        if evap_mm is None:
            temp = _number(weather["temperature_c"], 20.0)
            humidity = _number(weather["relative_humidity_pct"], 60.0)
            wind = _number(weather["wind_speed_kmh"], 5.0)
            evap_mm = max(0.01, 0.025 + (temp / 38.0) * ((100.0 - humidity) / 100.0) * (1.0 + wind / 45.0))

        loss = evap_mm * pot["evaporation_factor"] * pot["_sun_factor"] * pot["_wind_factor"]
        if pot["plant_type_code"] in {"vegetables", "herbs"}:
            loss *= 1.12
        elif pot["plant_type_code"] == "succulents":
            loss *= 0.48

        rain_gain = min(8.0, _number(weather["precipitation_mm"], 0.0) * 0.85)
        state.moisture += rain_gain - loss
    else:
        state.moisture -= 0.018 if pot["plant_type_code"] != "succulents" else 0.006

    state.moisture = _clamp(state.moisture, 0.0, 100.0)


def _generate_database_anfis_dataset(
    weather_rows: list[dict[str, Any]],
    pots: list[dict[str, Any]],
    samples: int,
    seed: int | None,
    sensor_context: dict[str, Any] | None = None,
    weather_by_day: dict[date, list[dict[str, Any]]] | None = None,
    day_profiles: dict[date, dict[str, Any]] | None = None,
) -> list[dict[str, float | str]]:
    rng = random.Random(seed)
    weather_by_day = weather_by_day or _group_weather_by_day(weather_rows)
    day_profiles = day_profiles or {}
    dataset = []

    for _ in range(samples):
        weather = rng.choice(weather_rows)
        pot = rng.choice(pots)
        observed_local = _local_observed_at(weather)
        observed_date = observed_local.date()
        day_weather = weather_by_day.get(observed_date, [weather])
        day_profile = day_profiles.get(observed_date) or _day_profile(observed_date, day_weather, weather_by_day)
        threshold = _threshold_for_pot(pot, day_profile, "morning")
        target = pot["moisture_target_pct"]
        sensor_reading = _sensor_reading_for_pot(
            sensor_context,
            pot,
            observed_local.date(),
            observed_local.hour,
        )

        if sensor_reading:
            moisture = _number(sensor_reading["soil_moisture_pct"], target)
            temperature = _number(sensor_reading["air_temperature_c"], _number(weather["temperature_c"], 20.0))
            humidity = _number(sensor_reading["air_humidity_pct"], _number(weather["relative_humidity_pct"], 60.0))
        else:
            moisture = _clamp(rng.gauss(target, 16.0), 0.0, 100.0)
            temperature = _number(weather["temperature_c"], 20.0)
            humidity = _number(weather["relative_humidity_pct"], 60.0)

        if day_profile["freeze_risk"] or day_profile["precipitation_mm"] >= 2.0 and moisture > threshold * 0.85:
            probability = 0.12
        elif moisture < threshold:
            probability = 0.85
        elif moisture < target and (temperature >= 28.0 or day_profile["dry_windy_day"]):
            probability = 0.52
        else:
            probability = 0.18

        dataset.append(
            {
                "moisture": float(moisture),
                "temperature": float(temperature),
                "humidity": float(humidity),
                "target_probability": probability,
                "target_category": probability_category(probability),
            }
        )

    return dataset


def _evaluate_anfis_model(model: ANFIS, dataset: list[dict[str, float | str]]) -> dict[str, Any]:
    matches = 0
    mse = 0.0
    for item in dataset:
        predicted = model.predict(item)
        target_probability = float(item["target_probability"])
        mse += (predicted - target_probability) ** 2
        if probability_category(predicted) == item["target_category"]:
            matches += 1

    mse /= max(len(dataset), 1)
    return {
        "test_mse": round(mse, 6),
        "test_accuracy_percent": round(matches / max(len(dataset), 1) * 100.0, 2),
        "test_samples": len(dataset),
    }


def _persist_daily_results(
    experiment_type: str,
    start_date: date,
    end_date: date,
    decisions: list[dict[str, Any]],
    events: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    start_ts = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_ts = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    current_day = datetime.now(LOCAL_TZ).date()
    current_day_start = datetime.combine(current_day, time.min, tzinfo=LOCAL_TZ)
    current_day_end = current_day_start + timedelta(days=1)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE irrigation_actuations
            SET status = 'cancelled',
                changed_at = now()
            WHERE status = 'planned'
              AND (
                    scheduled_start_at < %(current_day_start)s
                 OR scheduled_start_at >= %(current_day_end)s
              )
            """,
            {
                "current_day_start": current_day_start,
                "current_day_end": current_day_end,
            },
        )
        conn.execute(
            """
            UPDATE irrigation_actuations
            SET status = 'cancelled',
                changed_at = now()
            WHERE experiment_type = %(experiment_type)s
              AND scheduled_start_at >= %(start_ts)s
              AND scheduled_start_at < %(end_ts)s
              AND status = 'planned'
            """,
            {"experiment_type": experiment_type, "start_ts": start_ts, "end_ts": end_ts},
        )
        conn.execute(
            """
            UPDATE irrigation_events
            SET status = 'cancelled',
                changed_at = now()
            WHERE experiment_type = %(experiment_type)s
              AND scheduled_start_at >= %(start_ts)s
              AND scheduled_start_at < %(end_ts)s
              AND status = 'planned'
            """,
            {"experiment_type": experiment_type, "start_ts": start_ts, "end_ts": end_ts},
        )
        decision_rows = _decision_persist_rows(experiment_type, decisions)
        if decision_rows:
            conn.execute(
                """
                WITH input_rows AS (
                    SELECT *
                    FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
                        experiment_type text,
                        sensor_id bigint,
                        decided_at timestamptz,
                        decision_date date,
                        decision_slot text,
                        should_irrigate boolean,
                        reason_code text,
                        reason_detail text,
                        current_moisture_pct numeric,
                        target_moisture_pct numeric,
                        weather_hourly_id bigint
                    )
                )
                INSERT INTO irrigation_decisions (
                    experiment_type, sensor_id, decided_at, decision_date, decision_slot, should_irrigate,
                    reason_code, reason_detail, current_moisture_pct, target_moisture_pct,
                    weather_hourly_id
                )
                SELECT
                    experiment_type, sensor_id, decided_at, decision_date, decision_slot, should_irrigate,
                    reason_code, reason_detail, current_moisture_pct, target_moisture_pct,
                    weather_hourly_id
                FROM input_rows
                ON CONFLICT (experiment_type, sensor_id, decided_at, decision_slot) DO UPDATE SET
                    decision_date = EXCLUDED.decision_date,
                    should_irrigate = EXCLUDED.should_irrigate,
                    reason_code = EXCLUDED.reason_code,
                    reason_detail = EXCLUDED.reason_detail,
                    current_moisture_pct = EXCLUDED.current_moisture_pct,
                    target_moisture_pct = EXCLUDED.target_moisture_pct,
                    weather_hourly_id = EXCLUDED.weather_hourly_id,
                    changed_at = now()
                """,
                {"rows": Jsonb(decision_rows)},
            )

        decision_ids = _load_persisted_decision_ids(conn, experiment_type, start_ts, end_ts)
        event_rows = _event_persist_rows(experiment_type, events, decision_ids)
        if event_rows:
            conn.execute(
                """
                WITH input_rows AS (
                    SELECT *
                    FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
                        experiment_type text,
                        decision_id bigint,
                        sensor_id bigint,
                        scheduled_start_at timestamptz,
                        scheduled_end_at timestamptz,
                        flow_rate_ml_min numeric,
                        planned_volume_ml numeric,
                        cycle_count integer,
                        soak_pause_min integer
                    )
                )
                INSERT INTO irrigation_events (
                    experiment_type, decision_id, sensor_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, cycle_count, soak_pause_min, status
                )
                SELECT
                    experiment_type, decision_id, sensor_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, cycle_count, soak_pause_min, 'planned'
                FROM input_rows
                ON CONFLICT (experiment_type, sensor_id, scheduled_start_at) DO UPDATE SET
                    decision_id = EXCLUDED.decision_id,
                    scheduled_end_at = EXCLUDED.scheduled_end_at,
                    flow_rate_ml_min = EXCLUDED.flow_rate_ml_min,
                    planned_volume_ml = EXCLUDED.planned_volume_ml,
                    cycle_count = EXCLUDED.cycle_count,
                    soak_pause_min = EXCLUDED.soak_pause_min,
                    status = CASE
                        WHEN irrigation_events.status IN ('completed', 'running') THEN irrigation_events.status
                        ELSE EXCLUDED.status
                    END,
                    changed_at = now()
                """,
                {"rows": Jsonb(event_rows)},
            )

        event_ids = _load_persisted_event_ids(conn, experiment_type, start_ts, end_ts)
        current_day_event_rows = [
            event
            for event in event_rows
            if _local_date(event["scheduled_start_at"]) == current_day
        ]
        actuation_rows = _actuation_persist_rows(current_day_event_rows, event_ids)
        if actuation_rows:
            conn.execute(
                """
                WITH input_rows AS (
                    SELECT *
                    FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
                        event_id bigint,
                        experiment_type text,
                        pot_id bigint,
                        scheduled_start_at timestamptz,
                        scheduled_end_at timestamptz,
                        flow_rate_ml_min numeric,
                        planned_volume_ml numeric,
                        cycle_count integer,
                        soak_pause_min integer
                    )
                )
                INSERT INTO irrigation_actuations (
                    event_id, experiment_type, pot_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, cycle_count, soak_pause_min, status
                )
                SELECT
                    event_id, experiment_type, pot_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, cycle_count, soak_pause_min, 'planned'
                FROM input_rows
                ON CONFLICT (experiment_type, pot_id, scheduled_start_at) DO UPDATE SET
                    event_id = EXCLUDED.event_id,
                    scheduled_end_at = EXCLUDED.scheduled_end_at,
                    flow_rate_ml_min = EXCLUDED.flow_rate_ml_min,
                    planned_volume_ml = EXCLUDED.planned_volume_ml,
                    cycle_count = EXCLUDED.cycle_count,
                    soak_pause_min = EXCLUDED.soak_pause_min,
                    status = CASE
                        WHEN irrigation_actuations.status IN ('completed', 'running') THEN irrigation_actuations.status
                        ELSE EXCLUDED.status
                    END,
                    changed_at = now()
                """,
                {"rows": Jsonb(actuation_rows)},
            )

        alert_rows = _alert_persist_rows(experiment_type, alerts)
        if alert_rows:
            conn.execute(
                """
                WITH input_rows AS (
                    SELECT *
                    FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
                        experiment_type text,
                        pot_id bigint,
                        raised_at timestamptz,
                        alert_type text,
                        severity text,
                        title text,
                        detail text
                    )
                )
                INSERT INTO alerts (experiment_type, pot_id, raised_at, alert_type, severity, title, detail)
                SELECT experiment_type, pot_id, raised_at, alert_type, severity, title, detail
                FROM input_rows
                ON CONFLICT (experiment_type, pot_id, raised_at, alert_type) DO UPDATE SET
                    severity = EXCLUDED.severity,
                    title = EXCLUDED.title,
                    detail = EXCLUDED.detail,
                    changed_at = now()
                """,
                {"rows": Jsonb(alert_rows)},
            )
        conn.commit()


def _decision_persist_rows(experiment_type: str, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for decision in decisions:
        sensor_id = int(decision.get("sensor_id", decision["pot_id"]))
        row = {
            "experiment_type": experiment_type,
            "sensor_id": sensor_id,
            "decided_at": decision["decided_at"],
            "decision_date": decision["date"],
            "decision_slot": decision["slot"],
            "should_irrigate": bool(decision["should_irrigate"]),
            "reason_code": decision["reason_code"],
            "reason_detail": decision["reason_detail"],
            "current_moisture_pct": decision.get("current_moisture_pct"),
            "target_moisture_pct": decision.get("target_moisture_pct"),
            "weather_hourly_id": decision.get("weather_hourly_id"),
        }
        key = (experiment_type, sensor_id, _local_timestamp_key(row["decided_at"]), row["decision_slot"])
        if key in rows_by_key:
            rows_by_key[key] = _merge_sensor_decision_row(rows_by_key[key], row)
        else:
            rows_by_key[key] = row
    return list(rows_by_key.values())


def _merge_sensor_decision_row(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if incoming["should_irrigate"] and not existing["should_irrigate"]:
        merged = dict(incoming)
    else:
        merged = dict(existing)
        merged["should_irrigate"] = bool(existing["should_irrigate"] or incoming["should_irrigate"])
    if existing.get("current_moisture_pct") is not None and incoming.get("current_moisture_pct") is not None:
        merged["current_moisture_pct"] = min(existing["current_moisture_pct"], incoming["current_moisture_pct"])
    if existing.get("target_moisture_pct") is not None and incoming.get("target_moisture_pct") is not None:
        merged["target_moisture_pct"] = max(existing["target_moisture_pct"], incoming["target_moisture_pct"])
    return merged


def _event_persist_rows(
    experiment_type: str,
    events: list[dict[str, Any]],
    decision_ids: dict[tuple[int, str, str], int],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for event in events:
        sensor_id = int(event.get("sensor_id", event["pot_id"]))
        row = {
            "experiment_type": experiment_type,
            "decision_id": decision_ids.get((sensor_id, _local_timestamp_key(event["scheduled_start_at"]), event["slot"])),
            "sensor_id": sensor_id,
            "actuator_pot_id": int(event.get("pot_id", sensor_id)),
            "scheduled_start_at": event["scheduled_start_at"],
            "scheduled_end_at": event["scheduled_end_at"],
            "flow_rate_ml_min": event["flow_rate_ml_min"],
            "planned_volume_ml": event["planned_volume_ml"],
            "cycle_count": int(event["cycle_count"]),
            "soak_pause_min": int(event["soak_pause_min"]),
        }
        key = (experiment_type, sensor_id, _local_timestamp_key(row["scheduled_start_at"]))
        if key in rows_by_key:
            rows_by_key[key] = _merge_sensor_event_row(rows_by_key[key], row)
        else:
            rows_by_key[key] = row
    return list(rows_by_key.values())


def _merge_sensor_event_row(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["planned_volume_ml"] = float(existing["planned_volume_ml"]) + float(incoming["planned_volume_ml"])
    merged["flow_rate_ml_min"] = float(existing["flow_rate_ml_min"]) + float(incoming["flow_rate_ml_min"])
    merged["scheduled_end_at"] = max(existing["scheduled_end_at"], incoming["scheduled_end_at"])
    merged["cycle_count"] = max(int(existing["cycle_count"]), int(incoming["cycle_count"]))
    merged["soak_pause_min"] = max(int(existing["soak_pause_min"]), int(incoming["soak_pause_min"]))
    return merged


def _actuation_persist_rows(
    event_rows: list[dict[str, Any]],
    event_ids: dict[tuple[int, str], int],
) -> list[dict[str, Any]]:
    return [
        {
            "event_id": event_ids.get((int(event["sensor_id"]), _local_timestamp_key(event["scheduled_start_at"]))),
            "experiment_type": event["experiment_type"],
            "pot_id": int(event.get("actuator_pot_id", event["sensor_id"])),
            "scheduled_start_at": event["scheduled_start_at"],
            "scheduled_end_at": event["scheduled_end_at"],
            "flow_rate_ml_min": event["flow_rate_ml_min"],
            "planned_volume_ml": event["planned_volume_ml"],
            "cycle_count": event["cycle_count"],
            "soak_pause_min": event["soak_pause_min"],
        }
        for event in event_rows
    ]


def _local_date(value: str | datetime) -> date:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value)
    else:
        local_value = value
    if local_value.tzinfo is not None:
        local_value = local_value.astimezone(LOCAL_TZ)
    return local_value.date()


def _alert_persist_rows(experiment_type: str, alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for alert in alerts:
        row = {
            "experiment_type": experiment_type,
            "pot_id": int(alert["pot_id"]),
            "raised_at": alert["raised_at"],
            "alert_type": alert["alert_type"],
            "severity": alert["severity"],
            "title": alert["title"],
            "detail": alert["detail"],
        }
        rows_by_key[(experiment_type, row["pot_id"], _local_timestamp_key(row["raised_at"]), row["alert_type"])] = row
    return list(rows_by_key.values())


def _load_persisted_decision_ids(conn, experiment_type: str, start_ts: datetime, end_ts: datetime) -> dict[tuple[int, str, str], int]:
    rows = conn.execute(
        """
        SELECT id, sensor_id, decided_at AT TIME ZONE 'Europe/Bucharest' AS decided_local_at, decision_slot
        FROM irrigation_decisions
        WHERE experiment_type = %(experiment_type)s
          AND decided_at >= %(start_ts)s
          AND decided_at < %(end_ts)s
        """,
        {"experiment_type": experiment_type, "start_ts": start_ts, "end_ts": end_ts},
    ).fetchall()
    return {
        (int(row[1]), _local_timestamp_key(row[2]), row[3]): int(row[0])
        for row in rows
    }


def _load_persisted_event_ids(conn, experiment_type: str, start_ts: datetime, end_ts: datetime) -> dict[tuple[int, str], int]:
    rows = conn.execute(
        """
        SELECT id, sensor_id, scheduled_start_at AT TIME ZONE 'Europe/Bucharest' AS scheduled_local_at
        FROM irrigation_events
        WHERE experiment_type = %(experiment_type)s
          AND scheduled_start_at >= %(start_ts)s
          AND scheduled_start_at < %(end_ts)s
        """,
        {"experiment_type": experiment_type, "start_ts": start_ts, "end_ts": end_ts},
    ).fetchall()
    return {
        (int(row[1]), _local_timestamp_key(row[2])): int(row[0])
        for row in rows
    }


def _local_timestamp_key(value: str | datetime) -> str:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value)
    else:
        local_value = value
    if local_value.tzinfo is not None:
        local_value = local_value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return local_value.replace(microsecond=0).isoformat()



