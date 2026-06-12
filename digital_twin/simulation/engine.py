from __future__ import annotations

import random
import math
import time as perf_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

from digital_twin.db.connection import get_connection
from digital_twin.db.schema import initialize_database
from digital_twin.domain.irrigation_methods import VALVE_ZONE_DESIGN, VALVE_ZONE_ORDER
from digital_twin.experiments.anfis import ANFIS, probability_category, target_probability
from digital_twin.simulation.dto import (
    ANFIS_DECISION_THRESHOLD,
    ANFIS_FORECAST_DECISION_THRESHOLD,
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
    DEFAULT_FUZZY_POLICY,
    DEFAULT_IRRIGATION_POLICY,
    _alert_row,
    _apply_event_delivery,
    _is_emergency_dryness,
    _is_outdoor,
    _make_baseline_irrigation_decision,
    _make_fuzzy_dt_decision,
    _precipitation_last_days,
    _rain_exposure_factor,
    _threshold_for_pot,
    _upcoming_freeze,
)
from digital_twin.services.sensor_readings import (
    ACTUAL_SENSOR_SOURCE,
    DEFAULT_SENSOR_SOURCE,
    ensure_sensor_readings_for_experiment_range,
    load_sensor_readings_for_experiment,
)


BASELINE_WINTER_LOOKAHEAD_DAYS = 14
MORNING_SENSOR_CALIBRATION_TIME = time(5, 30)
EVENING_SENSOR_CALIBRATION_TIME = time(17, 30)
SPARSE_FORECAST_SENSOR_SOURCE = "forecast_simulated_sensor"
ANFIS_HARD_STOP_REASON_CODES = {
    "freeze_risk",
    "winter_indoor_not_valve_managed",
    "anfis_cold_skip",
}
ANFIS_ZONE_MODEL_MIN_SAMPLES = 120
ANFIS_CALIBRATION_SHARE = 0.18
ANFIS_TRAINING_DATASET_VERSION = 4


def load_experiment_snapshot(start_date: date, end_date: date) -> ExperimentSnapshot:
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    initialize_database()
    pots = _load_active_pots()
    if not pots:
        raise ValueError("No active pots found in the database")

    sensor_context = _load_sensor_context(start_date, end_date, pots)
    weather_start = _snapshot_weather_start(start_date, sensor_context)
    weather_end = (
        end_date + timedelta(days=BASELINE_WINTER_LOOKAHEAD_DAYS)
        if _baseline_winter_lookahead_needed(start_date, end_date)
        else end_date
    )
    weather_rows = _load_weather(weather_start, weather_end)
    _raise_if_missing_historical_weather(weather_rows, start_date, end_date)
    weather_rows, estimated_weather_rows = _with_estimated_future_weather(weather_rows, weather_start, weather_end)
    selected_weather_rows = [
        row for row in weather_rows
        if start_date <= _local_observed_at(row).date() <= end_date
    ]
    estimated_selected_weather_rows = sum(1 for row in selected_weather_rows if row.get("source") == "estimated-weather")
    estimated_lookahead_weather_rows = sum(
        1
        for row in weather_rows
        if row.get("source") == "estimated-weather"
        and not start_date <= _local_observed_at(row).date() <= end_date
    )
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
        estimated_selected_weather_rows=estimated_selected_weather_rows,
        estimated_lookahead_weather_rows=estimated_lookahead_weather_rows,
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


def _resolve_anfis_training_snapshot(
    start_date: date,
    end_date: date,
    fallback_snapshot: ExperimentSnapshot,
) -> ExperimentSnapshot:
    training_start = min(start_date, _state_simulation_start(end_date, end_date))
    try:
        return load_experiment_snapshot(training_start, end_date)
    except ValueError:
        return fallback_snapshot


def _state_simulation_start(start_date: date, end_date: date) -> date:
    season_start = _active_season_start_date(end_date)
    if season_start is None:
        return start_date

    anchor_date = _historical_state_anchor_date(end_date)
    warmup_start = season_start
    if anchor_date is not None:
        warmup_start = max(season_start, anchor_date)
    return min(start_date, warmup_start)


def _active_season_start_date(day: date) -> date | None:
    if DEFAULT_IRRIGATION_POLICY.dormant_period(day):
        return None
    return date(day.year, 4, 1)


def _historical_state_anchor_date(end_date: date) -> date | None:
    try:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT
                    (
                        SELECT min(recorded_at::date)
                        FROM sensor_readings
                        WHERE recorded_at::date <= %(end_date)s
                    ) AS sensor_start,
                    (
                        SELECT min(observed_local_at::date)
                        FROM weather_hourly
                        WHERE observed_local_at::date <= %(end_date)s
                    ) AS weather_start
                """,
                {"end_date": end_date},
            ).fetchone()
    except Exception:
        return None

    if not row:
        return None

    sensor_start = row.get("sensor_start")
    weather_start = row.get("weather_start")
    if sensor_start and weather_start:
        return max(sensor_start, weather_start)
    return sensor_start or weather_start


def _resolve_simulation_snapshot(
    start_date: date,
    end_date: date,
    selected_snapshot: ExperimentSnapshot,
) -> tuple[date, ExperimentSnapshot]:
    simulation_start_date = _state_simulation_start(start_date, end_date)
    if simulation_start_date >= start_date:
        return start_date, selected_snapshot

    try:
        return simulation_start_date, load_experiment_snapshot(simulation_start_date, end_date)
    except ValueError:
        return start_date, selected_snapshot


def _uses_hourly_chart(start_date: date, end_date: date) -> bool:
    return (end_date - start_date).days < HOURLY_CHART_MAX_RANGE_DAYS


def _new_daily_moisture_tracker() -> dict[str, Any]:
    return {"snapshots": []}


def _daily_moisture_snapshot_label(day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
    slot = DEFAULT_IRRIGATION_POLICY.decision_slot(day, observed_at, day_profile)
    if slot:
        return f"before_{slot}"

    hour = observed_at.hour
    max_temp = _number(day_profile.get("max_temperature_c"), 20.0)
    if day.month in {12, 1, 2, 3} and hour == 13:
        return "after_winter_check"
    if hour == 9:
        return "after_morning"
    if max_temp >= 32.0 and hour == 21:
        return "after_evening"
    return None


def _post_irrigation_snapshot_index(
    day: date,
    weather_rows: list[dict[str, Any]],
    start_index: int,
    day_profile: dict[str, Any],
) -> tuple[int | None, str | None]:
    for index in range(max(0, start_index), len(weather_rows)):
        observed_local = _local_observed_at(weather_rows[index])
        if observed_local.date() != day:
            continue
        label = _daily_moisture_snapshot_label(day, observed_local, day_profile)
        if label and label.startswith("after_"):
            return index, label
    return None, None


def _record_daily_moisture_snapshot(
    tracker: dict[str, Any],
    pot_states: dict[int, PotState],
    label: str,
) -> None:
    moistures = [state.moisture for state in pot_states.values()]
    if not moistures:
        return
    tracker.setdefault("snapshots", []).append(
        {
            "label": label,
            "average_moisture": sum(moistures) / len(moistures),
            "min_moisture": min(moistures),
            "max_moisture": max(moistures),
        }
    )


def _daily_moisture_summary(
    tracker: dict[str, Any],
    pot_states: dict[int, PotState],
) -> dict[str, Any]:
    snapshots = tracker.get("snapshots") or []
    moistures = [state.moisture for state in pot_states.values()]
    avg_moisture = sum(moistures) / max(len(moistures), 1)
    end_of_day_summary = {
        "moisture": round(avg_moisture, 2),
        "average_moisture": round(avg_moisture, 2),
        "min_moisture": round(min(moistures), 2),
        "max_moisture": round(max(moistures), 2),
    }
    if not snapshots:
        return {
            **end_of_day_summary,
            "moisture_sample_count": 0,
            "moisture_sample_method": "end_of_day",
        }

    post_window_snapshots = [
        item for item in snapshots
        if str(item.get("label") or "").startswith("after_")
    ]
    pre_window_snapshots = [
        item for item in snapshots
        if str(item.get("label") or "").startswith("before_")
    ]
    pre_window_moisture = (
        sum(float(item["average_moisture"]) for item in pre_window_snapshots) / len(pre_window_snapshots)
        if pre_window_snapshots
        else None
    )
    post_window_moisture = (
        sum(float(item["average_moisture"]) for item in post_window_snapshots) / len(post_window_snapshots)
        if post_window_snapshots
        else None
    )
    return {
        **end_of_day_summary,
        "moisture_sample_count": len(snapshots),
        "moisture_sample_method": "end_of_day",
        "moisture_sample_labels": [str(item["label"]) for item in snapshots],
        "pre_irrigation_moisture": round(pre_window_moisture, 2) if pre_window_moisture is not None else None,
        "post_irrigation_moisture": round(post_window_moisture, 2) if post_window_moisture is not None else None,
    }


def _baseline_winter_lookahead_needed(start_date: date, end_date: date) -> bool:
    current = start_date
    while current <= end_date:
        if current.month in {12, 1, 2, 3}:
            return True
        current += timedelta(days=1)
    return False


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


def _sampling_moisture_chart_summary(
    rows: list[dict[str, Any]],
    sample_interval_hours: int,
    hourly: bool,
) -> dict[str, int]:
    if not rows:
        return {"sample_count": 0, "sample_interval_rows": 0}

    sample_interval_rows = max(1, sample_interval_hours if hourly else round(sample_interval_hours / 24))
    sample_count = 0
    has_sample_flags = any("sparse_sensor_sample" in row for row in rows)

    for index, row in enumerate(rows):
        raw_sparse = _number(row.get("sparse_moisture"), None)
        if raw_sparse is not None:
            row["sparse_moisture_raw"] = round(raw_sparse, 2)

        sample_now = bool(row.get("sparse_sensor_sample")) if has_sample_flags else index % sample_interval_rows == 0
        if sample_now:
            sample_count += 1

    return {"sample_count": sample_count, "sample_interval_rows": sample_interval_rows}


def _strategy_comparison_baseline(
    start_date: date,
    end_date: date,
    snapshot: ExperimentSnapshot,
    baseline_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = (baseline_result or {}).get("summary") or {}
    if (
        baseline_result is not None
        and summary.get("baselineSensorCalibrationPolicy") == "continuous_sensor_calibration"
    ):
        return baseline_result

    return run_default_dt_irrigation_control(
        start_date=start_date,
        end_date=end_date,
        persist=False,
        snapshot=snapshot,
    )


def run_default_dt_irrigation_control(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    state_anchor_policy: str = "stable_historical_timeline",
    sensor_calibration_policy: str = "continuous_sensor_calibration",
) -> dict[str, Any]:
    """Run the database-backed default DT irrigation control strategy.

    The strategy reads stored weather and seeded pot inventory from Postgres.
    It returns daily aggregate rows for charting.
    """
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    if state_anchor_policy == "experiment_start_sensor_anchor":
        simulation_start_date = start_date
        simulation_snapshot = selected_snapshot
    else:
        simulation_start_date, simulation_snapshot = _resolve_simulation_snapshot(start_date, end_date, selected_snapshot)
    weather_rows = selected_snapshot.selected_weather_rows
    pots = simulation_snapshot.pots
    zone_pots = _pots_by_valve_zone(pots)
    sensor_context = simulation_snapshot.sensor_context
    selected_sensor_context = selected_snapshot.sensor_context
    control_pots = _sensor_control_pots(pots, sensor_context)
    control_pot_ids = {int(pot["id"]) for pot in control_pots}
    weather_by_day = simulation_snapshot.weather_by_day
    pot_states = _copy_pot_states(simulation_snapshot.initial_pot_states)
    sensor_state_anchor = _initialize_states_from_first_day_sensor_readings(
        pot_states,
        pots,
        sensor_context,
        simulation_start_date,
    )
    state_at_experiment_start: dict[str, dict[str, float | int]] | None = None

    entries = []
    detail_entries = []
    decisions = []
    events = []
    alerts = []

    total_water_ml = 0.0
    total_irrigation_events = 0
    total_irrigation_decisions = 0
    current_date = simulation_start_date
    while current_date <= end_date:
        if current_date == start_date and state_at_experiment_start is None:
            state_at_experiment_start = _serialize_pot_states(pot_states)

        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        record_date = current_date >= start_date
        day_profile = simulation_snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_moisture_tracker = _new_daily_moisture_tracker()

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current_date, observed_local, day_profile)
            decision_by_pot_id: dict[int, dict[str, Any]] = {}
            zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

            for pot in pots:
                state = pot_states[pot["id"]]
                _apply_hourly_environment(
                    state,
                    pot,
                    hour_weather,
                    day_profile,
                    observed_local.date(),
                    rain_exposure_factor=_rain_exposure_factor(pot, observed_local.date()),
                )
                if sensor_calibration_policy != "initial_state_only" and int(pot["id"]) in control_pot_ids:
                    _apply_sensor_calibration_marker(state, pot, current_date, observed_local, sensor_context, day_profile)

                if slot is None:
                    if int(pot["id"]) in control_pot_ids and _is_emergency_dryness(state, pot, current_date, observed_local):
                        if record_date:
                            alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                            daily_alerts += 1
                            hourly_alerts += 1
            if slot is not None:
                for pot in control_pots:
                    state = pot_states[pot["id"]]
                    decision = _with_sensor_key(
                        _make_baseline_irrigation_decision(state, pot, hour_weather, day_profile, slot),
                        pot,
                        sensor_context,
                    )
                    decision = _apply_cold_month_indoor_skip(decision, pot, current_date)
                    decision_by_pot_id[int(pot["id"])] = decision
                    if record_date:
                        decisions.append(decision)
                        daily_decisions += 1
                        hourly_decisions += 1

                    if decision["should_irrigate"] and _is_valve_managed_pot(pot, current_date):
                        zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

            snapshot_label = _daily_moisture_snapshot_label(current_date, observed_local, day_profile)
            if record_date and snapshot_label:
                _record_daily_moisture_snapshot(daily_moisture_tracker, pot_states, snapshot_label)

            if slot is not None:
                for zone, trigger_decisions in zone_trigger_decisions.items():
                    trigger_pot_ids = _trigger_pot_ids(trigger_decisions)
                    trigger_sensor_ids = _trigger_sensor_ids(trigger_decisions)
                    trigger_pot_codes = _trigger_pot_codes(trigger_decisions)
                    zone_dose_factor = _baseline_zone_dose_factor(trigger_decisions)
                    execution_decisions = _zone_execution_decision_map(
                        decision_by_pot_id,
                        zone_pots,
                        zone,
                        current_date,
                        trigger_decisions,
                    )
                    zone_events = _execute_valve_zone_distribution(
                        pot_states,
                        zone_pots,
                        zone,
                        current_date,
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
                    for event in zone_events:
                        if record_date:
                            events.append(event)
                            daily_events += 1
                            daily_water_ml += event["planned_volume_ml"]
                            hourly_events += 1
                            hourly_water_ml += event["planned_volume_ml"]

                for pot in pots:
                    state = pot_states[pot["id"]]
                    if state.moisture > pot["moisture_max_pct"]:
                        state.too_wet_hours += 1
                        if record_date and state.too_wet_hours == 24:
                            alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                            daily_alerts += 1
                            hourly_alerts += 1
                    else:
                        state.too_wet_hours = 0

            if record_date and _uses_hourly_chart(start_date, end_date):
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
                        _hourly_line_metadata(selected_sensor_context, current_date, observed_local, hour_weather),
                    )
                )

        if not record_date:
            current_date += timedelta(days=1)
            continue

        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions
        moisture_summary = _daily_moisture_summary(daily_moisture_tracker, pot_states)

        entries.append(
            {
                "date": current_date.isoformat(),
                "timestamp": datetime.combine(current_date, time(12, 0), tzinfo=LOCAL_TZ).isoformat(),
                "day_label": current_date.strftime("%Y-%m-%d"),
                "chart_label": current_date.strftime("%Y-%m-%d"),
                "moisture": moisture_summary["moisture"],
                "average_moisture": moisture_summary["average_moisture"],
                "min_moisture": moisture_summary["min_moisture"],
                "max_moisture": moisture_summary["max_moisture"],
                "moisture_sample_count": moisture_summary.get("moisture_sample_count", 0),
                "moisture_sample_method": moisture_summary.get("moisture_sample_method", "end_of_day"),
                "moisture_sample_labels": moisture_summary.get("moisture_sample_labels", []),
                "pre_irrigation_moisture": moisture_summary.get("pre_irrigation_moisture"),
                "post_irrigation_moisture": moisture_summary.get("post_irrigation_moisture"),
                "temperature": round(day_profile["avg_temperature_c"], 2),
                "max_temperature": round(day_profile["max_temperature_c"], 2),
                "min_temperature": round(day_profile["min_temperature_c"], 2),
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
                **_daily_line_metadata(selected_sensor_context, current_date, day_weather),
            }
        )
        current_date += timedelta(days=1)

    valve_rollup = _apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

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
        "stateSimulationStartDate": simulation_start_date.isoformat(),
        "stateLookbackDays": (end_date - simulation_start_date).days + 1,
        "stateAnchorPolicy": state_anchor_policy,
        "baselineSensorCalibrationPolicy": sensor_calibration_policy,
        "source": _experiment_source(selected_sensor_context),
    }
    summary.update(_sensor_control_summary_fields(pots, selected_sensor_context))
    if sensor_state_anchor is not None:
        summary["stateSensorAnchor"] = sensor_state_anchor
    summary.update(_sensor_summary_fields(selected_sensor_context))
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
        "stateAtExperimentStart": state_at_experiment_start or _serialize_pot_states(pot_states),
    }


def run_daily_sampling_experiment(
    start_date: date,
    end_date: date,
    sample_interval_days: int = 3,
    sample_interval_hours: int | None = None,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    baseline_result: dict[str, Any] | None = None,
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
    baseline = baseline_result or run_default_dt_irrigation_control(
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
        baseline_result=baseline,
    )

    comparison = _ExperimentComparison(start_date, end_date, snapshot, baseline, sparse, "sparse", "sparse_water_usage_l")

    def sparse_row(baseline_entry: dict[str, Any], sparse_entry: dict[str, Any]) -> dict[str, Any]:
        return comparison.row(
            baseline_entry,
            sparse_entry,
            {
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
                "sparse_sensor_sample": bool(sparse_entry.get("sparse_sensor_sample")),
                "sparse_sensor_samples": int(sparse_entry.get("sparse_sensor_samples") or 0),
            },
        )

    entries = comparison.daily_entries(sparse_row)
    chart_entries = comparison.chart_entries(sparse_row)
    matches = sum(1 for entry in entries if entry["baseline_irrigation_active"] == entry["sparse_irrigation_active"])
    mismatches = len(entries) - matches

    table_moisture_display = _sampling_moisture_chart_summary(entries, sample_interval_hours, hourly=False)
    chart_moisture_display = _sampling_moisture_chart_summary(
        chart_entries,
        sample_interval_hours,
        hourly=_uses_hourly_chart(start_date, end_date) and bool(chart_entries),
    )

    total_days = len(entries)
    baseline_summary = comparison.baseline_summary
    sparse_summary = comparison.comparison_summary
    baseline_only_irrigation_days = [
        entry for entry in entries
        if entry["baseline_irrigation_active"] and not entry["sparse_irrigation_active"]
    ]
    sparse_only_irrigation_days = [
        entry for entry in entries
        if entry["sparse_irrigation_active"] and not entry["baseline_irrigation_active"]
    ]
    baseline_only_valve_runs = sum(int(entry["baseline_valve_runs"] or 0) for entry in baseline_only_irrigation_days)
    baseline_only_water_l = sum(float(entry["baseline_water_usage_l"] or 0.0) for entry in baseline_only_irrigation_days)
    missed_valve_run_delta = sum(
        max(0, int(entry["baseline_valve_runs"] or 0) - int(entry["sparse_valve_runs"] or 0))
        for entry in entries
    )
    sparse_extra_valve_run_delta = sum(
        max(0, int(entry["sparse_valve_runs"] or 0) - int(entry["baseline_valve_runs"] or 0))
        for entry in entries
    )
    totals = comparison.total_usage_counts(entries)
    summary = {
        **comparison.base_summary(entries),
        "sample_interval_days": sample_interval_days,
        "sample_interval_hours": sample_interval_hours,
        "sample_interval": sample_interval_days,
        "accuracy_percent": round(matches / max(total_days, 1) * 100.0, 2),
        "mismatch_days": mismatches,
        "mismatch_steps": mismatches,
        **totals,
        **comparison.decision_counts(),
        "sampledWeatherRows": sparse_summary.get("sampledWeatherRows", 0),
        "sampledSensorRows": sparse_summary.get("sampledSensorRows", 0),
        "sampledSensorMoments": sparse_summary.get("sampledSensorMoments", 0),
        "samplingDataPolicy": sparse_summary.get("samplingDataPolicy", "sensor-and-weather-sampled"),
        "forecastSensorCalibrationPolicy": sparse_summary.get("forecastSensorCalibrationPolicy"),
        "weatherSamplingPolicy": sparse_summary.get("weatherSamplingPolicy", "current-weather"),
        "sparseSimulationGranularity": sparse_summary.get("sparseSimulationGranularity", "hourly_state_decision_slot_control"),
        "sampling_moisture_mae_pct": sparse_summary.get("sampling_moisture_mae_pct", 0),
        "sampling_moisture_bias_pct": sparse_summary.get("sampling_moisture_bias_pct", 0),
        "sampling_moisture_max_error_pct": sparse_summary.get("sampling_moisture_max_error_pct", 0),
        "sampling_estimation_points": sparse_summary.get("sampling_estimation_points", 0),
        "sampling_sensor_refreshes": sparse_summary.get("sampling_sensor_refreshes", 0),
        "sampling_direct_refreshes": sparse_summary.get("sampling_direct_refreshes", 0),
        "sampling_associated_refreshes": sparse_summary.get("sampling_associated_refreshes", 0),
        "sampling_forecast_refreshes": sparse_summary.get("sampling_forecast_refreshes", 0),
        "sampling_missing_refreshes": sparse_summary.get("sampling_missing_refreshes", 0),
        "sampling_average_association_distance": sparse_summary.get("sampling_average_association_distance", 0),
        "baseline_only_irrigation_days": len(baseline_only_irrigation_days),
        "sparse_only_irrigation_days": len(sparse_only_irrigation_days),
        "baseline_only_valve_runs": baseline_only_valve_runs,
        "baseline_only_water_usage_l": round(baseline_only_water_l, 2),
        "missed_valve_run_delta": missed_valve_run_delta,
        "sparse_extra_valve_run_delta": sparse_extra_valve_run_delta,
        "sparse_moisture_display_policy": "simulated_between_sensor_calibrations",
        "sparse_moisture_chart_samples": chart_moisture_display["sample_count"],
        "sparse_moisture_table_samples": table_moisture_display["sample_count"],
        "decisionLevel": "valve_zone",
        "execution_time_seconds": round(perf_time.perf_counter() - start_time, 3),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "stateSimulationStartDate": sparse_summary.get(
            "stateSimulationStartDate",
            baseline_summary.get("stateSimulationStartDate"),
        ),
        "stateLookbackDays": sparse_summary.get(
            "stateLookbackDays",
            baseline_summary.get("stateLookbackDays"),
        ),
        "stateAnchorPolicy": sparse_summary.get("stateAnchorPolicy"),
        "samplingWarmupReusePolicy": sparse_summary.get("samplingWarmupReusePolicy"),
        "baselineWarmupStateSimulationStartDate": sparse_summary.get(
            "baselineWarmupStateSimulationStartDate",
        ),
        "baselineWarmupStateLookbackDays": sparse_summary.get("baselineWarmupStateLookbackDays"),
        "source": baseline_summary.get("source", "database-weather-and-pot-inventory"),
    }
    summary.update(comparison.sensor_summary())
    return comparison.payload(entries, chart_entries, summary)


def run_daily_fuzzy_dt_experiment(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    baseline_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the fuzzy DT prescription controller with default DT usage.

    The fuzzy controller uses the same three measured inputs as ANFIS:
    soil moisture, temperature, and rain. Physical execution is constrained to
    valve zones, so a triggered zone waters all currently managed pots. The
    comparator is the application's normal threshold/weather rule controller.
    """
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    start_time = perf_time.perf_counter()
    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    baseline = _strategy_comparison_baseline(start_date, end_date, snapshot, baseline_result)
    fuzzy = _run_fuzzy_dt_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        persist=persist,
        snapshot=snapshot,
    )

    comparison = _ExperimentComparison(start_date, end_date, snapshot, baseline, fuzzy, "fuzzy", "fuzzy_water_usage_l")

    def fuzzy_row(baseline_entry: dict[str, Any], fuzzy_entry: dict[str, Any]) -> dict[str, Any]:
        prescription_mm = fuzzy_entry.get("fuzzy_prescription_mm", fuzzy_entry.get("avg_prescription_mm", 0.0))
        return comparison.row(
            baseline_entry,
            fuzzy_entry,
            {
                "fuzzy_prescription_mm": prescription_mm,
                "avg_prescription_mm": fuzzy_entry.get("avg_prescription_mm", prescription_mm),
            },
            include_moisture_alias=True,
        )

    entries = comparison.daily_entries(fuzzy_row)
    chart_entries = comparison.chart_entries(fuzzy_row)
    totals = comparison.total_usage_counts(entries)
    baseline_agreement_matches = sum(
        1
        for entry in entries
        if entry["baseline_irrigation_active"] == entry["fuzzy_irrigation_active"]
    )
    baseline_mismatch_days = len(entries) - baseline_agreement_matches
    baseline_only_irrigation_days = [
        entry
        for entry in entries
        if entry["baseline_irrigation_active"] and not entry["fuzzy_irrigation_active"]
    ]
    fuzzy_only_irrigation_days = [
        entry
        for entry in entries
        if entry["fuzzy_irrigation_active"] and not entry["baseline_irrigation_active"]
    ]
    missed_valve_run_delta = sum(
        max(0, int(entry["baseline_valve_runs"] or 0) - int(entry["fuzzy_valve_runs"] or 0))
        for entry in entries
    )
    fuzzy_extra_valve_run_delta = sum(
        max(0, int(entry["fuzzy_valve_runs"] or 0) - int(entry["baseline_valve_runs"] or 0))
        for entry in entries
    )
    fuzzy_summary = fuzzy["summary"]
    control_pots = _sensor_control_pots(snapshot.pots, snapshot.sensor_context)
    baseline_water = float(totals["baseline_total_water_usage_l"])
    fuzzy_water = float(totals["fuzzy_total_water_usage_l"])
    water_savings_l = baseline_water - fuzzy_water
    water_savings_percent = round((water_savings_l / baseline_water) * 100.0, 2) if baseline_water > 0 else 0.0
    comfort_metrics = _moisture_safe_savings_metrics(
        entries,
        "fuzzy",
        control_pots,
        water_savings_percent,
        comfort_threshold_pct=_fuzzy_comfort_threshold_pct(control_pots),
    )
    summary = {
        **comparison.base_summary(entries),
        **comparison.irrigation_day_counts(entries),
        **totals,
        **comparison.decision_counts(),
        "baseline_agreement_percent": round(baseline_agreement_matches / max(len(entries), 1) * 100.0, 2),
        "baseline_mismatch_days": baseline_mismatch_days,
        "baseline_only_irrigation_days": len(baseline_only_irrigation_days),
        "fuzzy_only_irrigation_days": len(fuzzy_only_irrigation_days),
        "missed_valve_run_delta": missed_valve_run_delta,
        "fuzzy_extra_valve_run_delta": fuzzy_extra_valve_run_delta,
        "water_savings_l": round(water_savings_l, 2),
        "water_savings_percent": water_savings_percent,
        **comfort_metrics,
        "average_prescription_mm": fuzzy_summary.get("averagePrescriptionMm", 0.0),
        "fuzzyDataPolicy": fuzzy_summary.get("fuzzyDataPolicy", "daily-fis-prescription"),
        "fuzzyControllerPolicy": fuzzy_summary.get("fuzzyControllerPolicy", "fuzzy_dt_prescription_control"),
        "fuzzyDecisionPolicy": fuzzy_summary.get("fuzzyDecisionPolicy", "daily_zone_prescription_control"),
        "fuzzyActuationPolicy": fuzzy_summary.get("fuzzyActuationPolicy", FUZZY_ACTUATION_POLICY.summary()),
        "fuzzySensorCalibrationPolicy": fuzzy_summary.get("fuzzySensorCalibrationPolicy", "initial_state_only"),
        "controllerInputPolicy": fuzzy_summary.get("controllerInputPolicy"),
        "sensorDecisionPotCount": fuzzy_summary.get("sensorDecisionPotCount"),
        "sensorThresholdPolicy": fuzzy_summary.get("sensorThresholdPolicy"),
        "comparisonBaseline": "default_dt_threshold_control",
        "comparisonBaselineStateAnchorPolicy": baseline.get("summary", {}).get("stateAnchorPolicy"),
        "comparisonBaselineSensorCalibrationPolicy": baseline.get("summary", {}).get("baselineSensorCalibrationPolicy"),
        "comparisonBaselineStateSimulationStartDate": baseline.get("summary", {}).get("stateSimulationStartDate"),
        "comparisonBaselineStateLookbackDays": baseline.get("summary", {}).get("stateLookbackDays"),
        "decisionLevel": "valve_zone",
        "execution_time_seconds": round(perf_time.perf_counter() - start_time, 3),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "stateSimulationStartDate": fuzzy_summary.get(
            "stateSimulationStartDate",
            baseline.get("summary", {}).get("stateSimulationStartDate"),
        ),
        "stateLookbackDays": fuzzy_summary.get(
            "stateLookbackDays",
            baseline.get("summary", {}).get("stateLookbackDays"),
        ),
        "stateAnchorPolicy": fuzzy_summary.get(
            "stateAnchorPolicy",
            baseline.get("summary", {}).get("stateAnchorPolicy"),
        ),
        "source": comparison.baseline_summary.get("source", "database-weather-and-pot-inventory"),
    }
    summary.update(comparison.sensor_summary())
    return comparison.payload(entries, chart_entries, summary)


def train_anfis_model_for_experiment(
    start_date: date,
    end_date: date,
    seed: int | None = 2026,
    generations: int = 35,
    population: int = 24,
    snapshot: ExperimentSnapshot | None = None,
) -> AnfisTrainingResult:
    """Train and evaluate the ANFIS controller from database sensor/weather examples."""
    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    training_snapshot = _resolve_anfis_training_snapshot(start_date, end_date, selected_snapshot)
    anfis_dataset = _generate_database_anfis_dataset(
        training_snapshot.selected_weather_rows,
        training_snapshot.pots,
        0,
        seed,
        training_snapshot.sensor_context,
        training_snapshot.weather_by_day,
        training_snapshot.day_profiles,
    )
    if not anfis_dataset:
        raise ValueError("ANFIS training requires recorded sensor readings for the selected or historical interval")

    train_dataset = list(anfis_dataset)
    fit_dataset, calibration_dataset = _split_anfis_training_calibration(train_dataset, seed)
    model = _train_anfis_controller(
        fit_dataset,
        calibration_dataset,
        generations=generations,
        population=population,
        seed=seed,
    )
    evaluation = _evaluate_anfis_model(model, train_dataset)
    metadata = {
        "train_samples": len(train_dataset),
        "fit_samples": len(fit_dataset),
        "weighted_fit_samples": len(_expand_anfis_training_dataset(fit_dataset)),
        "calibration_samples": len(calibration_dataset),
        "test_samples": len(train_dataset),
        "evaluation_samples": len(train_dataset),
        "training_sample_policy": "all_available_sensor_readings",
        "evaluation_sample_policy": "all_available_sensor_readings",
        "training_dataset_version": ANFIS_TRAINING_DATASET_VERSION,
        "seed": seed,
        "generations": generations,
        "population": population,
        "training_start_date": training_snapshot.start_date.isoformat(),
        "training_end_date": training_snapshot.end_date.isoformat(),
        "training_lookback_days": (training_snapshot.end_date - training_snapshot.start_date).days + 1,
        "training_history_days": (training_snapshot.end_date - training_snapshot.start_date).days + 1,
        "training_signals": _anfis_training_signal_summary(train_dataset),
        "anfis_input_features": list(model.global_model.input_names),
        "evaluation": evaluation,
    }
    return AnfisTrainingResult(model=model, evaluation=evaluation, metadata=metadata)


def run_daily_anfis_experiment(
    start_date: date,
    end_date: date,
    seed: int | None = 2026,
    generations: int = 35,
    population: int = 24,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    baseline_result: dict[str, Any] | None = None,
    trained_model: _AnfisModelController | None = None,
    training_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply an ANFIS controller to the same database weather/pot simulation."""
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    sensor_context = snapshot.sensor_context

    start_time = perf_time.perf_counter()
    if trained_model is None:
        training_result = train_anfis_model_for_experiment(
            start_date=start_date,
            end_date=end_date,
            seed=seed,
            generations=generations,
            population=population,
            snapshot=snapshot,
        )
        model = training_result.model
        training = training_result.metadata
        evaluation = training_result.evaluation
        model_source = "inline_training"
    else:
        model = trained_model
        training = dict(training_metadata or {})
        evaluation = dict(training.get("evaluation") or {})
        model_source = str(training.get("model_source") or "persisted_model")

    decision_threshold = ANFIS_WATER_SAVING_POLICY.threshold(ANFIS_DECISION_THRESHOLD)
    forecast_decision_threshold = ANFIS_WATER_SAVING_POLICY.threshold(
        ANFIS_FORECAST_DECISION_THRESHOLD,
        forecast=True,
    )
    threshold_calibration = {
        "threshold": decision_threshold,
        "raw_threshold": ANFIS_DECISION_THRESHOLD,
        "forecast_threshold": forecast_decision_threshold,
        "raw_forecast_threshold": ANFIS_FORECAST_DECISION_THRESHOLD,
        "calibrated": False,
        "reason": "fixed_threshold_with_water_saving_margin",
        "water_saving_margin": ANFIS_WATER_SAVING_POLICY.decision_margin,
        "forecast_water_saving_margin": ANFIS_WATER_SAVING_POLICY.forecast_decision_margin,
    }

    baseline = _strategy_comparison_baseline(start_date, end_date, snapshot, baseline_result)
    anfis = _run_anfis_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        model=model,
        persist=persist,
        snapshot=snapshot,
        decision_threshold=decision_threshold,
        forecast_decision_threshold=forecast_decision_threshold,
    )

    comparison = _ExperimentComparison(start_date, end_date, snapshot, baseline, anfis, "anfis", "anfis_water_usage_l")

    def anfis_row(baseline_entry: dict[str, Any], anfis_entry: dict[str, Any]) -> dict[str, Any]:
        predicted_probability = anfis_entry.get("predicted_probability")
        predicted_probability_percent = anfis_entry.get("predicted_probability_percent")
        if predicted_probability is not None and predicted_probability_percent is None:
            predicted_probability_percent = round(predicted_probability * 100.0, 2)
        trigger_probability = anfis_entry.get("trigger_probability", predicted_probability)
        trigger_probability_percent = anfis_entry.get("trigger_probability_percent")
        if trigger_probability is not None and trigger_probability_percent is None:
            trigger_probability_percent = round(trigger_probability * 100.0, 2)
        anfis_decision_threshold = anfis_entry.get("anfis_decision_threshold", decision_threshold)
        anfis_decision_threshold_percent = anfis_entry.get("anfis_decision_threshold_percent")
        if anfis_decision_threshold is not None and anfis_decision_threshold_percent is None:
            anfis_decision_threshold_percent = round(float(anfis_decision_threshold) * 100.0, 2)

        return comparison.row(
            baseline_entry,
            anfis_entry,
            {
                "predicted_probability": predicted_probability,
                "predicted_probability_percent": predicted_probability_percent,
                "trigger_probability": trigger_probability,
                "trigger_probability_percent": trigger_probability_percent,
                "anfis_decision_threshold": anfis_decision_threshold,
                "anfis_decision_threshold_percent": anfis_decision_threshold_percent,
                "predicted_category": probability_category(predicted_probability) if predicted_probability is not None else "not_applicable",
                "trigger_predicted_category": probability_category(trigger_probability) if trigger_probability is not None else "not_applicable",
            },
            include_moisture_alias=True,
        )

    entries = comparison.daily_entries(anfis_row)
    chart_entries = comparison.chart_entries(anfis_row)
    predicted_probabilities = [
        float(entry["predicted_probability"])
        for entry in entries
        if entry.get("predicted_probability") is not None
    ]
    execution_time_seconds = round(perf_time.perf_counter() - start_time, 3)
    pred_prob_mean = round(sum(predicted_probabilities) / max(len(predicted_probabilities), 1), 4) if predicted_probabilities else 0.0
    pred_prob_min = round(min(predicted_probabilities), 4) if predicted_probabilities else 0.0
    pred_prob_max = round(max(predicted_probabilities), 4) if predicted_probabilities else 0.0

    anfis_summary = comparison.comparison_summary
    control_pots = _sensor_control_pots(snapshot.pots, snapshot.sensor_context)
    totals = comparison.total_usage_counts(entries)
    baseline_agreement_matches = sum(
        1
        for entry in entries
        if entry["baseline_irrigation_active"] == entry["anfis_irrigation_active"]
    )
    baseline_mismatch_days = len(entries) - baseline_agreement_matches
    baseline_only_irrigation_days = [
        entry
        for entry in entries
        if entry["baseline_irrigation_active"] and not entry["anfis_irrigation_active"]
    ]
    anfis_only_irrigation_days = [
        entry
        for entry in entries
        if entry["anfis_irrigation_active"] and not entry["baseline_irrigation_active"]
    ]
    missed_valve_run_delta = sum(
        max(0, int(entry["baseline_valve_runs"] or 0) - int(entry["anfis_valve_runs"] or 0))
        for entry in entries
    )
    anfis_extra_valve_run_delta = sum(
        max(0, int(entry["anfis_valve_runs"] or 0) - int(entry["baseline_valve_runs"] or 0))
        for entry in entries
    )
    baseline_water = float(totals["baseline_total_water_usage_l"])
    anfis_water = float(totals["anfis_total_water_usage_l"])
    water_savings_l = baseline_water - anfis_water
    water_savings_percent = round(water_savings_l / baseline_water * 100.0, 2) if baseline_water > 0 else 0.0
    comfort_metrics = _moisture_safe_savings_metrics(entries, "anfis", control_pots, water_savings_percent)
    summary = {
        **comparison.base_summary(entries),
        **comparison.irrigation_day_counts(entries),
        **totals,
        "baseline_agreement_percent": round(baseline_agreement_matches / max(len(entries), 1) * 100.0, 2),
        "baseline_mismatch_days": baseline_mismatch_days,
        "baseline_only_irrigation_days": len(baseline_only_irrigation_days),
        "anfis_only_irrigation_days": len(anfis_only_irrigation_days),
        "missed_valve_run_delta": missed_valve_run_delta,
        "anfis_extra_valve_run_delta": anfis_extra_valve_run_delta,
        "water_savings_l": round(water_savings_l, 2),
        "water_savings_percent": water_savings_percent,
        **comfort_metrics,
        "anfis_probability_threshold": decision_threshold,
        "anfis_forecast_probability_threshold": forecast_decision_threshold,
        "anfis_default_probability_threshold": ANFIS_DECISION_THRESHOLD,
        "anfis_threshold_calibration": threshold_calibration,
        "predicted_probability_mean": pred_prob_mean,
        "predicted_probability_min": pred_prob_min,
        "predicted_probability_max": pred_prob_max,
        "train_samples": int(training.get("train_samples", 0)),
        "fit_samples": int(training.get("fit_samples", 0)),
        "weighted_fit_samples": int(training.get("weighted_fit_samples", 0)),
        "calibration_samples": int(training.get("calibration_samples", 0)),
        "test_samples": int(training.get("test_samples", 0)),
        "evaluation_samples": int(training.get("evaluation_samples", training.get("test_samples", 0))),
        "trainingSamplePolicy": training.get("training_sample_policy", "all_available_sensor_readings"),
        "evaluationSamplePolicy": training.get("evaluation_sample_policy", "all_available_sensor_readings"),
        "anfisModelSource": model_source,
        "anfisModelId": training.get("model_id"),
        "anfisModelTrainedAt": training.get("trained_at"),
        "anfisTrainingSignals": training.get("training_signals", {}),
        "anfisTrainingTarget": (
            "recorded-sensor-readings-moisture-temperature-effective-rain-probability"
        ),
        "anfisProbabilityCalibration": model.summary(),
        "anfisWaterSavingPolicy": ANFIS_WATER_SAVING_POLICY.summary(
            ANFIS_DECISION_THRESHOLD,
            ANFIS_FORECAST_DECISION_THRESHOLD,
        ),
        "anfisSensorCalibrationPolicy": anfis_summary.get("anfisSensorCalibrationPolicy", "initial_state_only"),
        "controllerInputPolicy": anfis_summary.get("controllerInputPolicy"),
        "sensorDecisionPotCount": anfis_summary.get("sensorDecisionPotCount"),
        "sensorThresholdPolicy": anfis_summary.get("sensorThresholdPolicy"),
        "comparisonBaselineStateAnchorPolicy": baseline.get("summary", {}).get("stateAnchorPolicy"),
        "comparisonBaselineSensorCalibrationPolicy": baseline.get("summary", {}).get("baselineSensorCalibrationPolicy"),
        "comparisonBaselineStateSimulationStartDate": baseline.get("summary", {}).get("stateSimulationStartDate"),
        "comparisonBaselineStateLookbackDays": baseline.get("summary", {}).get("stateLookbackDays"),
        "anfisRainInputPolicy": "pot_effective_rain_by_rain_exposure",
        "anfisInputFeatures": model.global_model.input_names,
        "anfisOptimizer": "genetic-algorithm",
        "trainingStartDate": training.get("training_start_date"),
        "trainingEndDate": training.get("training_end_date"),
        "trainingLookbackDays": training.get("training_lookback_days"),
        "trainingHistoryDays": training.get("training_history_days", training.get("training_lookback_days")),
        "stateSimulationStartDate": anfis_summary.get(
            "stateSimulationStartDate",
            baseline.get("summary", {}).get("stateSimulationStartDate"),
        ),
        "stateLookbackDays": anfis_summary.get(
            "stateLookbackDays",
            baseline.get("summary", {}).get("stateLookbackDays"),
        ),
        "stateAnchorPolicy": anfis_summary.get(
            "stateAnchorPolicy",
            baseline.get("summary", {}).get("stateAnchorPolicy"),
        ),
        "decisionLevel": "valve_zone",
        "execution_time_seconds": execution_time_seconds,
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "source": "database-weather-and-pot-inventory",
        **evaluation,
    }
    summary.update(_sensor_summary_fields(sensor_context))
    summary["source"] = _experiment_source(sensor_context)
    return comparison.payload(entries, chart_entries, summary)



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


def _execute_valve_zone_distribution(
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
    minimum_runtime_min = max(0.0, _number(event_metadata.get("minimum_valve_runtime_min"), 0.0))
    public_event_metadata = {
        key: value
        for key, value in event_metadata.items()
        if key != "minimum_valve_runtime_min"
    }
    request_items: list[tuple[dict[str, Any], PotState, dict[str, Any]]] = []
    for zone_pot in _valve_managed_zone_pots(zone_pots, zone, current_date):
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
        events.append(_apply_event_delivery(state, zone_pot, event, delivered_ml, runtime_min))
    return events


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


def _is_valve_managed_pot(pot: dict[str, Any], day: date) -> bool:
    return _is_outdoor(pot, day)


def _valve_managed_zone_pots(zone_pots: dict[str, list[dict[str, Any]]], zone: str, day: date) -> list[dict[str, Any]]:
    return [pot for pot in zone_pots.get(zone, []) if _is_valve_managed_pot(pot, day)]


def _apply_cold_month_indoor_skip(decision: dict[str, Any], pot: dict[str, Any], day: date) -> dict[str, Any]:
    if decision.get("should_irrigate") and not _is_valve_managed_pot(pot, day):
        skipped = dict(decision)
        skipped["should_irrigate"] = False
        skipped["reason_code"] = "winter_indoor_not_valve_managed"
        skipped["reason_detail"] = (
            "Skipped because the pot is indoors from November through March; "
            "indoor irrigation is not implemented yet."
        )
        return skipped
    return decision


def _baseline_zone_dose_factor(trigger_decisions: list[dict[str, Any]]) -> float:
    return max((float(decision.get("dose_factor") or 1.0) for decision in trigger_decisions), default=1.0)


def _trigger_pot_ids(trigger_decisions: list[dict[str, Any]]) -> list[int]:
    return [int(decision["pot_id"]) for decision in trigger_decisions]


def _trigger_sensor_ids(trigger_decisions: list[dict[str, Any]]) -> list[int]:
    return sorted({
        int(decision.get("sensor_id", decision["pot_id"]))
        for decision in trigger_decisions
    })


def _trigger_pot_codes(trigger_decisions: list[dict[str, Any]]) -> list[str]:
    return [decision["pot_code"] for decision in trigger_decisions if decision.get("pot_code")]


def _sparse_zone_dose_factor(
    trigger_decisions: list[dict[str, Any]],
    sample_interval_hours: int,
    sample_now: bool,
) -> float:
    baseline_factor = _baseline_zone_dose_factor(trigger_decisions)
    if sample_now or sample_interval_hours <= 24:
        return baseline_factor
    if sample_interval_hours <= 48:
        return min(baseline_factor, 0.5)
    return min(baseline_factor, 0.75)


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
    managed = _valve_managed_zone_pots(zone_pots, zone, date.fromisoformat(decision_date))
    trigger_pot_ids = [int(decision["pot_id"]) for decision in should]
    trigger_sensor_ids = _trigger_sensor_ids(should)
    trigger_pot_codes = [decision.get("pot_code") for decision in should if decision.get("pot_code")]
    affected_pot_ids = [int(pot["id"]) for pot in managed] if should else []
    affected_pot_codes = [pot["pot_code"] for pot in managed] if should else []
    reason_detail = (
        f"Valve V{valve_number} controls {zone}; {len(trigger_pot_ids)} of {len(group)} evaluated pots require irrigation, "
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
        "trigger_pots": len(trigger_pot_ids),
        "trigger_pot_ids": trigger_pot_ids,
        "trigger_sensor_ids": trigger_sensor_ids,
        "trigger_pot_codes": trigger_pot_codes,
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


def _valve_event_from_group(
    key: tuple[str, str, str, str],
    group: list[dict[str, Any]],
    pot_by_id: dict[int, dict[str, Any]],
    zone_pots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    event_date, slot, scheduled_key, zone = key
    valve_number = _valve_number_for_zone(zone)
    managed = _valve_managed_zone_pots(zone_pots, zone, date.fromisoformat(event_date))
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
        entry["activated_valve_numbers"] = _activated_valve_numbers(entry_events)
        entry["activated_valves"] = _activated_valve_label(entry_events)
        entry["valves"] = _entry_valves(entry_events)
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


def _comparison_window_fields(prefix: str, entry: dict[str, Any]) -> dict[str, Any]:
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


def _entry_valves(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _activated_valve_label(events: list[dict[str, Any]]) -> str:
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


def _average_moisture_from_state_payload(payload: Any) -> float | None:
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


def _comfort_threshold_pct(pots: list[dict[str, Any]]) -> float:
    targets = [
        float(pot["moisture_target_pct"])
        for pot in pots
        if pot.get("moisture_target_pct") is not None
    ]
    return round(sum(targets) / max(len(targets), 1), 2) if targets else 0.0


def _fuzzy_comfort_threshold_pct(pots: list[dict[str, Any]]) -> float:
    day_profile = {
        "avg_temperature_c": 22.0,
        "max_temperature_c": 22.0,
        "precipitation_mm": 0.0,
        "heatwave_day": False,
        "dry_windy_day": False,
    }
    floors = [
        DEFAULT_FUZZY_POLICY.comfort_floor(pot, day_profile, "morning")
        for pot in pots
        if pot.get("moisture_target_pct") is not None and pot.get("moisture_min_pct") is not None
    ]
    return round(sum(floors) / max(len(floors), 1), 2) if floors else 0.0


def _moisture_safe_savings_metrics(
    entries: list[dict[str, Any]],
    prefix: str,
    pots: list[dict[str, Any]],
    water_savings_percent: float,
    comfort_threshold_pct: float | None = None,
) -> dict[str, Any]:
    threshold = _comfort_threshold_pct(pots) if comfort_threshold_pct is None else float(comfort_threshold_pct)
    tolerance_pct = 2.0
    safe_days = 0
    deficits = []
    for entry in entries:
        baseline_moisture = float(entry.get("baseline_moisture") or 0.0)
        comparison_moisture = float(entry.get(f"{prefix}_moisture") or 0.0)
        if comparison_moisture >= threshold or comparison_moisture >= baseline_moisture - tolerance_pct:
            safe_days += 1
        deficits.append(max(0.0, threshold - comparison_moisture))

    total_days = max(len(entries), 1)
    comfort_preserved = safe_days / total_days * 100.0
    moisture_safe_savings = float(water_savings_percent) * comfort_preserved / 100.0
    return {
        "comfort_threshold_pct": threshold,
        "comfort_preserved_days": safe_days,
        "comfort_preserved_percent": round(comfort_preserved, 2),
        "comfort_tolerance_pct": tolerance_pct,
        "average_comfort_deficit_pct": round(sum(deficits) / total_days, 2),
        "moisture_safe_savings_percent": round(moisture_safe_savings, 2),
    }


class _ExperimentComparison:
    """Builds baseline-vs-controller experiment payloads."""

    SENSOR_SUMMARY_KEYS = (
        "sensorDataUsed",
        "sensorSource",
        "sensorRows",
        "sensorLocationCount",
        "sensorAssociatedPotCount",
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
    )

    def __init__(
        self,
        start_date: date,
        end_date: date,
        snapshot: ExperimentSnapshot,
        baseline: dict[str, Any],
        comparison: dict[str, Any],
        prefix: str,
        water_usage_field: str,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.snapshot = snapshot
        self.baseline = baseline
        self.comparison = comparison
        self.prefix = prefix
        self.water_usage_field = water_usage_field
        self.baseline_summary = baseline["summary"]
        self.comparison_summary = comparison["summary"]
        self._baseline_by_date = {entry["date"]: entry for entry in baseline["entries"]}
        self.shared_initial_moisture = _average_moisture_from_state_payload(
            baseline.get("stateAtExperimentStart")
        )

    def daily_entries(self, row_builder) -> list[dict[str, Any]]:
        return self._align_first_row_to_shared_initial_moisture(
            self._aligned_rows(
                self.comparison["entries"],
                self._baseline_by_date,
                row_builder,
            )
        )

    def chart_entries(self, row_builder) -> list[dict[str, Any]]:
        baseline_by_timestamp = {
            entry["timestamp"]: entry
            for entry in self.baseline.get("chartEntries", self.baseline["entries"])
        }
        return self._align_first_row_to_shared_initial_moisture(
            self._aligned_rows(
                self.comparison.get("chartEntries", self.comparison["entries"]),
                baseline_by_timestamp,
                row_builder,
                fallback_lookup=self._baseline_by_date,
                lookup_key="timestamp",
            )
        )

    def row(
        self,
        baseline_entry: dict[str, Any],
        comparison_entry: dict[str, Any],
        extra: dict[str, Any] | None = None,
        include_moisture_alias: bool = False,
    ) -> dict[str, Any]:
        baseline_active = self.is_active(baseline_entry)
        comparison_active = self.is_active(comparison_entry)
        row = {
            "date": comparison_entry["date"],
            "timestamp": comparison_entry["timestamp"],
            "day_label": comparison_entry["day_label"],
            "chart_label": comparison_entry.get("chart_label", comparison_entry["day_label"]),
            "baseline_moisture": baseline_entry["average_moisture"],
            f"{self.prefix}_moisture": comparison_entry["average_moisture"],
            "temperature": comparison_entry["temperature"],
            "max_temperature": comparison_entry["max_temperature"],
            "min_temperature": comparison_entry.get("min_temperature", comparison_entry["temperature"]),
            "humidity": comparison_entry["humidity"],
            "cloud_cover_pct": comparison_entry["cloud_cover_pct"],
            "rain_prediction": comparison_entry["rain_prediction"],
            "rain_amount": comparison_entry["rain_amount"],
            "baseline_irrigation_active": baseline_active,
            f"{self.prefix}_irrigation_active": comparison_active,
            "baseline_irrigation_events": baseline_entry["irrigation_events"],
            f"{self.prefix}_irrigation_events": comparison_entry["irrigation_events"],
            "baseline_valve_runs": baseline_entry.get("valve_runs", baseline_entry["irrigation_events"]),
            f"{self.prefix}_valve_runs": comparison_entry.get("valve_runs", comparison_entry["irrigation_events"]),
            "baseline_water_usage_l": baseline_entry["water_usage_l"],
            f"{self.prefix}_water_usage_l": comparison_entry["water_usage_l"],
            "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
            f"{self.prefix}_water_usage_ml": comparison_entry["water_usage_ml"],
            **_comparison_window_fields("baseline", baseline_entry),
            **_comparison_window_fields(self.prefix, comparison_entry),
            "alerts": comparison_entry["alerts"],
            **_combined_line_metadata(baseline_entry, comparison_entry),
        }
        if include_moisture_alias:
            row["moisture"] = baseline_entry["average_moisture"]
        if extra:
            row.update(extra)
        return row

    def base_summary(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        total_entries = len(entries)
        return {
            "totalEntries": total_entries,
            "daysAnalyzed": total_entries,
            "potsAnalyzed": self.comparison_summary["potsAnalyzed"],
        }

    def irrigation_day_counts(self, entries: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "baseline_irrigation_days": self.active_days(entries, "baseline"),
            f"{self.prefix}_irrigation_days": self.active_days(entries, self.prefix),
        }

    def total_usage_counts(self, entries: list[dict[str, Any]]) -> dict[str, int | float]:
        return {
            "baseline_total_water_usage_l": self.water_total(entries, "baseline"),
            f"{self.prefix}_total_water_usage_l": self.water_total(entries, self.prefix),
            "baseline_irrigation_event_count": self.event_total(entries, "baseline"),
            f"{self.prefix}_irrigation_event_count": self.event_total(entries, self.prefix),
            "baseline_valve_run_count": self.valve_run_total(entries, "baseline"),
            f"{self.prefix}_valve_run_count": self.valve_run_total(entries, self.prefix),
        }

    def decision_counts(self) -> dict[str, int]:
        return {
            "baseline_irrigation_decisions": self.baseline_summary["irrigationDecisions"],
            f"{self.prefix}_irrigation_decisions": self.comparison_summary["irrigationDecisions"],
        }

    def sensor_summary(self, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
        allowed = keys or self.SENSOR_SUMMARY_KEYS
        return {key: self.baseline_summary[key] for key in allowed if key in self.baseline_summary}

    def shared_initial_summary(self) -> dict[str, Any]:
        if self.shared_initial_moisture is None:
            return {
                "firstPointAlignmentPolicy": "unavailable",
            }
        return {
            "firstPointAlignmentPolicy": "shared_experiment_start_state",
            "sharedInitialMoisture": self.shared_initial_moisture,
        }

    def payload(
        self,
        entries: list[dict[str, Any]],
        chart_entries: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        summary.update(self.shared_initial_summary())
        _add_chart_summary(summary, chart_entries, self.start_date, self.end_date)
        return {
            "entries": entries,
            "chartEntries": chart_entries,
            "summary": summary,
            "pots": _comparison_pot_info_entries(
                self.snapshot.pots,
                self.baseline,
                self.comparison,
                self.water_usage_field,
            ),
            "sampleDecisions": self.comparison.get("sampleDecisions", [])[:200],
            "sampleEvents": self.comparison.get("sampleEvents", [])[:200],
            "sampleAlerts": self.comparison.get("sampleAlerts", [])[:200],
        }

    @staticmethod
    def is_active(entry: dict[str, Any]) -> bool:
        return int(entry.get("irrigation_events") or 0) > 0

    @staticmethod
    def active_days(entries: list[dict[str, Any]], prefix: str) -> int:
        return sum(1 for entry in entries if entry.get(f"{prefix}_irrigation_active"))

    @staticmethod
    def water_total(entries: list[dict[str, Any]], prefix: str) -> float:
        return round(sum(float(entry.get(f"{prefix}_water_usage_l") or 0.0) for entry in entries), 2)

    @staticmethod
    def event_total(entries: list[dict[str, Any]], prefix: str) -> int:
        return sum(int(entry.get(f"{prefix}_irrigation_events") or 0) for entry in entries)

    @staticmethod
    def valve_run_total(entries: list[dict[str, Any]], prefix: str) -> int:
        return sum(int(entry.get(f"{prefix}_valve_runs") or 0) for entry in entries)

    def _align_first_row_to_shared_initial_moisture(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows or self.shared_initial_moisture is None:
            return rows
        first = rows[0]
        if first.get("date") != self.start_date.isoformat():
            return rows
        aligned = dict(first)
        aligned["baseline_moisture"] = self.shared_initial_moisture
        aligned[f"{self.prefix}_moisture"] = self.shared_initial_moisture
        aligned["shared_initial_moisture"] = self.shared_initial_moisture
        aligned["first_point_alignment_policy"] = "shared_experiment_start_state"
        if "moisture" in aligned:
            aligned["moisture"] = self.shared_initial_moisture
        return [aligned, *rows[1:]]

    @staticmethod
    def _aligned_rows(
        comparison_entries: list[dict[str, Any]],
        baseline_lookup: dict[Any, dict[str, Any]],
        row_builder,
        fallback_lookup: dict[Any, dict[str, Any]] | None = None,
        lookup_key: str = "date",
    ) -> list[dict[str, Any]]:
        rows = []
        for comparison_entry in comparison_entries:
            baseline_entry = baseline_lookup.get(comparison_entry[lookup_key])
            if baseline_entry is None and fallback_lookup is not None:
                baseline_entry = fallback_lookup.get(comparison_entry["date"])
            if baseline_entry is not None:
                rows.append(row_builder(baseline_entry, comparison_entry))
        return rows


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
        sensor_pot = min(zone_sensors, key=lambda item: _sensor_association_distance(pot, item))
        associations[pot_id] = {
            "sensor_id": int(sensor_pot["id"]),
            "direct": False,
            "distance": round(_sensor_association_distance(pot, sensor_pot), 4),
        }

    enriched = dict(sensor_context)
    enriched["associations"] = associations
    enriched["sensor_pots"] = {int(pot["id"]): pot for pot in sensor_pots}
    enriched["associated_pot_count"] = len([item for item in associations.values() if not item["direct"]])
    sensor_thresholds = _load_sensor_threshold_overrides(sensor_ids)
    if sensor_context.get("sensor_thresholds"):
        sensor_thresholds.update(sensor_context["sensor_thresholds"])
    if sensor_thresholds:
        enriched["sensor_thresholds"] = sensor_thresholds
    return enriched


def _sensor_control_ids(sensor_context: dict[str, Any] | None, pots: list[dict[str, Any]]) -> set[int]:
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


def _sensor_control_pots(pots: list[dict[str, Any]], sensor_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    control_ids = _sensor_control_ids(sensor_context, pots)
    return [
        _sensor_control_pot(pot, sensor_context)
        for pot in pots
        if int(pot["id"]) in control_ids
    ]


def _sensor_control_pot(pot: dict[str, Any], sensor_context: dict[str, Any] | None) -> dict[str, Any]:
    overrides = _sensor_threshold_overrides(sensor_context, int(pot["id"]))
    if not overrides:
        return pot

    enriched = dict(pot)
    enriched.update(overrides)
    enriched["sensor_threshold_override"] = dict(overrides)
    return enriched


def _sensor_threshold_overrides(sensor_context: dict[str, Any] | None, sensor_id: int) -> dict[str, float]:
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

    return _normalize_sensor_threshold_config(raw_config)


def _normalize_sensor_threshold_config(raw_config: dict[str, Any]) -> dict[str, float]:
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
        value = _number(raw_config.get(source_key), None)
        if value is None:
            continue
        overrides[target_key] = round(_clamp(value, 0.0, 100.0), 2)
    return overrides


def _load_sensor_threshold_overrides(sensor_ids: set[int]) -> dict[int, dict[str, float]]:
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
        overrides = _normalize_sensor_threshold_config(criteria)
        if overrides:
            thresholds[int(row["pot_id"])] = overrides
    return thresholds


def _sensor_control_summary_fields(
    pots: list[dict[str, Any]],
    sensor_context: dict[str, Any] | None,
) -> dict[str, Any]:
    control_ids = _sensor_control_ids(sensor_context, pots)
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


def _zone_execution_decision_map(
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
    for zone_pot in _valve_managed_zone_pots(zone_pots, zone, current_date):
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


def _with_sensor_key(record: dict[str, Any], pot: dict[str, Any], sensor_context: dict[str, Any]) -> dict[str, Any]:
    sensor_id = _sensor_id_for_pot(sensor_context, pot)
    enriched = dict(record)
    enriched["sensor_id"] = sensor_id
    threshold_overrides = _sensor_threshold_overrides(sensor_context, sensor_id)
    if threshold_overrides:
        enriched["sensor_threshold_override"] = dict(threshold_overrides)
    if sensor_id != int(pot["id"]):
        enriched["associated_pot_id"] = int(pot["id"])
    return enriched


def _with_event_sensor_key(event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(event)
    sensor_id = int(decision.get("sensor_id", event["pot_id"]))
    enriched["sensor_id"] = sensor_id
    enriched["request_sensor_id"] = sensor_id
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


def _apply_sensor_reading(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
) -> dict[str, Any] | None:
    reading = _sensor_reading_for_pot(sensor_context, pot, experiment_date, observed_at)
    if reading is None:
        return None

    reading = dict(reading)
    if reading.get("source") != ACTUAL_SENSOR_SOURCE:
        return reading

    sensor_moisture = _number(reading["soil_moisture_pct"], state.moisture)
    if reading.get("association_source") == "associated_sensor":
        sensor_weight = _associated_sensor_weight(observed_at)
        state.moisture = _clamp(sensor_moisture * sensor_weight + state.moisture * (1.0 - sensor_weight), 0.0, 100.0)
        reading["soil_moisture_pct"] = round(state.moisture, 2)
        reading["sensor_blend_weight"] = round(sensor_weight, 2)
    else:
        state.moisture = _clamp(sensor_moisture, 0.0, 100.0)
    return reading


def _apply_sensor_calibration_marker(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
    day_profile: dict[str, Any],
) -> dict[str, Any] | None:
    marker_time = _sensor_calibration_marker_time(observed_at, day_profile)
    if marker_time is None:
        return None
    marker_at = datetime.combine(experiment_date, marker_time, tzinfo=LOCAL_TZ)
    return _apply_stored_sensor_calibration(state, pot, experiment_date, marker_at, sensor_context)


def _sensor_calibration_marker_time(observed_at: datetime, day_profile: dict[str, Any]) -> time | None:
    if observed_at.hour == 6:
        return MORNING_SENSOR_CALIBRATION_TIME
    if (
        observed_at.hour == 18
        and _number(day_profile.get("max_temperature_c"), 20.0) > 32.0
    ):
        return EVENING_SENSOR_CALIBRATION_TIME
    return None


def _sampling_calibration_at(experiment_date: date, observed_at: datetime, day_profile: dict[str, Any]) -> datetime:
    marker_time = _sensor_calibration_marker_time(observed_at, day_profile)
    if marker_time is None:
        return observed_at
    return datetime.combine(experiment_date, marker_time, tzinfo=LOCAL_TZ)


def _apply_stored_sensor_calibration(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
) -> dict[str, Any] | None:
    reading = _sensor_reading_for_pot(sensor_context, pot, experiment_date, observed_at)
    if reading is None:
        return None

    return _apply_calibration_reading(state, reading, observed_at)


def _apply_calibration_reading(
    state: PotState,
    reading: dict[str, Any],
    observed_at: datetime,
) -> dict[str, Any]:
    reading = dict(reading)
    sensor_moisture = _number(reading["soil_moisture_pct"], state.moisture)
    state.moisture = _clamp(sensor_moisture, 0.0, 100.0)
    if reading.get("association_source") == "associated_sensor":
        reading["soil_moisture_pct"] = round(state.moisture, 2)
        reading["sensor_blend_weight"] = 1.0
    reading["calibration_marker_time"] = observed_at.time().replace(second=0, microsecond=0).isoformat()
    return reading


def _associated_sensor_weight(observed_at: datetime) -> float:
    if _reading_slot_label(observed_at) == "evening":
        return 0.95
    return 0.82


def _reading_slot_label(observed_at: datetime) -> str | None:
    if observed_at.hour == 18:
        return "evening"
    if observed_at.hour == 6:
        return "morning"
    if observed_at.hour == 10:
        return "winter_check"
    return None


def _new_sampling_estimation_stats() -> dict[str, float | int]:
    return {
        "estimation_points": 0,
        "error_sum": 0.0,
        "absolute_error_sum": 0.0,
        "max_absolute_error": 0.0,
        "sensor_refreshes": 0,
        "direct_refreshes": 0,
        "associated_refreshes": 0,
        "forecast_refreshes": 0,
        "missing_refreshes": 0,
        "association_distance_sum": 0.0,
    }


def _record_sampling_estimation_error(
    stats: dict[str, float | int],
    controller_state: PotState,
    actual_state: PotState,
) -> None:
    error = controller_state.moisture - actual_state.moisture
    absolute_error = abs(error)
    stats["estimation_points"] += 1
    stats["error_sum"] += error
    stats["absolute_error_sum"] += absolute_error
    stats["max_absolute_error"] = max(float(stats["max_absolute_error"]), absolute_error)


def _sampling_estimation_summary(stats: dict[str, float | int]) -> dict[str, Any]:
    points = int(stats["estimation_points"])
    associated_refreshes = int(stats["associated_refreshes"])
    return {
        "sampling_moisture_mae_pct": round(float(stats["absolute_error_sum"]) / max(points, 1), 2),
        "sampling_moisture_bias_pct": round(float(stats["error_sum"]) / max(points, 1), 2),
        "sampling_moisture_max_error_pct": round(float(stats["max_absolute_error"]), 2),
        "sampling_estimation_points": points,
        "sampling_sensor_refreshes": int(stats["sensor_refreshes"]),
        "sampling_direct_refreshes": int(stats["direct_refreshes"]),
        "sampling_associated_refreshes": associated_refreshes,
        "sampling_forecast_refreshes": int(stats["forecast_refreshes"]),
        "sampling_missing_refreshes": int(stats["missing_refreshes"]),
        "sampling_average_association_distance": round(
            float(stats["association_distance_sum"]) / max(associated_refreshes, 1),
            2,
        ),
    }


def _sensor_reading_for_pot(
    sensor_context: dict[str, Any] | None,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime | time | int,
) -> dict[str, Any] | None:
    if not sensor_context or not sensor_context.get("available"):
        return None

    lookup = sensor_context.get("lookup") or {}
    pot_id = int(pot["id"])
    slot_time = _sensor_lookup_time(observed_at)
    direct = _lookup_sensor_reading(lookup, experiment_date, slot_time, pot_id)
    if direct is not None:
        return direct

    association = (sensor_context.get("associations") or {}).get(pot_id)
    if not association:
        return None
    sensor_id = int(association["sensor_id"])
    sensor_reading = _lookup_sensor_reading(lookup, experiment_date, slot_time, sensor_id)
    if sensor_reading is None:
        return None
    sensor_pot = (sensor_context.get("sensor_pots") or {}).get(sensor_id)
    if sensor_pot is None:
        return sensor_reading
    return _associated_sensor_reading(pot, sensor_pot, sensor_reading)


def _forecast_sensor_reading_for_pot(
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
    slot_time = _sensor_lookup_time(observed_at)
    reading = {
        "sensor_id": pot_id,
        "source": SPARSE_FORECAST_SENSOR_SOURCE,
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


def _sensor_lookup_time(value: datetime | time | int) -> time:
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    return time(int(value), 0)


def _lookup_sensor_reading(
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
    rain = {
        "covered": 0.0,
        "partially_exposed": 0.5,
        "fully_exposed": 1.0,
    }.get(str(pot.get("rain_exposure") or "partially_exposed"), 0.5)
    return rain + (_sun_factor(pot) - 1.0) * 1.6 + (_wind_factor(pot) - 1.0) * 1.2


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


def _initialize_states_from_first_day_sensor_readings(
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
            _sensor_lookup_time(slot_time)
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
            reading = _sensor_reading_for_pot(sensor_context, pot, start_date, slot_time)
            if reading is None:
                continue
            pot_states[pot_id].moisture = _clamp(
                _number(reading["soil_moisture_pct"], pot_states[pot_id].moisture),
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
    zone_pots = _pots_by_valve_zone(pots)
    control_pots = _sensor_control_pots(pots, sensor_context)
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
            _apply_hourly_environment(
                state,
                pot,
                hour_weather,
                day_profile,
                current.date(),
                rain_exposure_factor=_rain_exposure_factor(pot, current.date()),
            )
        slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current.date(), current, day_profile)
        if slot is None:
            current += timedelta(hours=1)
            continue

        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}
        for pot in control_pots:
            state = pot_states[pot["id"]]
            decision = _make_baseline_irrigation_decision(state, pot, hour_weather, day_profile, slot)
            decision = _with_sensor_key(decision, pot, sensor_context)
            decision = _apply_cold_month_indoor_skip(decision, pot, current_day)
            decision_by_pot_id[int(pot["id"])] = decision
            if decision["should_irrigate"] and _is_valve_managed_pot(pot, current_day):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        for zone, trigger_decisions in zone_trigger_decisions.items():
            trigger_pot_ids = _trigger_pot_ids(trigger_decisions)
            trigger_sensor_ids = _trigger_sensor_ids(trigger_decisions)
            trigger_pot_codes = _trigger_pot_codes(trigger_decisions)
            zone_dose_factor = _baseline_zone_dose_factor(trigger_decisions)
            execution_decisions = _zone_execution_decision_map(
                decision_by_pot_id,
                zone_pots,
                zone,
                current_day,
                trigger_decisions,
            )
            _execute_valve_zone_distribution(
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


def _weather_for_hour(day_weather: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any] | None:
    for row in day_weather:
        if _local_observed_at(row).hour == observed_at.hour:
            return row
    return day_weather[0] if day_weather else None


def _hourly_line_metadata(
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


def _sensor_line_metadata_for_hour(sensor_context: dict[str, Any], experiment_date: date, observed_at: datetime | time | int) -> dict[str, Any]:
    has_reading_for_day = _has_sensor_reading_for_day(sensor_context, experiment_date)
    hour = observed_at.hour if isinstance(observed_at, (datetime, time)) else int(observed_at)
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

    slot_time = _sensor_lookup_time(observed_at)
    for sensor_id in sensor_ids:
        reading = _lookup_sensor_reading(lookup, experiment_date, slot_time, int(sensor_id))
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
        for (reading_date, _slot_time, sensor_id), reading in lookup.items()
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


def _anfis_decision_threshold(
    sensor_context: dict[str, Any],
    experiment_date: date,
    decision_threshold: float = ANFIS_DECISION_THRESHOLD,
    forecast_decision_threshold: float = ANFIS_FORECAST_DECISION_THRESHOLD,
) -> float:
    if _sensor_date_is_future(sensor_context, experiment_date):
        return forecast_decision_threshold
    return decision_threshold


def _make_anfis_execution_decision(
    state: PotState,
    pot: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    slot: str,
) -> dict[str, Any]:
    observed_local = _local_observed_at(weather)
    target = pot["winter_moisture_target_pct"] if slot == "winter_check" else pot["moisture_target_pct"]
    reason_code = "anfis_probability_pending"
    reason_detail = "Valve-zone average ANFIS probability decides irrigation; ANFIS water-saving policy controls duration."
    should_irrigate = False

    if _number(weather.get("temperature_c"), day_profile["avg_temperature_c"]) <= 3.0:
        reason_code = "freeze_risk"
        reason_detail = "Skipped because temperature is too low for irrigation."

    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "decided_at": observed_local.isoformat(),
        "date": observed_local.date().isoformat(),
        "slot": slot,
        "should_irrigate": should_irrigate,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "current_moisture_pct": round(state.moisture, 2),
        "target_moisture_pct": round(target, 2),
        "weather_hourly_id": weather["id"],
        "dose_factor": 1.0,
        "dose_policy_source": "anfis_water_saving_policy",
    }


def _anfis_duration_policy_note(decision: dict[str, Any]) -> str:
    dose_factor = _number(decision.get("dose_factor"), 1.0)
    if dose_factor < 1.0:
        return f" ANFIS water-saving policy reduces runtime to {dose_factor * 100.0:.0f}%."
    return " ANFIS water-saving policy keeps full runtime."


@dataclass
class _AnfisProbabilityCalibrator:
    points: list[tuple[float, float]]

    @classmethod
    def fit(cls, model: ANFIS, dataset: list[dict[str, float | str]], max_bins: int = 8) -> "_AnfisProbabilityCalibrator":
        pairs = sorted(
            (
                _clamp(model.predict(item), 0.0, 1.0),
                _clamp(float(item["target_probability"]), 0.0, 1.0),
            )
            for item in dataset
            if item.get("target_probability") is not None
        )
        if len(pairs) < 30:
            return cls([])

        bin_count = min(max_bins, max(3, len(pairs) // 45))
        bin_size = max(1, math.ceil(len(pairs) / bin_count))
        bins = []
        for index in range(0, len(pairs), bin_size):
            chunk = pairs[index:index + bin_size]
            if not chunk:
                continue
            raw_mean = sum(raw for raw, _ in chunk) / len(chunk)
            target_mean = sum(target for _, target in chunk) / len(chunk)
            bins.append([raw_mean, target_mean, len(chunk)])

        # Pool adjacent bins so the calibration curve remains monotonic.
        pooled: list[list[float]] = []
        for raw_mean, target_mean, weight in bins:
            pooled.append([raw_mean, target_mean, float(weight)])
            while len(pooled) >= 2 and pooled[-2][1] > pooled[-1][1]:
                right = pooled.pop()
                left = pooled.pop()
                merged_weight = left[2] + right[2]
                pooled.append(
                    [
                        (left[0] * left[2] + right[0] * right[2]) / merged_weight,
                        (left[1] * left[2] + right[1] * right[2]) / merged_weight,
                        merged_weight,
                    ]
                )

        points = [(float(raw), _clamp(float(target), 0.0, 1.0)) for raw, target, _ in pooled]
        return cls(points)

    def predict(self, raw_probability: float) -> float:
        raw = _clamp(float(raw_probability), 0.0, 1.0)
        if not self.points:
            return raw
        if raw <= self.points[0][0]:
            return self.points[0][1]
        if raw >= self.points[-1][0]:
            return self.points[-1][1]
        for index in range(1, len(self.points)):
            left_raw, left_target = self.points[index - 1]
            right_raw, right_target = self.points[index]
            if raw <= right_raw:
                span = max(right_raw - left_raw, 1e-9)
                ratio = (raw - left_raw) / span
                return _clamp(left_target + (right_target - left_target) * ratio, 0.0, 1.0)
        return raw

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.points),
            "bin_count": len(self.points),
            "points": [
                {"raw": round(raw, 4), "calibrated": round(calibrated, 4)}
                for raw, calibrated in self.points
            ],
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "points": [
                {"raw": float(raw), "calibrated": float(calibrated)}
                for raw, calibrated in self.points
            ],
        }

    @classmethod
    def deserialize(cls, payload: dict[str, Any] | None) -> "_AnfisProbabilityCalibrator":
        points = []
        for item in (payload or {}).get("points") or []:
            points.append((float(item["raw"]), float(item["calibrated"])))
        return cls(points)


@dataclass
class _AnfisModelController:
    global_model: ANFIS
    global_calibrator: _AnfisProbabilityCalibrator
    zone_models: dict[str, ANFIS]
    zone_calibrators: dict[str, _AnfisProbabilityCalibrator]

    def predict(self, inputs: dict[str, float], zone: str | None = None) -> float:
        raw = self.raw_predict(inputs, zone)
        return self.calibrator_for_zone(zone).predict(raw)

    def raw_predict(self, inputs: dict[str, float], zone: str | None = None) -> float:
        return self.model_for_zone(zone).predict(inputs)

    def model_for_zone(self, zone: str | None) -> ANFIS:
        return self.zone_models.get(str(zone or ""), self.global_model)

    def calibrator_for_zone(self, zone: str | None) -> _AnfisProbabilityCalibrator:
        return self.zone_calibrators.get(str(zone or ""), self.global_calibrator)

    def summary(self) -> dict[str, Any]:
        return {
            "trained_per_valve_zone": bool(self.zone_models),
            "zone_model_count": len(self.zone_models),
            "zone_models": sorted(self.zone_models),
            "global_probability_calibration": self.global_calibrator.summary(),
            "zone_probability_calibration": {
                zone: calibrator.summary()
                for zone, calibrator in sorted(self.zone_calibrators.items())
            },
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "global_model": self.global_model.serialize(),
            "global_calibrator": self.global_calibrator.serialize(),
            "zone_models": {
                zone: model.serialize()
                for zone, model in sorted(self.zone_models.items())
            },
            "zone_calibrators": {
                zone: calibrator.serialize()
                for zone, calibrator in sorted(self.zone_calibrators.items())
            },
        }

    @classmethod
    def deserialize(cls, payload: dict[str, Any]) -> "_AnfisModelController":
        return cls(
            global_model=ANFIS.deserialize(payload["global_model"]),
            global_calibrator=_AnfisProbabilityCalibrator.deserialize(payload.get("global_calibrator")),
            zone_models={
                str(zone): ANFIS.deserialize(model_payload)
                for zone, model_payload in (payload.get("zone_models") or {}).items()
            },
            zone_calibrators={
                str(zone): _AnfisProbabilityCalibrator.deserialize(calibrator_payload)
                for zone, calibrator_payload in (payload.get("zone_calibrators") or {}).items()
            },
        )


@dataclass
class AnfisTrainingResult:
    model: _AnfisModelController
    evaluation: dict[str, Any]
    metadata: dict[str, Any]


def serialize_trained_anfis_model(model: _AnfisModelController) -> dict[str, Any]:
    return model.serialize()


def deserialize_trained_anfis_model(payload: dict[str, Any]) -> _AnfisModelController:
    return _AnfisModelController.deserialize(payload)


@dataclass(frozen=True)
class _AnfisWaterSavingPolicy:
    decision_margin: float = 0.04
    forecast_decision_margin: float = 0.04
    maximum_threshold: float = 0.92
    light_deficit_pct: float = 4.0
    moderate_deficit_pct: float = 9.0
    severe_deficit_pct: float = 12.0
    low_confidence_margin: float = 0.08
    medium_confidence_margin: float = 0.20
    safety_dose_floor: float = 0.75
    minimum_runtime_min: float = 2.0
    safety_minimum_runtime_min: float = 0.5
    cadence_days_after_irrigation: int = 2
    cadence_escape_probability: float = 0.90

    def threshold(self, raw_threshold: float, forecast: bool = False) -> float:
        margin = self.forecast_decision_margin if forecast else self.decision_margin
        return round(_clamp(float(raw_threshold) + margin, 0.0, self.maximum_threshold), 4)

    def zone_dose_factor(
        self,
        trigger_decisions: list[dict[str, Any]],
        slot_probability: float,
        decision_threshold: float,
        day_profile: dict[str, Any],
    ) -> float:
        max_deficit = self._max_deficit(trigger_decisions)
        need_factor = self._need_factor(max_deficit)
        confidence_factor = self._confidence_factor(slot_probability, decision_threshold)
        dose_factor = _anfis_allowed_dose_factor(min(need_factor, confidence_factor))

        if self._has_safety_override(trigger_decisions):
            dose_factor = max(dose_factor, self.safety_dose_floor)
        return _anfis_allowed_dose_factor(dose_factor)

    def cadence_days(self, day_profile: dict[str, Any]) -> int:
        return self.cadence_days_after_irrigation

    def cadence_blocks(
        self,
        last_irrigated_by_zone: dict[str, date],
        zone: str,
        current_date: date,
        trigger_decisions: list[dict[str, Any]],
        slot_probability: float,
        day_profile: dict[str, Any],
    ) -> bool:
        last_irrigated = last_irrigated_by_zone.get(zone)
        if last_irrigated is None:
            return False
        if self._has_safety_override(trigger_decisions):
            return False
        if slot_probability >= self.cadence_escape_probability:
            return False
        return (current_date - last_irrigated).days < self.cadence_days(day_profile)

    def summary(self, raw_threshold: float, raw_forecast_threshold: float) -> dict[str, Any]:
        return {
            "decision_threshold": self.threshold(raw_threshold),
            "forecast_decision_threshold": self.threshold(raw_forecast_threshold, forecast=True),
            "decision_margin": self.decision_margin,
            "forecast_decision_margin": self.forecast_decision_margin,
            "dose_steps": [0.5, 0.75, 1.0],
            "light_deficit_pct": self.light_deficit_pct,
            "moderate_deficit_pct": self.moderate_deficit_pct,
            "safety_dose_floor": self.safety_dose_floor,
            "minimum_valve_runtime_min": self.minimum_runtime_min,
            "safety_minimum_valve_runtime_min": self.safety_minimum_runtime_min,
            "cadence_days_after_irrigation": self.cadence_days_after_irrigation,
            "cadence_escape_probability": self.cadence_escape_probability,
            "dose_policy": "anfis_probability_confidence_and_moisture_deficit",
            "rain_temperature_policy": "learned_through_anfis_inputs",
            "zone_activation_policy": "sensor_pot_probability_with_safety_override",
            "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
        }

    def _max_deficit(self, decisions: list[dict[str, Any]]) -> float:
        return max(
            (
                max(
                    0.0,
                    float(decision.get("target_moisture_pct") or 0.0)
                    - float(decision.get("current_moisture_pct") or 0.0),
                )
                for decision in decisions
            ),
            default=0.0,
        )

    def _has_safety_override(self, decisions: list[dict[str, Any]]) -> bool:
        return any(bool(decision.get("anfis_below_safety_threshold")) for decision in decisions)

    def _need_factor(self, max_deficit: float) -> float:
        if max_deficit <= self.light_deficit_pct:
            return 0.5
        if max_deficit <= self.moderate_deficit_pct:
            return 0.75
        return 1.0

    def _confidence_factor(self, slot_probability: float, decision_threshold: float) -> float:
        confidence_margin = float(slot_probability) - float(decision_threshold)
        if confidence_margin < self.low_confidence_margin:
            return 0.5
        if confidence_margin < self.medium_confidence_margin:
            return 0.75
        return 1.0


ANFIS_WATER_SAVING_POLICY = _AnfisWaterSavingPolicy()


@dataclass(frozen=True)
class _FuzzyActuationPolicy:
    minimum_runtime_min: float = 2.0
    safety_minimum_runtime_min: float = 0.5
    heatwave_supplement_minimum_prescription_mm: float = 1.0

    def has_safety_need(self, trigger_decisions: list[dict[str, Any]]) -> bool:
        return any(
            float(decision.get("current_moisture_pct") or 0.0)
            <= float(decision.get("fuzzy_safety_floor_pct") or decision.get("target_moisture_pct") or 0.0)
            for decision in trigger_decisions
        )

    def has_prescription_need(self, trigger_decisions: list[dict[str, Any]]) -> bool:
        return any(
            bool(decision.get("should_irrigate"))
            or float(decision.get("prescription_mm") or 0.0)
            >= self.heatwave_supplement_minimum_prescription_mm
            for decision in trigger_decisions
        )

    def minimum_runtime(self, trigger_decisions: list[dict[str, Any]]) -> float:
        if self.has_safety_need(trigger_decisions):
            return self.safety_minimum_runtime_min
        return self.minimum_runtime_min

    def allows_heatwave_supplement(
        self,
        slot: str,
        day_profile: dict[str, Any],
        trigger_decisions: list[dict[str, Any]] | None = None,
    ) -> bool:
        hot_evening = (
            slot == "evening"
            and (
                bool(day_profile.get("heatwave_day"))
                or _number(day_profile.get("max_temperature_c"), 20.0) >= 35.0
            )
        )
        if not hot_evening:
            return False
        if trigger_decisions is None:
            return True
        return self.has_safety_need(trigger_decisions) or self.has_prescription_need(trigger_decisions)

    def summary(self) -> dict[str, Any]:
        return {
            "minimum_valve_runtime_min": self.minimum_runtime_min,
            "safety_minimum_valve_runtime_min": self.safety_minimum_runtime_min,
            "heatwave_supplement_minimum_prescription_mm": self.heatwave_supplement_minimum_prescription_mm,
            "daily_execution_policy": "one_daily_prescription_plus_heatwave_evening_supplement",
            "heatwave_supplement_policy": "safety_floor_or_fuzzy_prescription_signal",
            "cadence_policy": "not_used",
            "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
        }


FUZZY_ACTUATION_POLICY = _FuzzyActuationPolicy()


def _anfis_zone_probability_summary(decisions: list[dict[str, Any]]) -> tuple[float, float]:
    probabilities = [
        float(decision["predicted_probability"])
        for decision in decisions
        if decision.get("predicted_probability") is not None
    ]
    if not probabilities:
        return 0.0, 0.0
    return sum(probabilities) / len(probabilities), max(probabilities)


def _anfis_zone_dose_factor(
    trigger_decisions: list[dict[str, Any]],
    slot_probability: float,
    day_profile: dict[str, Any],
    decision_threshold: float = ANFIS_DECISION_THRESHOLD,
) -> float:
    return ANFIS_WATER_SAVING_POLICY.zone_dose_factor(
        trigger_decisions,
        slot_probability,
        decision_threshold,
        day_profile,
    )


def _anfis_allowed_dose_factor(value: float) -> float:
    number_value = _clamp(_number(value, 1.0), 0.5, 1.0)
    if number_value <= 0.625:
        return 0.5
    if number_value <= 0.875:
        return 0.75
    return 1.0


def _anfis_zone_cadence_days(day_profile: dict[str, Any]) -> int:
    return ANFIS_WATER_SAVING_POLICY.cadence_days(day_profile)


def _anfis_zone_cadence_blocks(
    last_irrigated_by_zone: dict[str, date],
    zone: str,
    current_date: date,
    trigger_decisions: list[dict[str, Any]],
    slot_probability: float,
    day_profile: dict[str, Any],
) -> bool:
    return ANFIS_WATER_SAVING_POLICY.cadence_blocks(
        last_irrigated_by_zone,
        zone,
        current_date,
        trigger_decisions,
        slot_probability,
        day_profile,
    )


def _sensor_hour_is_future(experiment_date: date, hour: int) -> bool:
    observed_at = datetime.combine(experiment_date, time(hour, 0), tzinfo=LOCAL_TZ)
    return observed_at > datetime.now(LOCAL_TZ)


def _sensor_slot_is_prediction(
    sensor_context: dict[str, Any],
    experiment_date: date,
    observed_at: datetime | time | int,
) -> bool:
    slot_time = _sensor_lookup_time(observed_at)
    return _sensor_date_is_future(sensor_context, experiment_date) or _sensor_hour_is_future(
        experiment_date,
        slot_time.hour,
    )


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


def _anfis_inputs(
    state: PotState,
    weather: dict[str, Any],
    sensor_reading: dict[str, Any] | None,
    pot: dict[str, Any],
    day_profile: dict[str, Any],
    prior_moisture_pct: float | None = None,
) -> dict[str, float]:
    observed_day = _local_observed_at(weather).date()
    rain = _number(day_profile.get("precipitation_mm"), _number(weather.get("precipitation_mm"), 0.0))
    effective_rain = rain * _rain_exposure_factor(pot, observed_day)
    if sensor_reading:
        moisture = _number(sensor_reading["soil_moisture_pct"], state.moisture)
        temperature = _number(sensor_reading["air_temperature_c"], _number(weather["temperature_c"], 20.0))
    else:
        moisture = state.moisture
        temperature = _number(weather["temperature_c"], 20.0)
    return {
        "moisture": float(moisture),
        "temperature": float(temperature),
        "rain": float(effective_rain),
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
    baseline_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _SparseSamplingController(
        start_date=start_date,
        end_date=end_date,
        sample_interval_hours=sample_interval_hours,
        persist=persist,
        snapshot=snapshot,
        baseline_result=baseline_result,
    ).run()


class _SparseSamplingController:
    """Sparse sampling controller evaluated at default strategy decision slots."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        sample_interval_hours: int,
        persist: bool,
        snapshot: ExperimentSnapshot | None,
        baseline_result: dict[str, Any] | None = None,
    ) -> None:
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")

        self.start_date = start_date
        self.end_date = end_date
        self.sample_interval_hours = max(1, int(sample_interval_hours))
        self.persist = persist
        self.selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
        self.baseline_result = baseline_result or {}
        baseline_start_states = self.baseline_result.get("stateAtExperimentStart")
        if baseline_start_states:
            self.simulation_start_date = start_date
            self.simulation_snapshot = self.selected_snapshot
            self.state_anchor_policy = "baseline_warmup_reuse"
            self.warmup_reuse_policy = "baseline_start_state_reuse"
        else:
            self.simulation_start_date, self.simulation_snapshot = _resolve_simulation_snapshot(
                start_date,
                end_date,
                self.selected_snapshot,
            )
            self.state_anchor_policy = "stable_daily_timeline"
            self.warmup_reuse_policy = "independent_sparse_warmup"
        self.weather_rows = self.selected_snapshot.selected_weather_rows
        self.pots = self.simulation_snapshot.pots
        self.zone_pots = _pots_by_valve_zone(self.pots)
        self.sensor_context = self.simulation_snapshot.sensor_context
        self.selected_sensor_context = self.selected_snapshot.sensor_context
        self.control_pots = _sensor_control_pots(self.pots, self.sensor_context)
        self.control_pot_ids = {int(pot["id"]) for pot in self.control_pots}
        self.weather_by_day = self.simulation_snapshot.weather_by_day
        if baseline_start_states:
            self.states = _copy_pot_states_from_payload(
                baseline_start_states,
                self.simulation_snapshot.initial_pot_states,
            )
            self.probe_states = _copy_pot_states(self.states)
            baseline_summary = self.baseline_result.get("summary", {})
            self.sensor_state_anchor = {
                "source": "baseline_warmup_reuse",
                "anchor_date": start_date.isoformat(),
                "anchored_pots": len(self.states),
                "baseline_state_simulation_start_date": baseline_summary.get("stateSimulationStartDate"),
                "baseline_state_lookback_days": baseline_summary.get("stateLookbackDays"),
                "baseline_sensor_calibration_policy": baseline_summary.get("baselineSensorCalibrationPolicy"),
            }
        else:
            self.states = _copy_pot_states(self.simulation_snapshot.initial_pot_states)
            self.probe_states = _copy_pot_states(self.simulation_snapshot.initial_pot_states)
            self.sensor_state_anchor = _initialize_states_from_first_day_sensor_readings(
                self.states,
                self.pots,
                self.sensor_context,
                self.simulation_start_date,
            )
            _initialize_states_from_first_day_sensor_readings(
                self.probe_states,
                self.pots,
                self.sensor_context,
                self.simulation_start_date,
            )
        self.entries: list[dict[str, Any]] = []
        self.detail_entries: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.total_water_ml = 0.0
        self.total_irrigation_decisions = 0
        self.last_sensor_sample_at: datetime | None = None
        self.sensor_sample_count = 0
        self.sensor_sample_row_count = 0
        self.sampled_weather_rows = 0
        self.sampling_stats = _new_sampling_estimation_stats()

    def run(self) -> dict[str, Any]:
        current_date = self.simulation_start_date
        while current_date <= self.end_date:
            self._run_day(current_date)
            current_date += timedelta(days=1)

        valve_rollup = _apply_valve_rollup_to_entries(
            self.entries,
            self.detail_entries,
            self.pots,
            self.decisions,
            self.events,
        )
        summary = self._summary(valve_rollup)
        chart_entries = _chart_entries_for_range(self.start_date, self.end_date, self.entries, self.detail_entries)
        _add_chart_summary(summary, chart_entries, self.start_date, self.end_date)
        return {
            "entries": self.entries,
            "chartEntries": chart_entries,
            "summary": summary,
            "pots": _pot_info_entries(
                self.pots,
                {"period_water_usage_l": _event_water_usage_l_by_pot(self.events)},
            ),
            "sampleDecisions": valve_rollup["decisions"][:200],
            "sampleEvents": valve_rollup["events"][:200],
            "samplePotDecisions": self.decisions[:200],
            "samplePotEvents": self.events[:200],
            "sampleAlerts": self.alerts[:200],
        }

    def _run_day(self, current_date: date) -> None:
        day_weather = self.weather_by_day.get(current_date, [])
        if not day_weather:
            return

        record_date = current_date >= self.start_date
        day_profile = self.simulation_snapshot.day_profiles.get(current_date) or _day_profile(
            current_date,
            day_weather,
            self.weather_by_day,
        )
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_sensor_samples = 0
        daily_moisture_tracker = _new_daily_moisture_tracker()
        record_hourly = record_date and _uses_hourly_chart(self.start_date, self.end_date)

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current_date, observed_local, day_profile)
            self._apply_weather_window(current_date, [hour_weather], self.states)
            self._apply_weather_window(current_date, [hour_weather], self.probe_states)
            self._calibrate_probe_states(current_date, observed_local, day_profile)

            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            sample_now = False
            hourly_sensor_samples = 0

            if slot is not None:
                sample_now = record_date and self._should_sample(observed_local)
                calibrate_now = sample_now or not record_date
                if sample_now:
                    self.last_sensor_sample_at = observed_local
                    self.sensor_sample_count += 1
                if record_date:
                    self.sampled_weather_rows += 1

                slot_result = self._run_slot(
                    current_date,
                    hour_weather,
                    day_profile,
                    slot,
                    calibrate_now,
                    sample_now,
                    record_date,
                    daily_moisture_tracker,
                )
                hourly_sensor_samples = int(slot_result["sensor_samples"])
                self.sensor_sample_row_count += hourly_sensor_samples
                daily_sensor_samples += hourly_sensor_samples
                hourly_water_ml = slot_result["water_ml"]
                hourly_events = slot_result["events"]
                hourly_decisions = slot_result["decisions"]
                hourly_alerts = slot_result["alerts"]
                daily_water_ml += hourly_water_ml
                daily_events += hourly_events
                daily_decisions += hourly_decisions
                daily_alerts += hourly_alerts
                self._run_probe_slot(current_date, hour_weather, day_profile, slot)
            else:
                snapshot_label = _daily_moisture_snapshot_label(current_date, observed_local, day_profile)
                if record_date and snapshot_label:
                    _record_daily_moisture_snapshot(daily_moisture_tracker, self.states, snapshot_label)

            if record_hourly:
                self.detail_entries.append(
                    _hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        self.states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        {
                            **_hourly_line_metadata(self.selected_sensor_context, current_date, observed_local, hour_weather),
                            "sparse_sensor_sample": sample_now,
                            "sparse_sensor_samples": hourly_sensor_samples,
                        },
                    )
                )
        if not record_date:
            return

        self.entries.append(
            _daily_aggregate_entry(
                current_date,
                day_profile,
                self.states,
                daily_water_ml,
                daily_events,
                daily_decisions,
                daily_alerts,
                {
                    **_daily_line_metadata(self.selected_sensor_context, current_date, day_weather),
                    "sparse_sensor_sample": daily_sensor_samples > 0,
                    "sparse_sensor_samples": daily_sensor_samples,
                },
                moisture_summary=_daily_moisture_summary(daily_moisture_tracker, self.states),
            )
        )
        self.total_water_ml += daily_water_ml
        self.total_irrigation_decisions += daily_decisions

    def _should_sample(self, observed_local: datetime) -> bool:
        if self.last_sensor_sample_at is None:
            return True
        elapsed_hours = (observed_local - self.last_sensor_sample_at).total_seconds() / 3600.0
        return elapsed_hours >= self.sample_interval_hours

    def _run_slot(
        self,
        current_date: date,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
        calibrate_now: bool,
        sample_now: bool,
        record_date: bool,
        daily_moisture_tracker: dict[str, Any],
    ) -> dict[str, float | int]:
        observed_local = _local_observed_at(hour_weather)
        water_ml = 0.0
        events = 0
        decisions = 0
        alerts = 0
        sample_sensor_ids: set[int] = set()
        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

        for pot in self.control_pots:
            state = self.states[pot["id"]]
            if calibrate_now:
                sensor_id = self._refresh_state_from_sensor(
                    state,
                    pot,
                    current_date,
                    observed_local,
                    day_profile,
                    record_stats=sample_now,
                )
                if sample_now and sensor_id is not None:
                    sample_sensor_ids.add(sensor_id)

            if _is_emergency_dryness(state, pot, current_date, observed_local) and record_date:
                self.alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness at sparse decision slot"))
                alerts += 1

            decision = _with_sensor_key(
                _make_baseline_irrigation_decision(state, pot, hour_weather, day_profile, slot),
                pot,
                self.sensor_context,
            )
            decision = _apply_cold_month_indoor_skip(decision, pot, current_date)
            decision_by_pot_id[int(pot["id"])] = decision
            if record_date:
                self.decisions.append(decision)
                decisions += 1

            if decision["should_irrigate"] and _is_valve_managed_pot(pot, current_date):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        snapshot_label = _daily_moisture_snapshot_label(current_date, observed_local, day_profile)
        if record_date and snapshot_label:
            _record_daily_moisture_snapshot(daily_moisture_tracker, self.states, snapshot_label)

        for zone, trigger_decisions in zone_trigger_decisions.items():
            trigger_pot_ids = _trigger_pot_ids(trigger_decisions)
            trigger_sensor_ids = _trigger_sensor_ids(trigger_decisions)
            trigger_pot_codes = _trigger_pot_codes(trigger_decisions)
            zone_dose_factor = _sparse_zone_dose_factor(trigger_decisions, self.sample_interval_hours, sample_now)
            execution_decisions = _zone_execution_decision_map(
                decision_by_pot_id,
                self.zone_pots,
                zone,
                current_date,
                trigger_decisions,
            )
            zone_events = _execute_valve_zone_distribution(
                self.states,
                self.zone_pots,
                zone,
                current_date,
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
            for event in zone_events:
                if record_date:
                    self.events.append(event)
                    events += 1
                    water_ml += event["planned_volume_ml"]

        return {
            "water_ml": water_ml,
            "events": events,
            "decisions": decisions,
            "alerts": alerts,
            "sensor_samples": len(sample_sensor_ids),
        }

    def _refresh_state_from_sensor(
        self,
        state: PotState,
        pot: dict[str, Any],
        experiment_date: date,
        observed_at: datetime,
        day_profile: dict[str, Any],
        record_stats: bool = True,
    ) -> int | None:
        calibration_at = _sampling_calibration_at(experiment_date, observed_at, day_profile)
        before = PotState(state.moisture)
        pot_id = int(pot["id"])
        association = (self.sensor_context.get("associations") or {}).get(pot_id) or {}
        reading = _sensor_reading_for_pot(self.sensor_context, pot, experiment_date, calibration_at)
        if reading is None:
            reading = _forecast_sensor_reading_for_pot(
                self.probe_states,
                self.sensor_context,
                pot,
                experiment_date,
                calibration_at,
            )
        if reading is None:
            if record_stats:
                self.sampling_stats["missing_refreshes"] += 1
            return None
        reading = _apply_calibration_reading(state, reading, calibration_at)

        if record_stats:
            _record_sampling_estimation_error(self.sampling_stats, before, state)
            self.sampling_stats["sensor_refreshes"] += 1
            if reading.get("source") == SPARSE_FORECAST_SENSOR_SOURCE:
                self.sampling_stats["forecast_refreshes"] += 1
        inferred = reading.get("association_source") == "associated_sensor"
        if record_stats and inferred:
            distance = _number(association.get("distance"), 1.0)
            self.sampling_stats["associated_refreshes"] += 1
            self.sampling_stats["association_distance_sum"] += distance
        elif record_stats:
            self.sampling_stats["direct_refreshes"] += 1
        return int(reading.get("associated_sensor_id") or reading.get("sensor_id") or pot["id"])

    def _apply_weather_window(
        self,
        current_date: date,
        weather_rows: list[dict[str, Any]],
        states: dict[int, PotState],
    ) -> None:
        if not weather_rows:
            return

        reference_et_mm = sum(_hourly_reference_et_mm(row) for row in weather_rows)
        precipitation_mm = sum(_number(row.get("precipitation_mm"), 0.0) for row in weather_rows)
        indoor_hours = len(weather_rows)
        for pot in self.pots:
            state = states[pot["id"]]
            if _is_outdoor(pot, current_date):
                loss = reference_et_mm * pot["evaporation_factor"] * pot["_sun_factor"] * pot["_wind_factor"]
                if pot["plant_type_code"] in {"vegetables", "herbs"}:
                    loss *= 1.12
                elif pot["plant_type_code"] == "succulents":
                    loss *= 0.48
                loss *= _low_retention_drydown_multiplier(pot)
                rain_gain = min(
                    8.0,
                    precipitation_mm * _rain_exposure_factor(pot, current_date) * 0.85,
                )
                state.moisture += rain_gain - loss
            else:
                state.moisture -= _indoor_hourly_moisture_loss(pot, current_date) * indoor_hours
            state.moisture = _clamp(state.moisture, _minimum_realistic_moisture(pot, current_date), 100.0)

    def _calibrate_probe_states(
        self,
        current_date: date,
        observed_local: datetime,
        day_profile: dict[str, Any],
    ) -> None:
        for pot in self.control_pots:
            _apply_sensor_calibration_marker(
                self.probe_states[pot["id"]],
                pot,
                current_date,
                observed_local,
                self.sensor_context,
                day_profile,
            )

    def _run_probe_slot(
        self,
        current_date: date,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
    ) -> None:
        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

        for pot in self.control_pots:
            state = self.probe_states[pot["id"]]
            decision = _make_baseline_irrigation_decision(state, pot, hour_weather, day_profile, slot)
            decision = _with_sensor_key(decision, pot, self.sensor_context)
            decision = _apply_cold_month_indoor_skip(decision, pot, current_date)
            decision_by_pot_id[int(pot["id"])] = decision
            if decision["should_irrigate"] and _is_valve_managed_pot(pot, current_date):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        for zone, trigger_decisions in zone_trigger_decisions.items():
            trigger_pot_ids = _trigger_pot_ids(trigger_decisions)
            trigger_sensor_ids = _trigger_sensor_ids(trigger_decisions)
            trigger_pot_codes = _trigger_pot_codes(trigger_decisions)
            zone_dose_factor = _baseline_zone_dose_factor(trigger_decisions)
            execution_decisions = _zone_execution_decision_map(
                decision_by_pot_id,
                self.zone_pots,
                zone,
                current_date,
                trigger_decisions,
            )
            _execute_valve_zone_distribution(
                self.probe_states,
                self.zone_pots,
                zone,
                current_date,
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

    def _summary(self, valve_rollup: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        summary = _daily_summary(
            entries=self.entries,
            pots=self.pots,
            weather_rows=self.weather_rows,
            total_water_ml=self.total_water_ml,
            total_irrigation_events=len(valve_rollup["events"]),
            total_irrigation_decisions=len(valve_rollup["decisions"]),
            alerts=self.alerts,
            start_date=self.start_date,
            end_date=self.end_date,
            sensor_context=self.selected_sensor_context,
        )
        sampling_estimation = _sampling_estimation_summary(self.sampling_stats)
        summary.update(sampling_estimation)
        summary.update(_sensor_control_summary_fields(self.pots, self.selected_sensor_context))
        summary.update(
            {
                "potIrrigationDecisions": self.total_irrigation_decisions,
                "potIrrigationActions": len(self.events),
                "decisionLevel": "valve_zone",
                "sampledWeatherRows": self.sampled_weather_rows,
                "sampledSensorMoments": self.sensor_sample_count,
                "sampledSensorRows": self.sensor_sample_row_count,
                "weatherSamplingPolicy": "hourly-weather-state-decision-slot-control",
                "samplingDataPolicy": "decision-slot-sensor-sampling-hourly-state",
                "forecastSensorCalibrationPolicy": "sparse-sampling-fully-calibrates-to-default-strategy-at-sampling-points",
                "stateSimulationStartDate": self.simulation_start_date.isoformat(),
                "stateLookbackDays": (self.end_date - self.simulation_start_date).days + 1,
                "stateAnchorPolicy": self.state_anchor_policy,
                "samplingWarmupReusePolicy": self.warmup_reuse_policy,
                "baselineWarmupStateSimulationStartDate": (
                    self.sensor_state_anchor.get("baseline_state_simulation_start_date")
                    if isinstance(self.sensor_state_anchor, dict)
                    else None
                ),
                "baselineWarmupStateLookbackDays": (
                    self.sensor_state_anchor.get("baseline_state_lookback_days")
                    if isinstance(self.sensor_state_anchor, dict)
                    else None
                ),
                "sparseSimulationGranularity": "hourly_state_decision_slot_control",
            }
        )
        if self.sensor_state_anchor is not None:
            summary["stateSensorAnchor"] = self.sensor_state_anchor
        return summary


def _run_fuzzy_dt_daily_irrigation(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    simulation_start_date = start_date
    simulation_snapshot = selected_snapshot
    weather_rows = selected_snapshot.selected_weather_rows
    pots = simulation_snapshot.pots
    zone_pots = _pots_by_valve_zone(pots)
    sensor_context = simulation_snapshot.sensor_context
    selected_sensor_context = selected_snapshot.sensor_context
    control_pots = _sensor_control_pots(pots, sensor_context)
    control_pot_ids = {int(pot["id"]) for pot in control_pots}
    weather_by_day = simulation_snapshot.weather_by_day
    pot_states = _copy_pot_states(simulation_snapshot.initial_pot_states)
    sensor_state_anchor = _initialize_states_from_first_day_sensor_readings(
        pot_states,
        pots,
        sensor_context,
        simulation_start_date,
    )

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

    current_date = simulation_start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        record_date = current_date >= start_date
        day_profile = simulation_snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_prescription_sum = 0.0
        daily_prescription_count = 0
        daily_prescribed_zones: set[str] = set()

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            hourly_prescription_sum = 0.0
            hourly_prescription_count = 0
            slot = DEFAULT_FUZZY_POLICY.decision_slot(current_date, observed_local, day_profile)
            decision_by_pot_id: dict[int, dict[str, Any]] = {}
            zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

            for pot in pots:
                state = pot_states[pot["id"]]
                _apply_hourly_environment(
                    state,
                    pot,
                    hour_weather,
                    day_profile,
                    observed_local.date(),
                    rain_exposure_factor=_rain_exposure_factor(pot, observed_local.date()),
                )

                if slot is None:
                    if int(pot["id"]) in control_pot_ids and _is_emergency_dryness(state, pot, current_date, observed_local):
                        if record_date:
                            alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                            daily_alerts += 1
                            hourly_alerts += 1
            if slot is not None:
                for pot in control_pots:
                    state = pot_states[pot["id"]]
                    decision = _make_fuzzy_dt_decision(state, pot, hour_weather, day_profile, slot)
                    decision = _with_sensor_key(decision, pot, sensor_context)
                    decision = _apply_cold_month_indoor_skip(decision, pot, current_date)
                    decision_by_pot_id[int(pot["id"])] = decision

                    prescription_mm = float(decision.get("prescription_mm", 0.0))
                    if record_date:
                        decisions.append(decision)
                        daily_decisions += 1
                        hourly_decisions += 1
                        total_irrigation_decisions += 1
                        prescription_sum += prescription_mm
                        prescription_count += 1
                        daily_prescription_sum += prescription_mm
                        daily_prescription_count += 1
                        hourly_prescription_sum += prescription_mm
                        hourly_prescription_count += 1

                    if decision["should_irrigate"] and _is_valve_managed_pot(pot, current_date):
                        zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

            if slot is not None:
                for zone, trigger_decisions in zone_trigger_decisions.items():
                    if (
                        zone in daily_prescribed_zones
                        and not FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                            slot,
                            day_profile,
                            trigger_decisions,
                        )
                    ):
                        for decision in trigger_decisions:
                            decision["should_irrigate"] = False
                            decision["reason_code"] = "fuzzy_daily_prescription_already_sent"
                            decision["reason_detail"] = (
                                f"Valve zone {zone} already received its fuzzy daily prescription "
                                f"on {current_date.isoformat()}."
                            )
                        continue

                    trigger_pot_ids = _trigger_pot_ids(trigger_decisions)
                    trigger_sensor_ids = _trigger_sensor_ids(trigger_decisions)
                    trigger_pot_codes = _trigger_pot_codes(trigger_decisions)
                    trigger_prescriptions = [float(decision.get("prescription_mm") or 0.0) for decision in trigger_decisions]
                    zone_max_prescription_mm = max(trigger_prescriptions, default=0.0)
                    zone_average_prescription_mm = sum(trigger_prescriptions) / max(len(trigger_prescriptions), 1)
                    execution_decisions = _zone_execution_decision_map(
                        decision_by_pot_id,
                        zone_pots,
                        zone,
                        current_date,
                        trigger_decisions,
                    )

                    def fuzzy_zone_decision(zone_pot: dict[str, Any], zone_decision: dict[str, Any]) -> dict[str, Any]:
                        zone_state = pot_states[int(zone_pot["id"])]
                        prescription_mm = float(zone_decision.get("prescription_mm") or zone_average_prescription_mm)
                        planned_volume_ml = DEFAULT_FUZZY_POLICY.prescribed_volume_ml(
                            zone_state,
                            zone_pot,
                            prescription_mm,
                        )
                        zone_decision["prescription_mm"] = round(prescription_mm, 2)
                        zone_decision["planned_volume_ml"] = round(planned_volume_ml, 2)
                        if zone_decision.get("should_irrigate"):
                            zone_decision["reason_code"] = "valve_zone_prescription"
                            zone_decision["reason_detail"] = (
                                f"Valve zone {zone} is triggered by {len(trigger_pot_ids)} pot(s); "
                                "the fuzzy prescription is applied as a valve-zone runtime budget."
                            )
                        else:
                            zone_decision["reason_code"] = "valve_zone_passive_delivery"
                            zone_decision["reason_detail"] = (
                                f"Valve zone {zone} is triggered by another pot; this pot keeps its own "
                                "zone-scaled fuzzy prescription while the valve is open."
                            )
                        return zone_decision

                    zone_events = _execute_valve_zone_distribution(
                        pot_states,
                        zone_pots,
                        zone,
                        current_date,
                        hour_weather,
                        execution_decisions,
                        fuzzy_zone_decision,
                        DEFAULT_FUZZY_POLICY.irrigation_request,
                        {
                            "zone_triggered": True,
                            "zone_trigger_sensor_ids": trigger_sensor_ids,
                            "zone_trigger_pot_ids": trigger_pot_ids,
                            "zone_trigger_pot_codes": trigger_pot_codes,
                            "runtime_request_sensor_ids": trigger_sensor_ids,
                            "zone_prescription_mm": round(zone_average_prescription_mm, 2),
                            "zone_max_prescription_mm": round(zone_max_prescription_mm, 2),
                            "zone": zone,
                            "zone_activation_policy": "sensor_pot_trigger",
                            "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
                            "zone_daily_execution_policy": (
                                "heatwave_evening_supplement"
                                if zone in daily_prescribed_zones and slot == "evening"
                                else "daily_zone_prescription"
                            ),
                            "minimum_valve_runtime_min": FUZZY_ACTUATION_POLICY.minimum_runtime(trigger_decisions),
                        },
                    )
                    for event in zone_events:
                        if record_date:
                            events.append(event)
                            daily_events += 1
                            daily_water_ml += event["planned_volume_ml"]
                            hourly_events += 1
                            hourly_water_ml += event["planned_volume_ml"]
                            total_irrigation_events += 1
                    if zone_events:
                        daily_prescribed_zones.add(zone)

                for pot in pots:
                    state = pot_states[pot["id"]]
                    if state.moisture > pot["moisture_max_pct"]:
                        state.too_wet_hours += 1
                        if record_date and state.too_wet_hours == 24:
                            alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                            daily_alerts += 1
                            hourly_alerts += 1
                    else:
                        state.too_wet_hours = 0

            if record_date and _uses_hourly_chart(start_date, end_date):
                hourly_avg_prescription_mm = (
                    round(hourly_prescription_sum / hourly_prescription_count, 2)
                    if hourly_prescription_count > 0
                    else None
                )
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
                            **_hourly_line_metadata(selected_sensor_context, current_date, observed_local, hour_weather),
                            "fuzzy_prescription_mm": hourly_avg_prescription_mm,
                            "avg_prescription_mm": hourly_avg_prescription_mm,
                        },
                    )
                )

        if not record_date:
            current_date += timedelta(days=1)
            continue

        daily_avg_prescription_mm = (
            round(daily_prescription_sum / daily_prescription_count, 2)
            if daily_prescription_count > 0
            else None
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
                    **_daily_line_metadata(selected_sensor_context, current_date, day_weather),
                    "fuzzy_prescription_mm": daily_avg_prescription_mm,
                    "avg_prescription_mm": daily_avg_prescription_mm,
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
        sensor_context=selected_sensor_context,
    )
    summary["potIrrigationDecisions"] = total_irrigation_decisions
    summary["potIrrigationActions"] = len(events)
    summary["decisionLevel"] = "valve_zone"
    summary["averagePrescriptionMm"] = round(prescription_sum / max(prescription_count, 1), 2)
    summary["fuzzyDataPolicy"] = "daily-fis-prescription"
    summary["fuzzyControllerPolicy"] = "fuzzy_dt_prescription_control"
    summary["fuzzyDecisionPolicy"] = "daily_zone_prescription_with_heatwave_evening_supplement"
    summary["fuzzyActuationPolicy"] = FUZZY_ACTUATION_POLICY.summary()
    summary["fuzzySensorCalibrationPolicy"] = "initial_state_only"
    summary["stateSimulationStartDate"] = simulation_start_date.isoformat()
    summary["stateLookbackDays"] = (end_date - simulation_start_date).days + 1
    summary["stateAnchorPolicy"] = "experiment_start_sensor_anchor"
    summary.update(_sensor_control_summary_fields(pots, selected_sensor_context))
    if sensor_state_anchor is not None:
        summary["stateSensorAnchor"] = sensor_state_anchor
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
    model: ANFIS | _AnfisModelController,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    decision_threshold: float = ANFIS_DECISION_THRESHOLD,
    forecast_decision_threshold: float = ANFIS_FORECAST_DECISION_THRESHOLD,
) -> dict[str, Any]:
    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    simulation_start_date = start_date
    simulation_snapshot = selected_snapshot

    weather_rows = selected_snapshot.selected_weather_rows
    pots = simulation_snapshot.pots
    zone_pots = _pots_by_valve_zone(pots)
    sensor_context = simulation_snapshot.sensor_context
    selected_sensor_context = selected_snapshot.sensor_context
    control_pots = _sensor_control_pots(pots, sensor_context)
    control_pot_ids = {int(pot["id"]) for pot in control_pots}
    weather_by_day = simulation_snapshot.weather_by_day
    pot_states = _copy_pot_states(simulation_snapshot.initial_pot_states)
    previous_moisture_by_pot = {
        int(pot_id): float(state.moisture)
        for pot_id, state in pot_states.items()
    }

    entries = []
    detail_entries = []
    decisions = []
    events = []
    alerts = []

    total_water_ml = 0.0
    total_irrigation_events = 0
    total_irrigation_decisions = 0
    last_irrigated_by_zone: dict[str, date] = {}
    sensor_state_anchor = _initialize_states_from_first_day_sensor_readings(
        pot_states,
        pots,
        sensor_context,
        start_date,
    )

    current_date = simulation_start_date
    while current_date <= end_date:
        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        record_date = current_date >= start_date
        day_profile = simulation_snapshot.day_profiles.get(current_date) or _day_profile(current_date, day_weather, weather_by_day)
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_moisture_tracker = _new_daily_moisture_tracker()
        probability_sum = 0.0
        probability_count = 0
        probability_max = 0.0
        zone_signal_sum = 0.0
        zone_signal_count = 0
        zone_signal_max = 0.0
        current_decision_threshold = _anfis_decision_threshold(
            sensor_context,
            current_date,
            decision_threshold,
            forecast_decision_threshold,
        )

        for hour_weather in day_weather:
            observed_local = _local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            hourly_probability_sum = 0.0
            hourly_probability_count = 0
            hourly_probability_max = 0.0
            hourly_zone_signal_sum = 0.0
            hourly_zone_signal_count = 0
            hourly_zone_signal_max = 0.0
            slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current_date, observed_local, day_profile)
            decision_by_pot_id: dict[int, dict[str, Any]] = {}
            slot_decisions: list[dict[str, Any]] = []
            slot_decisions_by_zone: dict[str, list[dict[str, Any]]] = {}
            eligible_decisions_by_zone: dict[str, list[dict[str, Any]]] = {}
            safety_decisions_by_zone: dict[str, list[dict[str, Any]]] = {}
            zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}
            prior_moisture_by_pot: dict[int, float | None] = {}

            for pot in pots:
                state = pot_states[pot["id"]]
                pot_id = int(pot["id"])
                prior_moisture = previous_moisture_by_pot.get(pot_id)
                prior_moisture_by_pot[pot_id] = prior_moisture
                _apply_hourly_environment(
                    state,
                    pot,
                    hour_weather,
                    day_profile,
                    observed_local.date(),
                    rain_exposure_factor=_rain_exposure_factor(pot, observed_local.date()),
                )

                if slot is None:
                    if int(pot["id"]) in control_pot_ids and _is_emergency_dryness(state, pot, current_date, observed_local):
                        if record_date:
                            alerts.append(_alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                            daily_alerts += 1
                            hourly_alerts += 1
                    previous_moisture_by_pot[pot_id] = float(state.moisture)
                    continue
                previous_moisture_by_pot[pot_id] = float(state.moisture)

            if slot is not None:
                for pot in control_pots:
                    state = pot_states[pot["id"]]
                    prior_moisture = prior_moisture_by_pot.get(int(pot["id"]))
                    anfis_input = _anfis_inputs(
                        state,
                        hour_weather,
                        None,
                        pot,
                        day_profile,
                        prior_moisture_pct=prior_moisture,
                    )
                    rule_decision = _make_anfis_execution_decision(state, pot, hour_weather, day_profile, slot)
                    predicted_probability = _predict_anfis_probability(model, anfis_input, pot.get("balcony_zone"))
                    probability_sum += predicted_probability
                    probability_count += 1
                    probability_max = max(probability_max, predicted_probability)
                    hourly_probability_sum += predicted_probability
                    hourly_probability_count += 1
                    hourly_probability_max = max(hourly_probability_max, predicted_probability)

                    valve_managed = _is_valve_managed_pot(pot, current_date)

                    decision = dict(rule_decision)
                    decision = _with_sensor_key(decision, pot, sensor_context)
                    decision["should_irrigate"] = False
                    decision["predicted_probability"] = round(predicted_probability, 4)
                    decision["anfis_decision_threshold"] = current_decision_threshold
                    decision["predicted_category"] = probability_category(predicted_probability)
                    decision["simulated_moisture_pct"] = decision["current_moisture_pct"]
                    decision["current_moisture_pct"] = round(anfis_input["moisture"], 2)
                    decision["anfis_input_moisture_pct"] = round(anfis_input["moisture"], 2)
                    decision["anfis_input_temperature_c"] = round(anfis_input["temperature"], 2)
                    decision["anfis_input_rain_mm"] = round(anfis_input["rain"], 2)
                    decision = _apply_cold_month_indoor_skip(decision, pot, current_date)
                    hard_stop = decision["reason_code"] in ANFIS_HARD_STOP_REASON_CODES
                    below_target = state.moisture < decision["target_moisture_pct"]
                    safety_threshold = _threshold_for_pot(pot, day_profile, slot)
                    trigger_threshold = DEFAULT_IRRIGATION_POLICY.trigger_threshold(pot, day_profile, slot)
                    below_trigger_threshold = state.moisture < trigger_threshold
                    decision["anfis_trigger_threshold_pct"] = round(trigger_threshold, 2)
                    below_safety_threshold = state.moisture < safety_threshold
                    decision["anfis_safety_threshold_pct"] = round(safety_threshold, 2)
                    decision["anfis_below_safety_threshold"] = below_safety_threshold
                    rain_policy = DEFAULT_IRRIGATION_POLICY.rain_policy(pot, current_date, day_profile)
                    covered_rain_need = (
                        DEFAULT_IRRIGATION_POLICY.is_covered_rain_day(rain_policy, day_profile)
                        and state.moisture < decision["target_moisture_pct"]
                    )
                    decision["anfis_covered_rain_need"] = covered_rain_need

                    if record_date:
                        decisions.append(decision)
                        daily_decisions += 1
                        hourly_decisions += 1
                    decision_by_pot_id[int(pot["id"])] = decision
                    slot_decisions.append(decision)
                    if valve_managed:
                        slot_decisions_by_zone.setdefault(pot["balcony_zone"], []).append(decision)

                    if not hard_stop and valve_managed and (
                        (below_target and below_trigger_threshold)
                        or covered_rain_need
                    ):
                        eligible_decisions_by_zone.setdefault(pot["balcony_zone"], []).append(decision)
                    if not hard_stop and below_safety_threshold and valve_managed:
                        safety_decisions_by_zone.setdefault(pot["balcony_zone"], []).append(decision)

            snapshot_label = _daily_moisture_snapshot_label(current_date, observed_local, day_profile)
            if record_date and snapshot_label:
                _record_daily_moisture_snapshot(daily_moisture_tracker, pot_states, snapshot_label)

            if slot is not None:
                slot_average_probability = hourly_probability_sum / max(hourly_probability_count, 1)
                slot_max_probability = hourly_probability_max
                for decision in slot_decisions:
                    decision["anfis_global_slot_average_probability"] = round(slot_average_probability, 4)
                    decision["anfis_global_slot_average_probability_percent"] = round(slot_average_probability * 100.0, 2)
                    decision["anfis_global_slot_max_probability"] = round(slot_max_probability, 4)
                    decision["anfis_global_slot_max_probability_percent"] = round(slot_max_probability * 100.0, 2)
                    decision["anfis_slot_average_probability"] = round(slot_average_probability, 4)
                    decision["anfis_slot_average_probability_percent"] = round(slot_average_probability * 100.0, 2)
                    decision["anfis_slot_max_probability"] = round(slot_max_probability, 4)
                    decision["anfis_slot_max_probability_percent"] = round(slot_max_probability * 100.0, 2)

                zone_probability_summary = {
                    zone: _anfis_zone_probability_summary(zone_decisions)
                    for zone, zone_decisions in slot_decisions_by_zone.items()
                }
                slot_zone_probabilities = [
                    zone_average_probability
                    for zone_average_probability, _ in zone_probability_summary.values()
                ]
                if slot_zone_probabilities:
                    slot_zone_signal = sum(slot_zone_probabilities) / len(slot_zone_probabilities)
                    slot_zone_max = max(slot_zone_probabilities)
                    zone_signal_sum += slot_zone_signal
                    zone_signal_count += 1
                    zone_signal_max = max(zone_signal_max, slot_zone_max)
                    hourly_zone_signal_sum += slot_zone_signal
                    hourly_zone_signal_count += 1
                    hourly_zone_signal_max = max(hourly_zone_signal_max, slot_zone_max)
                for zone, zone_decisions in slot_decisions_by_zone.items():
                    zone_average_probability, zone_max_probability = zone_probability_summary[zone]
                    for decision in zone_decisions:
                        decision["anfis_slot_average_probability"] = round(zone_average_probability, 4)
                        decision["anfis_slot_average_probability_percent"] = round(zone_average_probability * 100.0, 2)
                        decision["anfis_slot_max_probability"] = round(zone_max_probability, 4)
                        decision["anfis_slot_max_probability_percent"] = round(zone_max_probability * 100.0, 2)
                        decision["anfis_zone_activation_probability"] = round(zone_average_probability, 4)
                        decision["anfis_zone_activation_probability_percent"] = round(zone_average_probability * 100.0, 2)

                    eligible_decisions = eligible_decisions_by_zone.get(zone, [])
                    safety_decisions = safety_decisions_by_zone.get(zone, [])
                    zone_activation_probability = zone_average_probability
                    if zone_activation_probability >= current_decision_threshold:
                        for decision in eligible_decisions:
                            decision["should_irrigate"] = True
                            decision["reason_code"] = "anfis_zone_probability_high"
                            decision["reason_detail"] = (
                                f"Valve-zone average ANFIS probability {zone_activation_probability:.2f} is above threshold "
                                f"{current_decision_threshold:.2f}; max pot probability is {zone_max_probability:.2f}, "
                                "and this pot is below target moisture."
                                f"{_anfis_duration_policy_note(decision)}"
                            )
                        if eligible_decisions:
                            zone_trigger_decisions[zone] = eligible_decisions

                        for decision in zone_decisions:
                            if not decision.get("should_irrigate") and decision["reason_code"] not in ANFIS_HARD_STOP_REASON_CODES:
                                decision["reason_code"] = (
                                    "anfis_zone_probability_high_pot_not_triggering"
                                    if eligible_decisions
                                    else "anfis_zone_probability_high_no_moisture_deficit"
                                )
                                decision["reason_detail"] = (
                                    f"Valve-zone average ANFIS probability {zone_activation_probability:.2f} is above threshold "
                                    f"{current_decision_threshold:.2f}, but this pot is not a managed below-target trigger."
                                    if eligible_decisions
                                    else (
                                        f"Valve-zone average ANFIS probability {zone_activation_probability:.2f} is above threshold "
                                        f"{current_decision_threshold:.2f}, but no managed pot in this valve zone "
                                        "is below target moisture."
                                    )
                                )
                    elif safety_decisions:
                        for decision in safety_decisions:
                            decision["should_irrigate"] = True
                            decision["reason_code"] = "anfis_moisture_safety_threshold"
                            decision["reason_detail"] = (
                                f"Moisture {decision['current_moisture_pct']:.1f}% is below safety threshold "
                                f"{decision['anfis_safety_threshold_pct']:.1f}%; valve-zone average ANFIS probability "
                                f"{zone_activation_probability:.2f} is below threshold {current_decision_threshold:.2f}, "
                                f"so a moisture safety override waters the valve zone."
                                f"{_anfis_duration_policy_note(decision)}"
                            )
                        zone_trigger_decisions[zone] = safety_decisions

                        for decision in zone_decisions:
                            if not decision.get("should_irrigate") and decision["reason_code"] not in ANFIS_HARD_STOP_REASON_CODES:
                                decision["reason_code"] = "anfis_probability_low_safety_not_triggering"
                                decision["reason_detail"] = (
                                    f"Valve-zone average ANFIS probability {zone_activation_probability:.2f} is below threshold "
                                    f"{current_decision_threshold:.2f}; another managed pot in the valve zone "
                                    "triggered the moisture safety override."
                                )
                    else:
                        for decision in zone_decisions:
                            if decision["reason_code"] not in ANFIS_HARD_STOP_REASON_CODES:
                                decision["reason_code"] = "anfis_zone_probability_low"
                                decision["reason_detail"] = (
                                    f"Valve-zone average ANFIS probability {zone_activation_probability:.2f} is below threshold "
                                    f"{current_decision_threshold:.2f}; max pot probability is {zone_max_probability:.2f}."
                                )

                for decision in slot_decisions:
                    if decision["reason_code"] == "anfis_probability_pending":
                        decision["reason_code"] = "anfis_not_valve_managed"
                        decision["reason_detail"] = "Pot is not managed by an outdoor irrigation valve in this season."

                for zone in list(zone_trigger_decisions):
                    trigger_decisions = zone_trigger_decisions[zone]
                    zone_activation_probability = zone_probability_summary.get(zone, (slot_average_probability, slot_max_probability))[0]
                    if not _anfis_zone_cadence_blocks(
                        last_irrigated_by_zone,
                        zone,
                        current_date,
                        trigger_decisions,
                        zone_activation_probability,
                        day_profile,
                    ):
                        continue
                    cadence_days = _anfis_zone_cadence_days(day_profile)
                    last_irrigated = last_irrigated_by_zone.get(zone)
                    for decision in trigger_decisions:
                        decision["should_irrigate"] = False
                        decision["reason_code"] = "anfis_water_saving_cadence"
                        decision["reason_detail"] = (
                            f"Valve-zone average ANFIS probability {zone_activation_probability:.2f} is above threshold "
                            f"{current_decision_threshold:.2f}, but valve zone {zone} was watered on "
                            f"{last_irrigated.isoformat() if last_irrigated else 'a recent day'}; "
                            f"water-saving cadence waits {cadence_days} days unless moisture is unsafe."
                        )
                    del zone_trigger_decisions[zone]

                for zone, trigger_decisions in zone_trigger_decisions.items():
                    trigger_pot_ids = _trigger_pot_ids(trigger_decisions)
                    trigger_sensor_ids = _trigger_sensor_ids(trigger_decisions)
                    trigger_pot_codes = _trigger_pot_codes(trigger_decisions)
                    zone_activation_probability = zone_probability_summary.get(zone, (slot_average_probability, slot_max_probability))[0]
                    zone_dose_factor = _anfis_zone_dose_factor(
                        trigger_decisions,
                        zone_activation_probability,
                        day_profile,
                        current_decision_threshold,
                    )
                    minimum_runtime_min = (
                        ANFIS_WATER_SAVING_POLICY.safety_minimum_runtime_min
                        if ANFIS_WATER_SAVING_POLICY._has_safety_override(trigger_decisions)
                        else ANFIS_WATER_SAVING_POLICY.minimum_runtime_min
                    )
                    for decision in trigger_decisions:
                        decision["dose_factor"] = zone_dose_factor
                        decision["dose_policy_source"] = "anfis_water_saving_policy"
                        decision["reason_detail"] = (
                            f"{decision['reason_detail']} Final ANFIS zone dose is "
                            f"{zone_dose_factor * 100.0:.0f}% of calculated need."
                        )

                    def anfis_zone_decision(zone_pot: dict[str, Any], zone_decision: dict[str, Any]) -> dict[str, Any]:
                        zone_decision["should_irrigate"] = True
                        zone_decision["dose_factor"] = zone_dose_factor
                        zone_decision["dose_policy_source"] = "anfis_water_saving_policy"
                        zone_state = pot_states[zone_pot["id"]]
                        zone_decision["full_dose_start_moisture_pct"] = round(zone_state.moisture, 2)
                        return zone_decision

                    execution_decisions = _zone_execution_decision_map(
                        decision_by_pot_id,
                        zone_pots,
                        zone,
                        current_date,
                        trigger_decisions,
                    )
                    zone_events = _execute_valve_zone_distribution(
                        pot_states,
                        zone_pots,
                        zone,
                        current_date,
                        hour_weather,
                        execution_decisions,
                        anfis_zone_decision,
                        DEFAULT_IRRIGATION_POLICY.irrigation_request,
                        {
                            "zone_triggered": True,
                            "zone_trigger_sensor_ids": trigger_sensor_ids,
                            "zone_trigger_pot_ids": trigger_pot_ids,
                            "zone_trigger_pot_codes": trigger_pot_codes,
                            "runtime_request_sensor_ids": trigger_sensor_ids,
                            "zone_dose_factor": zone_dose_factor,
                            "zone": zone,
                            "zone_activation_probability": round(zone_activation_probability, 4),
                            "zone_activation_policy": "sensor_pot_probability_with_safety_override",
                            "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
                            "dose_policy_source": "anfis_water_saving_policy",
                            "minimum_valve_runtime_min": minimum_runtime_min,
                        },
                    )
                    for event in zone_events:
                        if record_date:
                            events.append(event)
                            daily_events += 1
                            daily_water_ml += event["planned_volume_ml"]
                            hourly_events += 1
                            hourly_water_ml += event["planned_volume_ml"]
                    if zone_events:
                        last_irrigated_by_zone[zone] = current_date

                for pot in pots:
                    state = pot_states[pot["id"]]
                    if state.moisture > pot["moisture_max_pct"]:
                        state.too_wet_hours += 1
                        if record_date and state.too_wet_hours == 24:
                            alerts.append(_alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                            daily_alerts += 1
                            hourly_alerts += 1
                    else:
                        state.too_wet_hours = 0

            if record_date and _uses_hourly_chart(start_date, end_date):
                hourly_probability = hourly_probability_sum / hourly_probability_count if hourly_probability_count else 0.0
                hourly_zone_probability = (
                    hourly_zone_signal_sum / hourly_zone_signal_count
                    if hourly_zone_signal_count
                    else hourly_probability
                )
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
                            **_hourly_line_metadata(selected_sensor_context, current_date, observed_local, hour_weather),
                            "predicted_probability": round(hourly_zone_probability, 4),
                            "predicted_probability_percent": round(hourly_zone_probability * 100.0, 2),
                            "trigger_probability": round(hourly_zone_signal_max or hourly_probability_max, 4),
                            "trigger_probability_percent": round((hourly_zone_signal_max or hourly_probability_max) * 100.0, 2),
                            "global_predicted_probability": round(hourly_probability, 4),
                            "global_predicted_probability_percent": round(hourly_probability * 100.0, 2),
                            "anfis_decision_threshold": round(current_decision_threshold, 4),
                            "anfis_decision_threshold_percent": round(current_decision_threshold * 100.0, 2),
                        },
                    )
                )

        if not record_date:
            current_date += timedelta(days=1)
            continue

        global_predicted_probability = probability_sum / max(probability_count, 1)
        predicted_probability = (
            zone_signal_sum / zone_signal_count
            if zone_signal_count
            else global_predicted_probability
        )
        moisture_summary = _daily_moisture_summary(daily_moisture_tracker, pot_states)
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
                    **_daily_line_metadata(selected_sensor_context, current_date, day_weather),
                    "predicted_probability": round(predicted_probability, 4),
                    "predicted_probability_percent": round(predicted_probability * 100.0, 2),
                    "trigger_probability": round(zone_signal_max or probability_max, 4),
                    "trigger_probability_percent": round((zone_signal_max or probability_max) * 100.0, 2),
                    "global_predicted_probability": round(global_predicted_probability, 4),
                    "global_predicted_probability_percent": round(global_predicted_probability * 100.0, 2),
                    "anfis_decision_threshold": round(current_decision_threshold, 4),
                    "anfis_decision_threshold_percent": round(current_decision_threshold * 100.0, 2),
                },
                moisture_summary=moisture_summary,
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
        sensor_context=selected_sensor_context,
    )
    summary["stateSimulationStartDate"] = simulation_start_date.isoformat()
    summary["stateLookbackDays"] = (end_date - simulation_start_date).days + 1
    summary["stateAnchorPolicy"] = "experiment_start_sensor_anchor"
    summary["anfisSensorCalibrationPolicy"] = "initial_state_only"
    summary.update(_sensor_control_summary_fields(pots, selected_sensor_context))
    if sensor_state_anchor is not None:
        summary["anfisSensorStateAnchor"] = sensor_state_anchor
    summary["potIrrigationDecisions"] = total_irrigation_decisions
    summary["potIrrigationActions"] = len(events)
    summary["decisionLevel"] = "valve_zone"
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


def _serialize_pot_states(states: dict[int, PotState]) -> dict[str, dict[str, float | int]]:
    return {
        str(pot_id): {
            "moisture": round(float(state.moisture), 4),
            "too_wet_hours": int(state.too_wet_hours),
        }
        for pot_id, state in states.items()
    }


def _copy_pot_states_from_payload(
    payload: dict[str, Any],
    fallback_states: dict[int, PotState],
) -> dict[int, PotState]:
    states = _copy_pot_states(fallback_states)
    if not isinstance(payload, dict):
        return states

    for raw_pot_id, raw_state in payload.items():
        try:
            pot_id = int(raw_pot_id)
        except (TypeError, ValueError):
            continue
        if pot_id not in states:
            continue

        fallback = states[pot_id]
        if isinstance(raw_state, dict):
            moisture = _number(raw_state.get("moisture"), fallback.moisture)
            too_wet_hours = int(_number(raw_state.get("too_wet_hours"), fallback.too_wet_hours))
        else:
            moisture = _number(raw_state, fallback.moisture)
            too_wet_hours = fallback.too_wet_hours
        states[pot_id] = PotState(
            moisture=_clamp(moisture, 0.0, 100.0),
            too_wet_hours=max(0, too_wet_hours),
        )
    return states


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
        "min_temperature": round(day_profile["min_temperature_c"], 2),
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
    moisture_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    moisture = moisture_summary or _daily_moisture_summary(_new_daily_moisture_tracker(), pot_states)
    valve_runs = max(0, int(daily_events or 0))
    entry = {
        "date": current_date.isoformat(),
        "timestamp": datetime.combine(current_date, time(12, 0), tzinfo=LOCAL_TZ).isoformat(),
        "day_label": current_date.strftime("%Y-%m-%d"),
        "chart_label": current_date.strftime("%Y-%m-%d"),
        "moisture": moisture["moisture"],
        "average_moisture": moisture["average_moisture"],
        "min_moisture": moisture["min_moisture"],
        "max_moisture": moisture["max_moisture"],
        "moisture_sample_count": moisture.get("moisture_sample_count", 0),
        "moisture_sample_method": moisture.get("moisture_sample_method", "end_of_day"),
        "moisture_sample_labels": moisture.get("moisture_sample_labels", []),
        "pre_irrigation_moisture": moisture.get("pre_irrigation_moisture"),
        "post_irrigation_moisture": moisture.get("post_irrigation_moisture"),
        "temperature": round(day_profile["avg_temperature_c"], 2),
        "max_temperature": round(day_profile["max_temperature_c"], 2),
        "min_temperature": round(day_profile["min_temperature_c"], 2),
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
    radiation_values = [_number(row.get("shortwave_radiation_w_m2"), 0.0) for row in day_weather]
    precipitation = sum(_number(row["precipitation_mm"], 0.0) for row in day_weather)
    reference_et = sum(_hourly_reference_et_mm(row) for row in day_weather)
    rain_probabilities = [_number(row.get("precipitation_probability_pct"), 0.0) for row in day_weather]
    gusts = [_number(row["wind_gust_kmh"], _number(row["wind_speed_kmh"], 0.0)) for row in day_weather]
    lookahead_rows = [
        row
        for offset in range(BASELINE_WINTER_LOOKAHEAD_DAYS)
        for row in weather_by_day.get(day + timedelta(days=offset), [])
    ]
    lookahead_temperatures = [_number(row["temperature_c"], 20.0) for row in lookahead_rows] or temperatures
    lookahead_precipitation = sum(_number(row["precipitation_mm"], 0.0) for row in lookahead_rows)
    lookahead_probabilities = [
        _number(row.get("precipitation_probability_pct"), 0.0)
        for row in lookahead_rows
    ]
    freeze_risk = min(temperatures) <= 0 or _upcoming_freeze(day, weather_by_day)
    no_rain_10_days = _precipitation_last_days(day, weather_by_day, days=10) < 1.0
    dry_streak_days = _dry_streak_days(day, weather_by_day)

    max_temperature = max(temperatures)
    max_gust = max(gusts)
    avg_humidity = sum(humidities) / max(len(humidities), 1)
    avg_cloud_cover = sum(cloud_covers) / max(len(cloud_covers), 1)
    avg_radiation = sum(radiation_values) / max(len(radiation_values), 1)

    return {
        "season": _season(day),
        "dormant_period": day.month in {12, 1, 2, 3},
        "avg_temperature_c": sum(temperatures) / max(len(temperatures), 1),
        "max_temperature_c": max_temperature,
        "min_temperature_c": min(temperatures),
        "avg_humidity_pct": avg_humidity,
        "avg_cloud_cover_pct": avg_cloud_cover,
        "avg_shortwave_radiation_w_m2": avg_radiation,
        "max_shortwave_radiation_w_m2": max(radiation_values) if radiation_values else 0.0,
        "precipitation_mm": precipitation,
        "precipitation_next_14_days_mm": lookahead_precipitation,
        "reference_evapotranspiration_mm": reference_et,
        "max_precipitation_probability_pct": max(rain_probabilities) if rain_probabilities else 0.0,
        "max_precipitation_probability_next_14_days_pct": max(lookahead_probabilities) if lookahead_probabilities else 0.0,
        "max_wind_gust_kmh": max_gust,
        "min_temperature_next_14_days_c": min(lookahead_temperatures),
        "heatwave_day": max_temperature >= 30.0,
        "dry_windy_day": max_gust >= 35.0 and avg_humidity <= 55.0,
        "freeze_risk": freeze_risk,
        "no_rain_10_days": no_rain_10_days,
        "dry_streak_days": dry_streak_days,
    }


def _dry_streak_days(day: date, weather_by_day: dict[date, list[dict[str, Any]]]) -> int:
    streak = 0
    current = day
    while True:
        rows = weather_by_day.get(current)
        if not rows:
            return streak
        precipitation = sum(_number(row.get("precipitation_mm"), 0.0) for row in rows)
        if precipitation >= 0.5:
            return streak
        streak += 1
        current -= timedelta(days=1)


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


def _apply_hourly_environment(
    state: PotState,
    pot: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    local_day: date,
    rain_exposure_factor: float = 1.0,
) -> None:
    if _is_outdoor(pot, local_day):
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
        loss *= _low_retention_drydown_multiplier(pot)

        effective_rain_mm = _number(weather["precipitation_mm"], 0.0) * _clamp(rain_exposure_factor, 0.0, 1.0)
        rain_gain = min(8.0, effective_rain_mm * 0.85)
        state.moisture += rain_gain - loss
    else:
        state.moisture -= _indoor_hourly_moisture_loss(pot, local_day)

    state.moisture = _clamp(state.moisture, _minimum_realistic_moisture(pot, local_day), 100.0)


def _low_retention_drydown_multiplier(pot: dict[str, Any]) -> float:
    retention = _clamp(_number(pot.get("retention_factor"), 1.0), 0.1, 2.0)
    return 1.0 + max(0.0, 1.0 - retention) * 0.65


def _indoor_hourly_moisture_loss(pot: dict[str, Any], local_day: date) -> float:
    if local_day.month in {11, 12, 1, 2, 3}:
        return 0.003 if pot["plant_type_code"] != "succulents" else 0.001
    return 0.018 if pot["plant_type_code"] != "succulents" else 0.006


def _minimum_realistic_moisture(pot: dict[str, Any], local_day: date) -> float:
    if local_day.month in {11, 12, 1, 2, 3}:
        return max(8.0, float(pot["winter_moisture_target_pct"]) - 6.0)
    return max(7.0, float(pot["moisture_min_pct"]) - 8.0)


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
    if not sensor_context or not sensor_context.get("available"):
        return []

    pot_by_sensor_id = {int(pot["id"]): pot for pot in pots}
    lookup = sensor_context.get("lookup") or {}
    weather_by_day = weather_by_day or _group_weather_by_day(weather_rows)
    day_profiles = day_profiles or {}
    dataset: list[dict[str, float | str]] = []
    seen: set[tuple[date, time, int]] = set()
    latest_by_sensor: dict[int, dict[str, Any]] = {}

    exact_sensor_keys = [
        key for key in lookup.keys()
        if len(key) >= 3 and not isinstance(key[1], int)
    ]
    seen_readings: set[tuple[int, str]] = set()
    sorted_keys = sorted(
        exact_sensor_keys,
        key=lambda item: (item[0], _sensor_lookup_time(item[1]), int(item[2])),
    )
    for reading_date, slot_time, sensor_id in sorted_keys:
        slot_time = _sensor_lookup_time(slot_time)
        key = (reading_date, slot_time, int(sensor_id))
        if key in seen:
            continue
        seen.add(key)

        pot = pot_by_sensor_id.get(int(sensor_id))
        if pot is None:
            continue
        sensor_reading = _lookup_sensor_reading(lookup, reading_date, slot_time, int(sensor_id))
        if sensor_reading is None:
            continue
        recorded_at = sensor_reading.get("recorded_at")
        reading_time_key = (
            _local_timestamp_key(recorded_at)
            if recorded_at is not None
            else f"{reading_date.isoformat()}T{slot_time.isoformat()}"
        )
        reading_identity = (
            int(sensor_id),
            reading_time_key,
        )
        if reading_identity in seen_readings:
            continue

        day_weather = weather_by_day.get(reading_date, [])
        if not day_weather:
            continue
        observed_at = datetime.combine(reading_date, slot_time, tzinfo=LOCAL_TZ)
        weather = _weather_for_hour(day_weather, observed_at)
        if weather is None:
            continue
        day_profile = day_profiles.get(reading_date) or _day_profile(reading_date, day_weather, weather_by_day)
        decision_slot = DEFAULT_IRRIGATION_POLICY.decision_slot(reading_date, observed_at, day_profile)
        if decision_slot is None:
            decision_slot = _anfis_training_slot(reading_date, day_profile, rng)

        prior_reading = latest_by_sensor.get(int(sensor_id))
        example = _anfis_training_example(
            pot,
            sensor_reading,
            weather,
            day_profile,
            decision_slot,
            prior_reading=prior_reading,
            slot_time=slot_time,
        )
        if example is not None:
            dataset.append(example)
            seen_readings.add(reading_identity)
        latest_by_sensor[int(sensor_id)] = sensor_reading

    if samples > 0 and len(dataset) > samples:
        return _weighted_anfis_dataset_sample(dataset, samples, rng)
    return dataset


def _weighted_anfis_dataset_sample(
    dataset: list[dict[str, float | str]],
    samples: int,
    rng: random.Random,
) -> list[dict[str, float | str]]:
    if samples >= len(dataset):
        return list(dataset)
    selected: list[dict[str, float | str]] = []
    remaining = list(dataset)
    while remaining and len(selected) < samples:
        total_weight = sum(max(0.1, float(item.get("training_weight", 1.0))) for item in remaining)
        pick = rng.uniform(0.0, total_weight)
        cursor = 0.0
        for index, item in enumerate(remaining):
            cursor += max(0.1, float(item.get("training_weight", 1.0)))
            if cursor >= pick:
                selected.append(item)
                del remaining[index]
                break
    return selected


def _split_anfis_training_calibration(
    dataset: list[dict[str, float | str]],
    seed: int | None,
    calibration_share: float = ANFIS_CALIBRATION_SHARE,
) -> tuple[list[dict[str, float | str]], list[dict[str, float | str]]]:
    if len(dataset) < 2:
        return dataset, dataset
    rng = random.Random(None if seed is None else seed + 17)
    rows = list(dataset)
    rng.shuffle(rows)
    calibration_count = max(1, min(len(rows) - 1, int(round(len(rows) * calibration_share))))
    calibration = _stratified_anfis_test_sample(rows, calibration_count, rng)
    calibration_ids = {id(item) for item in calibration}
    training = [item for item in rows if id(item) not in calibration_ids]
    return training or rows, calibration or training or rows


def _expand_anfis_training_dataset(dataset: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
    expanded: list[dict[str, float | str]] = []
    for item in dataset:
        weight = max(1, min(5, int(round(float(item.get("training_weight", 1.0))))))
        expanded.extend([item] * weight)
    return expanded or dataset


def _train_anfis_controller(
    train_dataset: list[dict[str, float | str]],
    calibration_dataset: list[dict[str, float | str]],
    generations: int,
    population: int,
    seed: int | None,
) -> _AnfisModelController:
    weighted_train = _expand_anfis_training_dataset(train_dataset)
    global_model = ANFIS()
    global_model.fit(
        weighted_train,
        generations=generations,
        population=population,
        seed=seed,
    )
    global_calibrator = _AnfisProbabilityCalibrator.fit(global_model, calibration_dataset)
    zone_models: dict[str, ANFIS] = {}
    zone_calibrators: dict[str, _AnfisProbabilityCalibrator] = {}
    by_zone: dict[str, list[dict[str, float | str]]] = {}
    for item in train_dataset:
        zone = str(item.get("valve_zone") or "")
        if zone:
            by_zone.setdefault(zone, []).append(item)

    zone_generations = max(8, min(generations, generations // 2 or generations))
    zone_population = max(8, min(population, population // 2 or population))
    for zone, rows in sorted(by_zone.items()):
        if len(rows) < ANFIS_ZONE_MODEL_MIN_SAMPLES:
            continue
        zone_training, zone_calibration = _split_anfis_training_calibration(
            rows,
            None if seed is None else seed + _valve_number_for_zone(zone),
            calibration_share=ANFIS_CALIBRATION_SHARE,
        )
        model = ANFIS(
            membership_params=list(global_model.membership_params),
            rule_outputs=list(global_model.rule_outputs),
        )
        model.fit(
            _expand_anfis_training_dataset(zone_training),
            generations=zone_generations,
            population=zone_population,
            seed=None if seed is None else seed + _valve_number_for_zone(zone),
        )
        zone_models[zone] = model
        zone_calibrators[zone] = _AnfisProbabilityCalibrator.fit(model, zone_calibration)

    return _AnfisModelController(
        global_model=global_model,
        global_calibrator=global_calibrator,
        zone_models=zone_models,
        zone_calibrators=zone_calibrators,
    )


def _stratified_anfis_test_sample(
    dataset: list[dict[str, float | str]],
    test_count: int,
    rng: random.Random,
) -> list[dict[str, float | str]]:
    if test_count <= 0:
        return []

    by_category: dict[str, list[dict[str, float | str]]] = {}
    for item in dataset:
        by_category.setdefault(str(item.get("target_category", "unknown")), []).append(item)
    for rows in by_category.values():
        rng.shuffle(rows)

    selected: list[dict[str, float | str]] = []
    for rows in by_category.values():
        category_count = int(round(test_count * len(rows) / len(dataset)))
        if category_count == 0 and len(rows) > 1 and len(selected) < test_count:
            category_count = 1
        category_count = min(category_count, len(rows) - 1 if len(rows) > 1 else len(rows))
        selected.extend(rows[:category_count])

    if len(selected) < test_count:
        selected_ids = {id(item) for item in selected}
        remaining = [item for item in dataset if id(item) not in selected_ids]
        rng.shuffle(remaining)
        selected.extend(remaining[: test_count - len(selected)])
    elif len(selected) > test_count:
        rng.shuffle(selected)
        selected = selected[:test_count]

    return selected


def _anfis_training_example(
    pot: dict[str, Any],
    sensor_reading: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    slot: str,
    prior_reading: dict[str, Any] | None = None,
    slot_time: time | None = None,
) -> dict[str, float | str] | None:
    moisture = _number(sensor_reading.get("soil_moisture_pct"), None)
    if moisture is None:
        return None

    state = PotState(moisture=_clamp(float(moisture), 0.0, 100.0))
    prior_moisture = (
        _number(prior_reading.get("soil_moisture_pct"), None)
        if prior_reading
        else None
    )
    inputs = _anfis_inputs(
        state,
        weather,
        sensor_reading,
        pot,
        day_profile,
        prior_moisture_pct=prior_moisture,
    )
    probability = _anfis_training_target_probability(inputs)
    signals, weight = _anfis_training_signals(pot, sensor_reading, day_profile, prior_reading, slot_time)
    return {
        **inputs,
        "target_probability": probability,
        "target_category": probability_category(probability),
        "valve_zone": str(pot.get("balcony_zone") or ""),
        "training_signals": ",".join(signals),
        "training_weight": weight,
    }


def _anfis_training_signals(
    pot: dict[str, Any],
    sensor_reading: dict[str, Any],
    day_profile: dict[str, Any],
    prior_reading: dict[str, Any] | None,
    slot_time: time | None,
) -> tuple[list[str], float]:
    moisture = _number(sensor_reading.get("soil_moisture_pct"), 50.0)
    target = _number(pot.get("moisture_target_pct"), 40.0)
    min_moisture = _number(pot.get("moisture_min_pct"), target - 8.0)
    max_temp = _number(day_profile.get("max_temperature_c"), 20.0)
    rain_mm = _number(day_profile.get("precipitation_mm"), 0.0)
    signals = ["real_sensor_reading"]
    weight = 1.0

    if moisture <= min(target, 42.0) and max_temp >= 30.0 and rain_mm < 0.5:
        signals.append("dry_hot_no_rain")
        weight += 2.0
    elif moisture <= target and rain_mm < 0.5:
        signals.append("dry_no_rain")
        weight += 1.0

    if _is_post_irrigation_recovery_reading(sensor_reading, prior_reading, slot_time, target, moisture):
        signals.append("post_irrigation_recovery")
        weight += 1.0

    if moisture <= min_moisture:
        signals.append("below_minimum")
        weight += 1.0

    return signals, min(weight, 5.0)


def _is_post_irrigation_recovery_reading(
    sensor_reading: dict[str, Any],
    prior_reading: dict[str, Any] | None,
    slot_time: time | None,
    target: float,
    moisture: float,
) -> bool:
    if slot_time is not None and (
        time(7, 0) <= slot_time <= time(10, 30)
        or time(19, 0) <= slot_time <= time(22, 30)
    ) and moisture >= target - 2.0:
        return True

    if not prior_reading:
        return False
    prior_moisture = _number(prior_reading.get("soil_moisture_pct"), moisture)
    return moisture >= target - 2.0 and moisture - prior_moisture >= 2.0


def _anfis_training_signal_summary(dataset: list[dict[str, float | str]]) -> dict[str, Any]:
    signal_counts: dict[str, int] = {}
    weighted_samples = 0
    zones: dict[str, int] = {}
    for item in dataset:
        weighted_samples += max(1, min(5, int(round(float(item.get("training_weight", 1.0))))))
        zone = str(item.get("valve_zone") or "")
        if zone:
            zones[zone] = zones.get(zone, 0) + 1
        for signal in str(item.get("training_signals") or "real_sensor_reading").split(","):
            if signal:
                signal_counts[signal] = signal_counts.get(signal, 0) + 1
    return {
        "samples": len(dataset),
        "weighted_samples": weighted_samples,
        "signals": dict(sorted(signal_counts.items())),
        "valve_zones": dict(sorted(zones.items())),
    }


def _anfis_training_target_probability(
    inputs: dict[str, float],
) -> float:
    probability = target_probability(
        float(inputs["moisture"]),
        float(inputs["temperature"]),
        float(inputs["rain"]),
    )
    return round(_clamp(probability, 0.02, 0.95), 4)


def _anfis_training_slot(observed_date: date, day_profile: dict[str, Any], rng: random.Random) -> str:
    if observed_date.month in {12, 1, 2, 3}:
        return "winter_check"
    if _number(day_profile.get("max_temperature_c"), 20.0) >= 32.0 and rng.random() < 0.35:
        return "evening"
    return "morning"


def _predict_anfis_probability(model: ANFIS | _AnfisModelController, inputs: dict[str, Any], zone: str | None = None) -> float:
    if isinstance(model, _AnfisModelController):
        return model.predict(inputs, zone or str(inputs.get("valve_zone") or ""))
    return model.predict(inputs)


def _evaluate_anfis_model(model: ANFIS | _AnfisModelController, dataset: list[dict[str, float | str]]) -> dict[str, Any]:
    matches = 0
    decision_matches = 0
    mse = 0.0
    for item in dataset:
        predicted = _predict_anfis_probability(model, item)
        target_probability = float(item["target_probability"])
        mse += (predicted - target_probability) ** 2
        if probability_category(predicted) == item["target_category"]:
            matches += 1
        if (predicted >= ANFIS_DECISION_THRESHOLD) == (target_probability >= ANFIS_DECISION_THRESHOLD):
            decision_matches += 1

    mse /= max(len(dataset), 1)
    rmse = mse**0.5
    return {
        "test_mse": round(mse, 6),
        "test_rmse": round(rmse, 4),
        "test_probability_fit_percent": round(max(0.0, 1.0 - rmse) * 100.0, 2),
        "test_accuracy_percent": round(matches / max(len(dataset), 1) * 100.0, 2),
        "test_decision_accuracy_percent": round(decision_matches / max(len(dataset), 1) * 100.0, 2),
        "test_decision_threshold": ANFIS_DECISION_THRESHOLD,
        "test_samples": len(dataset),
    }


def _local_timestamp_key(value: str | datetime) -> str:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value)
    else:
        local_value = value
    if local_value.tzinfo is not None:
        local_value = local_value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return local_value.replace(microsecond=0).isoformat()



