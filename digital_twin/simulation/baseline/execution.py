from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import digital_twin.simulation.metrics as metrics
import digital_twin.simulation.result_helpers as results
import digital_twin.simulation.soil_model as soil
import digital_twin.simulation.state.aggregates as state_aggregates
import digital_twin.simulation.state.environment as state_environment
import digital_twin.simulation.state.lookback as state_lookback
import digital_twin.simulation.state.projection as state_projection
import digital_twin.simulation.sensors.calibration as sensor_calibration
import digital_twin.simulation.sensors.context as sensor_context_helpers
import digital_twin.simulation.valves.distribution as valve_distribution
import digital_twin.simulation.valves.rollups as valve_rollups
import digital_twin.simulation.valves.zones as valve_zones
from digital_twin.simulation.irrigation_controller.baseline_decision import (
    make_baseline_irrigation_decision,
)
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.environment import (
    alert_row,
    is_emergency_dryness,
    rain_exposure_factor,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import ExperimentSnapshot


def run_default_daily_irrigation(
    start_date: date,
    end_date: date,
    persist: bool,
    selected_snapshot: ExperimentSnapshot,
    simulation_start_date: date,
    simulation_snapshot: ExperimentSnapshot,
    state_anchor_policy: str,
    sensor_calibration_policy: str,
    sensor_calibration_marker=None,
) -> dict[str, Any]:
    """Run the default threshold/weather-rule controller over a resolved simulation timeline."""
    _ = persist
    sensor_calibration_marker = sensor_calibration_marker or sensor_calibration.apply_sensor_calibration_marker
    weather_rows = selected_snapshot.selected_weather_rows
    pots = simulation_snapshot.pots
    zone_pots = valve_zones.pots_by_valve_zone(pots)
    sensor_context = simulation_snapshot.sensor_context
    selected_sensor_context = selected_snapshot.sensor_context
    control_pots = sensor_context_helpers.sensor_control_pots(pots, sensor_context)
    control_pot_ids = {int(pot["id"]) for pot in control_pots}
    weather_by_day = simulation_snapshot.weather_by_day
    pot_states = state_environment.copy_pot_states(simulation_snapshot.initial_pot_states)
    sensor_state_anchor = state_projection.initialize_states_from_first_day_sensor_readings(
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
            state_at_experiment_start = state_environment.serialize_pot_states(pot_states)

        day_weather = weather_by_day.get(current_date, [])
        if not day_weather:
            current_date += timedelta(days=1)
            continue

        record_date = current_date >= start_date
        day_profile = (
            simulation_snapshot.day_profiles.get(current_date)
            or state_environment.day_profile(current_date, day_weather, weather_by_day)
        )
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_moisture_tracker = metrics.new_daily_moisture_tracker()

        for hour_weather in day_weather:
            observed_local = soil.local_observed_at(hour_weather)
            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current_date, observed_local, day_profile)
            decision_by_pot_id: dict[int, dict[str, Any]] = {}
            zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

            for pot in pots:
                state = pot_states[pot["id"]]
                state_environment.apply_hourly_environment(
                    state,
                    pot,
                    hour_weather,
                    day_profile,
                    observed_local.date(),
                    rain_exposure_factor=rain_exposure_factor(pot, observed_local.date()),
                )
                if sensor_calibration_policy != "initial_state_only" and int(pot["id"]) in control_pot_ids:
                    sensor_calibration_marker(
                        state,
                        pot,
                        current_date,
                        observed_local,
                        sensor_context,
                        day_profile,
                    )

                if slot is None:
                    if (
                        int(pot["id"]) in control_pot_ids
                        and is_emergency_dryness(state, pot, current_date, observed_local)
                    ):
                        if record_date:
                            alerts.append(
                                alert_row(
                                    pot,
                                    hour_weather,
                                    "emergency_dryness",
                                    "warning",
                                    "Emergency dryness outside watering window",
                                )
                            )
                            daily_alerts += 1
                            hourly_alerts += 1
            if slot is not None:
                for pot in control_pots:
                    state = pot_states[pot["id"]]
                    decision = sensor_context_helpers.with_sensor_key(
                        make_baseline_irrigation_decision(state, pot, hour_weather, day_profile, slot),
                        pot,
                        sensor_context,
                    )
                    decision = valve_distribution.apply_cold_month_indoor_skip(decision, pot, current_date)
                    decision_by_pot_id[int(pot["id"])] = decision
                    if record_date:
                        decisions.append(decision)
                        daily_decisions += 1
                        hourly_decisions += 1

                    if decision["should_irrigate"] and valve_zones.is_valve_managed_pot(pot, current_date):
                        zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

            snapshot_label = metrics.daily_moisture_snapshot_label(current_date, observed_local, day_profile)
            if record_date and snapshot_label:
                metrics.record_daily_moisture_snapshot(daily_moisture_tracker, pot_states, snapshot_label)

            if slot is not None:
                for zone, trigger_decisions in zone_trigger_decisions.items():
                    trigger_pot_ids = valve_distribution.trigger_pot_ids(trigger_decisions)
                    trigger_sensor_ids = valve_distribution.trigger_sensor_ids(trigger_decisions)
                    trigger_pot_codes = valve_distribution.trigger_pot_codes(trigger_decisions)
                    zone_dose_factor = valve_distribution.baseline_zone_dose_factor(trigger_decisions)
                    execution_decisions = valve_distribution.zone_execution_decision_map(
                        decision_by_pot_id,
                        zone_pots,
                        zone,
                        current_date,
                        trigger_decisions,
                    )
                    zone_events = valve_distribution.execute_valve_zone_distribution(
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
                            alerts.append(
                                alert_row(
                                    pot,
                                    hour_weather,
                                    "too_wet_too_long",
                                    "warning",
                                    "Pot stayed above maximum moisture for 24 hours",
                                )
                            )
                            daily_alerts += 1
                            hourly_alerts += 1
                    else:
                        state.too_wet_hours = 0

            if record_date and metrics.uses_hourly_chart(start_date, end_date):
                detail_entries.append(
                    state_aggregates.hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        pot_states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        state_lookback.hourly_line_metadata(selected_sensor_context, current_date, observed_local, hour_weather),
                    )
                )

        if not record_date:
            current_date += timedelta(days=1)
            continue

        total_water_ml += daily_water_ml
        total_irrigation_events += daily_events
        total_irrigation_decisions += daily_decisions
        moisture_summary = metrics.daily_moisture_summary(daily_moisture_tracker, pot_states)

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
                "irrigated_pots": len(
                    {event["pot_id"] for event in events if event["date"] == current_date.isoformat()}
                ),
                "alerts": daily_alerts,
                "water_usage_ml": round(daily_water_ml, 2),
                "water_usage_l": round(daily_water_ml / 1000.0, 2),
                **state_lookback.daily_line_metadata(selected_sensor_context, current_date, day_weather),
            }
        )
        current_date += timedelta(days=1)

    valve_rollup = valve_rollups.apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

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
        "source": state_lookback.experiment_source(selected_sensor_context),
    }
    summary.update(sensor_context_helpers.sensor_control_summary_fields(pots, selected_sensor_context))
    if sensor_state_anchor is not None:
        summary["stateSensorAnchor"] = sensor_state_anchor
    summary.update(state_lookback.sensor_summary_fields(selected_sensor_context))
    chart_entries = results.chart_entries_for_range(start_date, end_date, entries, detail_entries)
    metrics.add_chart_summary(summary, chart_entries, start_date, end_date)

    return results.experiment_result(
        entries=entries,
        chart_entries=chart_entries,
        summary=summary,
        pots=pots,
        valve_rollup=valve_rollup,
        decisions=decisions,
        events=events,
        alerts=alerts,
        extra_fields={
            "stateAtExperimentStart": state_at_experiment_start or state_environment.serialize_pot_states(pot_states),
        },
    )

