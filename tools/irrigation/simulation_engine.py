from __future__ import annotations

import random
import math
import time as perf_time
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

from database import get_connection, initialize_database
from tools.anfis import ANFIS, probability_category
from tools.irrigation.models import (
    ANFIS_DECISION_THRESHOLD,
    ExperimentSnapshot,
    HOURLY_CHART_MAX_RANGE_DAYS,
    LOCAL_TZ,
    PotState,
)
from tools.irrigation.utils import (
    clamp as _clamp,
    local_observed_at as _local_observed_at,
    number as _number,
    season as _season,
    sun_factor as _sun_factor,
    wind_factor as _wind_factor,
)
from tools.irrigation.weather import (
    _load_weather,
    _raise_if_missing_historical_weather,
    _with_estimated_future_weather,
)
from tools.sensor_readings import DEFAULT_SENSOR_SOURCE, load_sensor_readings_for_experiment


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
    _prime_future_states(
        initial_pot_states,
        pots,
        sensor_context,
        start_date,
        _group_weather_by_day(weather_rows),
    )

    return ExperimentSnapshot(
        start_date=start_date,
        end_date=end_date,
        pot_count=len(pots),
        pots=pots,
        weather_rows=weather_rows,
        selected_weather_rows=selected_weather_rows,
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
    weather_by_day = _group_weather_by_day(snapshot.weather_rows)
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

        day_profile = _day_profile(current_date, day_weather, weather_by_day)
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

                decision = _make_irrigation_decision(state, pot, hour_weather, day_profile, slot)
                decisions.append(decision)
                daily_decisions += 1
                hourly_decisions += 1

                if decision["should_irrigate"]:
                    event = _apply_irrigation_event(state, pot, hour_weather, decision)
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]

                if state.moisture > float(pot["moisture_max_pct"]):
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
                "rain_prediction": day_profile["precipitation_mm"] >= 0.5,
                "rain_amount": round(day_profile["precipitation_mm"], 2),
                "wind_gust_kmh": round(day_profile["max_wind_gust_kmh"], 2),
                "heatwave_day": day_profile["heatwave_day"],
                "freeze_risk": day_profile["freeze_risk"],
                "irrigation_active": daily_events > 0,
                "irrigation_events": daily_events,
                "irrigation_decisions": daily_decisions,
                "irrigated_pots": len({event["pot_id"] for event in events if event["date"] == current_date.isoformat()}),
                "alerts": daily_alerts,
                "water_usage_ml": round(daily_water_ml, 2),
                "water_usage_l": round(daily_water_ml / 1000.0, 2),
            }
        )
        current_date += timedelta(days=1)

    if persist:
        _persist_daily_results("baseline", start_date, end_date, decisions, events, alerts)

    total_days = len(entries)
    total_pots = len(pots)
    summary = {
        "totalEntries": total_days,
        "daysAnalyzed": total_days,
        "potsAnalyzed": total_pots,
        "weatherRows": len(weather_rows),
        "irrigationEvents": total_irrigation_events,
        "irrigationDecisions": total_irrigation_decisions,
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
        "sampleDecisions": decisions[:200],
        "sampleEvents": events[:200],
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
                "rain_prediction": sparse_entry["rain_prediction"],
                "rain_amount": sparse_entry["rain_amount"],
                "baseline_irrigation_active": baseline_active,
                "sparse_irrigation_active": sparse_active,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "sparse_irrigation_events": sparse_entry["irrigation_events"],
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "sparse_water_usage_l": sparse_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "sparse_water_usage_ml": sparse_entry["water_usage_ml"],
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
                "alerts": sparse_entry["alerts"],
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
                "rain_prediction": sparse_entry["rain_prediction"],
                "rain_amount": sparse_entry["rain_amount"],
                "baseline_irrigation_active": baseline_active,
                "sparse_irrigation_active": sparse_active,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "sparse_irrigation_events": sparse_entry["irrigation_events"],
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "sparse_water_usage_l": sparse_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "sparse_water_usage_ml": sparse_entry["water_usage_ml"],
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
                "alerts": sparse_entry["alerts"],
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
        "baseline_irrigation_decisions": baseline_summary["irrigationDecisions"],
        "sparse_irrigation_decisions": sparse_summary["irrigationDecisions"],
        "sampledWeatherRows": sparse_summary.get("sampledWeatherRows", 0),
        "samplingDataPolicy": sparse_summary.get("samplingDataPolicy", "sensor-and-weather-sampled"),
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
    train_dataset = _generate_database_anfis_dataset(weather_rows, pots, train_samples, seed, sensor_context)
    test_dataset = _generate_database_anfis_dataset(
        weather_rows,
        pots,
        test_samples,
        (seed + 1) if seed is not None else None,
        sensor_context,
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
                "rain_prediction": anfis_entry["rain_prediction"],
                "rain_amount": anfis_entry["rain_amount"],
                "predicted_probability": predicted_probability,
                "predicted_probability_percent": predicted_probability_percent,
                "predicted_category": probability_category(predicted_probability) if predicted_probability is not None else "not_applicable",
                "baseline_irrigation_active": baseline_entry["irrigation_events"] > 0,
                "anfis_irrigation_active": anfis_entry["irrigation_events"] > 0,
                "baseline_irrigation_events": baseline_entry["irrigation_events"],
                "anfis_irrigation_events": anfis_entry["irrigation_events"],
                "baseline_water_usage_l": baseline_entry["water_usage_l"],
                "anfis_water_usage_l": anfis_entry["water_usage_l"],
                "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
                "anfis_water_usage_ml": anfis_entry["water_usage_ml"],
                "alerts": anfis_entry["alerts"],
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
        return rows


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


def _load_sensor_context(start_date: date, end_date: date, pots: list[dict[str, Any]]) -> dict[str, Any]:
    pot_ids = [pot["id"] for pot in pots]
    try:
        return load_sensor_readings_for_experiment(
            start_date=start_date,
            end_date=end_date,
            pot_ids=pot_ids,
            source=DEFAULT_SENSOR_SOURCE,
        )
    except Exception as exc:
        return {
            "available": False,
            "source": DEFAULT_SENSOR_SOURCE,
            "lookup": {},
            "mapped_dates": {},
            "row_count": 0,
            "error": str(exc),
        }


def _apply_sensor_reading(
    state: PotState,
    pot: dict[str, Any],
    experiment_date: date,
    observed_at: datetime,
    sensor_context: dict[str, Any],
) -> dict[str, Any] | None:
    if not sensor_context.get("available"):
        return None

    reading = sensor_context["lookup"].get((experiment_date, observed_at.hour, pot["id"]))
    if reading is None:
        return None

    state.moisture = _clamp(_number(reading["soil_moisture_pct"], state.moisture), 0.0, 100.0)
    return reading


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
        latest = latest_states.get(pot["id"])
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
    while current < end:
        day_weather = weather_by_day.get(current.date(), [])
        hour_weather = _weather_for_hour(day_weather, current)
        if hour_weather is None:
            current += timedelta(hours=1)
            continue

        day_profile = _day_profile(current.date(), day_weather, weather_by_day)
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
        "sensorLocationCount": len(sensor_context.get("sensor_pot_ids", [])),
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
    weather_by_day = _group_weather_by_day(snapshot.weather_rows)
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

        day_profile = _day_profile(current_date, day_weather, weather_by_day)
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

                decision = _make_irrigation_decision(controller_state, pot, controller_weather, controller_day_profile, slot)
                decisions.append(decision)
                daily_decisions += 1
                hourly_decisions += 1

                if decision["should_irrigate"]:
                    event = _apply_irrigation_event(controller_state, pot, controller_weather, decision)
                    _apply_planned_volume(actual_state, pot, event["planned_volume_ml"])
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]

                if actual_state.moisture > float(pot["moisture_max_pct"]):
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
                    )
                )
            hour_index += 1

        entries.append(_daily_aggregate_entry(current_date, day_profile, actual_states, daily_water_ml, daily_events, daily_decisions, daily_alerts))
        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions
        current_date += timedelta(days=1)

    summary = _daily_summary(
        entries=entries,
        pots=pots,
        weather_rows=weather_rows,
        total_water_ml=total_water_ml,
        total_irrigation_events=total_irrigation_events,
        total_irrigation_decisions=total_irrigation_decisions,
        alerts=alerts,
        start_date=start_date,
        end_date=end_date,
        sensor_context=sensor_context,
    )
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
        "sampleDecisions": decisions[:200],
        "sampleEvents": events[:200],
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
    weather_by_day = _group_weather_by_day(snapshot.weather_rows)
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

        day_profile = _day_profile(current_date, day_weather, weather_by_day)
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
                    event = _apply_irrigation_event(state, pot, hour_weather, decision)
                    events.append(event)
                    daily_events += 1
                    daily_water_ml += event["planned_volume_ml"]
                    hourly_events += 1
                    hourly_water_ml += event["planned_volume_ml"]

                if state.moisture > float(pot["moisture_max_pct"]):
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
                {"predicted_probability": round(predicted_probability, 4)},
            )
        )
        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions
        current_date += timedelta(days=1)

    summary = _daily_summary(
        entries=entries,
        pots=pots,
        weather_rows=weather_rows,
        total_water_ml=total_water_ml,
        total_irrigation_events=total_irrigation_events,
        total_irrigation_decisions=total_irrigation_decisions,
        alerts=alerts,
        start_date=start_date,
        end_date=end_date,
        sensor_context=sensor_context,
    )
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
        "sampleDecisions": decisions[:200],
        "sampleEvents": events[:200],
        "sampleAlerts": alerts[:200],
    }


def _initial_pot_states(pots: list[dict[str, Any]]) -> dict[int, PotState]:
    states = {}
    for pot in pots:
        rng = random.Random(2026 + pot["id"])
        target = float(pot["moisture_target_pct"])
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
    rain_amount = _number(weather["precipitation_mm"], 0.0)
    wind_gust = _number(weather["wind_gust_kmh"], _number(weather["wind_speed_kmh"], 0.0))
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
        "rain_prediction": rain_amount >= 0.5,
        "rain_amount": round(rain_amount, 2),
        "wind_gust_kmh": round(wind_gust, 2),
        "heatwave_day": day_profile["heatwave_day"],
        "freeze_risk": day_profile["freeze_risk"],
        "irrigation_active": hourly_events > 0,
        "irrigation_events": hourly_events,
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
        "rain_prediction": day_profile["precipitation_mm"] >= 0.5,
        "rain_amount": round(day_profile["precipitation_mm"], 2),
        "wind_gust_kmh": round(day_profile["max_wind_gust_kmh"], 2),
        "heatwave_day": day_profile["heatwave_day"],
        "freeze_risk": day_profile["freeze_risk"],
        "irrigation_active": daily_events > 0,
        "irrigation_events": daily_events,
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
        "irrigationEvents": total_irrigation_events,
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
    precipitation = sum(_number(row["precipitation_mm"], 0.0) for row in day_weather)
    gusts = [_number(row["wind_gust_kmh"], _number(row["wind_speed_kmh"], 0.0)) for row in day_weather]
    freeze_risk = min(temperatures) <= 0 or _upcoming_freeze(day, weather_by_day)
    no_rain_10_days = _precipitation_last_days(day, weather_by_day, days=10) < 1.0

    max_temperature = max(temperatures)
    max_gust = max(gusts)
    avg_humidity = sum(humidities) / max(len(humidities), 1)

    return {
        "season": _season(day),
        "avg_temperature_c": sum(temperatures) / max(len(temperatures), 1),
        "max_temperature_c": max_temperature,
        "min_temperature_c": min(temperatures),
        "avg_humidity_pct": avg_humidity,
        "precipitation_mm": precipitation,
        "max_wind_gust_kmh": max_gust,
        "heatwave_day": max_temperature >= 30.0,
        "dry_windy_day": max_gust >= 35.0 and avg_humidity <= 55.0,
        "freeze_risk": freeze_risk,
        "no_rain_10_days": no_rain_10_days,
    }


def _apply_hourly_environment(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any], local_day: date) -> None:
    outdoor = _is_outdoor(pot, local_day)
    if outdoor:
        evap_mm = _number(weather["evapotranspiration_mm"], None)
        if evap_mm is None:
            temp = _number(weather["temperature_c"], 20.0)
            humidity = _number(weather["relative_humidity_pct"], 60.0)
            wind = _number(weather["wind_speed_kmh"], 5.0)
            evap_mm = max(0.01, 0.025 + (temp / 38.0) * ((100.0 - humidity) / 100.0) * (1.0 + wind / 45.0))

        loss = evap_mm * float(pot["evaporation_factor"]) * _sun_factor(pot) * _wind_factor(pot)
        if pot["plant_type_code"] in {"vegetables", "herbs"}:
            loss *= 1.12
        elif pot["plant_type_code"] == "succulents":
            loss *= 0.48

        rain_gain = min(8.0, _number(weather["precipitation_mm"], 0.0) * 0.85)
        state.moisture += rain_gain - loss
    else:
        state.moisture -= 0.018 if pot["plant_type_code"] != "succulents" else 0.006

    state.moisture = _clamp(state.moisture, 0.0, 100.0)


def _decision_slot(day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
    hour = observed_at.hour
    season = day_profile["season"]
    if season == "summer":
        if 6 <= hour <= 8:
            return "morning"
        if day_profile["heatwave_day"] and 17 <= hour <= 19:
            return "evening"
    elif season == "winter":
        if hour == 10:
            return "winter_check"
    elif season == "autumn":
        if 8 <= hour <= 10:
            return "morning"
    else:
        if day_profile["min_temperature_c"] < 7.0:
            return "morning" if 9 <= hour <= 10 else None
        if 8 <= hour <= 10:
            return "morning"
    return None


def _make_irrigation_decision(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any], slot: str) -> dict[str, Any]:
    target = float(pot["winter_moisture_target_pct"]) if slot == "winter_check" else float(pot["moisture_target_pct"])
    threshold = _threshold_for_pot(pot, day_profile, slot)
    reason_code = "moisture_ok"
    reason_detail = "Moisture is above the decision threshold."
    should_irrigate = False
    observed_local = _local_observed_at(weather)

    if day_profile["freeze_risk"]:
        reason_code = "freeze_risk"
        reason_detail = "Skipped because freezing temperatures are present or forecast."
    elif slot == "winter_check" and not _winter_irrigation_allowed(state, day_profile):
        reason_code = "winter_conditions_not_met"
        reason_detail = "Winter watering requires temperature above 10C, dry soil, and no recent rain."
    elif slot == "evening" and not _second_watering_allowed(state, pot, day_profile):
        reason_code = "second_watering_not_needed"
        reason_detail = "Evening watering is reserved for heatwave/dry wind stress and eligible pots."
    elif _is_outdoor(pot, observed_local.date()) and day_profile["precipitation_mm"] >= 2.0 and state.moisture > threshold * 0.85:
        reason_code = "rain_sufficient"
        reason_detail = "Skipped because stored weather has enough daily precipitation."
    elif state.moisture < threshold:
        should_irrigate = True
        reason_code = "below_moisture_threshold"
        reason_detail = f"Moisture {state.moisture:.1f}% is below threshold {threshold:.1f}%."

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
    }


def _generate_database_anfis_dataset(
    weather_rows: list[dict[str, Any]],
    pots: list[dict[str, Any]],
    samples: int,
    seed: int | None,
    sensor_context: dict[str, Any] | None = None,
) -> list[dict[str, float | str]]:
    rng = random.Random(seed)
    weather_by_day = _group_weather_by_day(weather_rows)
    dataset = []

    for _ in range(samples):
        weather = rng.choice(weather_rows)
        pot = rng.choice(pots)
        observed_local = _local_observed_at(weather)
        day_weather = weather_by_day.get(observed_local.date(), [weather])
        day_profile = _day_profile(observed_local.date(), day_weather, weather_by_day)
        threshold = _threshold_for_pot(pot, day_profile, "morning")
        target = float(pot["moisture_target_pct"])
        sensor_reading = None
        if sensor_context and sensor_context.get("available"):
            sensor_reading = sensor_context["lookup"].get((observed_local.date(), observed_local.hour, pot["id"]))

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


def _size_flow_rate_multiplier(pot: dict[str, Any]) -> float:
    return 1.0


def _apply_irrigation_event(state: PotState, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    target = decision["target_moisture_pct"]
    need_pct = max(0.0, target - state.moisture)
    volume_l = float(pot["volume_l"])
    retention = max(float(pot["retention_factor"]), 0.1)
    flow_rate = max(float(pot["drip_flow_ml_min"]) * _size_flow_rate_multiplier(pot), 1.0)
    planned_volume_ml = max(0.0, need_pct * volume_l * 10.0 / retention)
    max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[pot["size_class"]]
    planned_volume_ml = min(planned_volume_ml, flow_rate * max_minutes)
    duration_min = planned_volume_ml / flow_rate
    cycle_count = 2 if pot["cycle_soak_enabled"] and duration_min >= 10 else 1
    soak_pause_min = 10 if cycle_count == 2 else 0

    _apply_planned_volume(state, pot, planned_volume_ml)

    scheduled_start = _local_observed_at(weather)
    scheduled_end = scheduled_start + timedelta(minutes=duration_min + soak_pause_min)
    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "date": scheduled_start.date().isoformat(),
        "slot": decision["slot"],
        "scheduled_start_at": scheduled_start.isoformat(),
        "scheduled_end_at": scheduled_end.isoformat(),
        "flow_rate_ml_min": round(flow_rate, 2),
        "planned_volume_ml": round(planned_volume_ml, 2),
        "cycle_count": cycle_count,
        "soak_pause_min": soak_pause_min,
    }


def _apply_planned_volume(state: PotState, pot: dict[str, Any], planned_volume_ml: float) -> None:
    volume_l = float(pot["volume_l"])
    retention = max(float(pot["retention_factor"]), 0.1)
    moisture_gain = planned_volume_ml * retention / max(volume_l * 10.0, 1.0)
    state.moisture = _clamp(state.moisture + moisture_gain, 0.0, 100.0)


def _persist_daily_results(
    experiment_type: str,
    start_date: date,
    end_date: date,
    decisions: list[dict[str, Any]],
    events: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    decision_ids: dict[tuple[int, str, str], int] = {}
    start_ts = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_ts = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    with get_connection() as conn:
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
        for decision in decisions:
            row = conn.execute(
                """
                INSERT INTO irrigation_decisions (
                    experiment_type, pot_id, decided_at, decision_date, decision_slot, should_irrigate,
                    reason_code, reason_detail, current_moisture_pct, target_moisture_pct,
                    weather_hourly_id
                )
                VALUES (
                    %(experiment_type)s, %(pot_id)s, %(decided_at)s, %(date)s, %(slot)s, %(should_irrigate)s,
                    %(reason_code)s, %(reason_detail)s, %(current_moisture_pct)s,
                    %(target_moisture_pct)s, %(weather_hourly_id)s
                )
                ON CONFLICT (experiment_type, pot_id, decided_at, decision_slot) DO UPDATE SET
                    decision_date = EXCLUDED.decision_date,
                    should_irrigate = EXCLUDED.should_irrigate,
                    reason_code = EXCLUDED.reason_code,
                    reason_detail = EXCLUDED.reason_detail,
                    current_moisture_pct = EXCLUDED.current_moisture_pct,
                    target_moisture_pct = EXCLUDED.target_moisture_pct,
                    weather_hourly_id = EXCLUDED.weather_hourly_id,
                    changed_at = now()
                RETURNING id
                """,
                {"experiment_type": experiment_type, **decision},
            ).fetchone()
            if row:
                decision_ids[(decision["pot_id"], decision["decided_at"], decision["slot"])] = row[0]
        for event in events:
            decision_id = decision_ids.get((event["pot_id"], event["scheduled_start_at"], event["slot"]))
            row = conn.execute(
                """
                INSERT INTO irrigation_events (
                    experiment_type, decision_id, pot_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, cycle_count, soak_pause_min, status
                )
                VALUES (
                    %(experiment_type)s, %(decision_id)s, %(pot_id)s, %(scheduled_start_at)s, %(scheduled_end_at)s,
                    %(flow_rate_ml_min)s, %(planned_volume_ml)s, %(cycle_count)s, %(soak_pause_min)s, 'planned'
                )
                ON CONFLICT (experiment_type, pot_id, scheduled_start_at) DO UPDATE SET
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
                RETURNING id
                """,
                {"experiment_type": experiment_type, "decision_id": decision_id, **event},
            ).fetchone()
            event_id = row[0] if row else None
            conn.execute(
                """
                INSERT INTO irrigation_actuations (
                    event_id, experiment_type, pot_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, cycle_count, soak_pause_min, status
                )
                VALUES (
                    %(event_id)s, %(experiment_type)s, %(pot_id)s, %(scheduled_start_at)s, %(scheduled_end_at)s,
                    %(flow_rate_ml_min)s, %(planned_volume_ml)s, %(cycle_count)s, %(soak_pause_min)s, 'planned'
                )
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
                {"event_id": event_id, "experiment_type": experiment_type, **event},
            )
        for alert in alerts:
            conn.execute(
                """
                INSERT INTO alerts (experiment_type, pot_id, raised_at, alert_type, severity, title, detail)
                VALUES (%(experiment_type)s, %(pot_id)s, %(raised_at)s, %(alert_type)s, %(severity)s, %(title)s, %(detail)s)
                ON CONFLICT (experiment_type, pot_id, raised_at, alert_type) DO UPDATE SET
                    severity = EXCLUDED.severity,
                    title = EXCLUDED.title,
                    detail = EXCLUDED.detail,
                    changed_at = now()
                """,
                {"experiment_type": experiment_type, **alert},
            )
        conn.commit()


def _threshold_for_pot(pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
    if slot == "winter_check":
        return 10.0
    threshold = float(pot["moisture_min_pct"])
    if day_profile["heatwave_day"] and pot["plant_type_code"] in {"vegetables", "herbs"}:
        threshold += 4.0
    if day_profile["dry_windy_day"] and pot["size_class"] == "small":
        threshold += 3.0
    if slot == "evening":
        threshold = max(8.0, threshold - 3.0)
    return threshold


def _winter_irrigation_allowed(state: PotState, day_profile: dict[str, Any]) -> bool:
    return (
        day_profile["max_temperature_c"] > 10.0
        and day_profile["no_rain_10_days"]
        and state.moisture < 10.0
    )


def _second_watering_allowed(state: PotState, pot: dict[str, Any], day_profile: dict[str, Any]) -> bool:
    eligible = pot["allows_second_watering"] or pot["size_class"] == "small"
    return eligible and (day_profile["heatwave_day"] or day_profile["dry_windy_day"]) and state.moisture < float(pot["moisture_target_pct"])


def _is_emergency_dryness(state: PotState, pot: dict[str, Any], day: date, observed_at: datetime) -> bool:
    if _season(day) == "summer" and 11 <= observed_at.hour <= 16:
        return state.moisture < max(8.0, float(pot["moisture_min_pct"]) - 8.0)
    return False


def _alert_row(pot: dict[str, Any], weather: dict[str, Any], alert_type: str, severity: str, title: str) -> dict[str, Any]:
    observed_local = _local_observed_at(weather)
    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "raised_at": observed_local.isoformat(),
        "alert_type": alert_type,
        "severity": severity,
        "title": title,
        "detail": f"{pot['pot_code']} at {observed_local.isoformat()}",
    }


def _is_outdoor(pot: dict[str, Any], day: date) -> bool:
    if _season(day) == "winter":
        return pot["winter_location"] == "outdoor"
    return pot["default_location"] == "outdoor"


def _upcoming_freeze(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 3) -> bool:
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day + timedelta(days=offset), [])
        if rows and min(_number(row["temperature_c"], 20.0) for row in rows) <= 0:
            return True
    return False


def _precipitation_last_days(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 10) -> float:
    total = 0.0
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day - timedelta(days=offset), [])
        total += sum(_number(row["precipitation_mm"], 0.0) for row in rows)
    return total


