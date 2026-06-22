from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import digital_twin.simulation.anfis.modeling as modeling
import digital_twin.simulation.anfis.controller as anfis_controller
import digital_twin.simulation.metrics as metrics
import digital_twin.simulation.result_helpers as results
from digital_twin.domain.pot import Pot
from digital_twin.domain.weather import local_observed_at
import digital_twin.simulation.state.aggregates as state_aggregates
import digital_twin.simulation.state.lookback as state_lookback
import digital_twin.simulation.sensors.context as sensor_context_helpers
import digital_twin.simulation.valves.distribution as valve_distribution
import digital_twin.simulation.valves.rollups as valve_rollups
import digital_twin.simulation.valves.zones as valve_zones
from digital_twin.simulation.anfis.model import ANFIS, probability_category
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_IRRIGATION_REQUEST_BUILDER,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.environment import alert_row
from digital_twin.simulation.shared.constants import (
    ANFIS_DECISION_THRESHOLD,
    ANFIS_FORECAST_DECISION_THRESHOLD,
)
from digital_twin.simulation.shared.types import ExperimentSnapshot
from digital_twin.simulation.state.environment import StateEnvironment
from digital_twin.simulation.state.projection import StateProjector

ANFIS_HARD_STOP_REASON_CODES = {
    "freeze_risk",
    "winter_indoor_not_valve_managed",
    "anfis_cold_skip",
}


class AnfisDailyIrrigationRunner:
    """Runs the run anfis daily irrigation workflow."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        model: ANFIS | anfis_controller.AnfisModelController,
        persist: bool,
        selected_snapshot: ExperimentSnapshot,
        decision_threshold: float = ANFIS_DECISION_THRESHOLD,
        forecast_decision_threshold: float = ANFIS_FORECAST_DECISION_THRESHOLD,
        state_environment: StateEnvironment | None = None,
        feature_builder: modeling.AnfisFeatureBuilder | None = None,
        model_evaluator: modeling.AnfisModelEvaluator | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.model = model
        self.persist = persist
        self.selected_snapshot = selected_snapshot
        self.decision_threshold = decision_threshold
        self.forecast_decision_threshold = forecast_decision_threshold
        self.state_environment = state_environment
        self.feature_builder = feature_builder or modeling.DEFAULT_ANFIS_FEATURE_BUILDER
        self.model_evaluator = model_evaluator or modeling.DEFAULT_ANFIS_MODEL_EVALUATOR

    def run(self) -> dict[str, Any]:
        start_date = self.start_date
        end_date = self.end_date
        model = self.model
        persist = self.persist
        selected_snapshot = self.selected_snapshot
        decision_threshold = self.decision_threshold
        forecast_decision_threshold = self.forecast_decision_threshold
        state_environment = self.state_environment
        feature_builder = self.feature_builder
        model_evaluator = self.model_evaluator
        _ = persist
        state_environment = state_environment or StateEnvironment()
        simulation_start_date = start_date
        simulation_snapshot = selected_snapshot

        weather_rows = selected_snapshot.selected_weather_rows
        pots = simulation_snapshot.pots
        zone_pots = valve_zones.pots_by_valve_zone(pots)
        sensor_context = simulation_snapshot.sensor_context
        selected_sensor_context = selected_snapshot.sensor_context
        control_pots = sensor_context_helpers.sensor_control_pots(pots, sensor_context)
        control_pot_ids = {int(pot["id"]) for pot in control_pots}
        weather_by_day = simulation_snapshot.weather_by_day
        pot_states = state_environment.copy_pot_states(simulation_snapshot.initial_pot_states)
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
        state_projector = StateProjector(state_environment)
        sensor_state_anchor = state_projector.initialize_from_first_day_sensor_readings(
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
            day_profile = simulation_snapshot.day_profiles.get(current_date) or state_environment.day_profile(current_date, day_weather, weather_by_day)
            daily_water_ml = 0.0
            daily_events = 0
            daily_decisions = 0
            daily_alerts = 0
            daily_moisture_tracker = metrics.new_daily_moisture_tracker()
            probability_sum = 0.0
            probability_count = 0
            probability_max = 0.0
            zone_signal_sum = 0.0
            zone_signal_count = 0
            zone_signal_max = 0.0
            current_decision_threshold = feature_builder.decision_threshold(
                sensor_context,
                current_date,
                decision_threshold,
                forecast_decision_threshold,
            )

            for hour_weather in day_weather:
                observed_local = local_observed_at(hour_weather)
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
                    state_environment.apply_hourly_environment(
                        state,
                        pot,
                        hour_weather,
                        day_profile,
                        observed_local.date(),
                        rain_exposure_factor=Pot.from_mapping(pot).rain_exposure_factor(observed_local.date()),
                    )

                    if slot is None:
                        if (
                            int(pot["id"]) in control_pot_ids
                            and DEFAULT_IRRIGATION_POLICY.has_emergency_dryness(state, pot, current_date, observed_local)
                        ):
                            if record_date:
                                alerts.append(alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness outside watering window"))
                                daily_alerts += 1
                                hourly_alerts += 1
                        previous_moisture_by_pot[pot_id] = float(state.moisture)
                        continue
                    previous_moisture_by_pot[pot_id] = float(state.moisture)

                if slot is not None:
                    for pot in control_pots:
                        state = pot_states[pot["id"]]
                        prior_moisture = prior_moisture_by_pot.get(int(pot["id"]))
                        anfis_input = feature_builder.inputs(
                            state,
                            hour_weather,
                            None,
                            pot,
                            day_profile,
                            prior_moisture_pct=prior_moisture,
                        )
                        rule_decision = feature_builder.execution_decision(state, pot, hour_weather, day_profile, slot)
                        predicted_probability = model_evaluator.predict_probability(model, anfis_input, pot.get("balcony_zone"))
                        probability_sum += predicted_probability
                        probability_count += 1
                        probability_max = max(probability_max, predicted_probability)
                        hourly_probability_sum += predicted_probability
                        hourly_probability_count += 1
                        hourly_probability_max = max(hourly_probability_max, predicted_probability)

                        valve_managed = valve_zones.is_valve_managed_pot(pot, current_date)

                        decision = dict(rule_decision)
                        decision = sensor_context_helpers.with_sensor_key(decision, pot, sensor_context)
                        decision["should_irrigate"] = False
                        decision["predicted_probability"] = round(predicted_probability, 4)
                        decision["anfis_decision_threshold"] = current_decision_threshold
                        decision["predicted_category"] = probability_category(predicted_probability)
                        decision["simulated_moisture_pct"] = decision["current_moisture_pct"]
                        decision["current_moisture_pct"] = round(anfis_input["moisture"], 2)
                        decision["anfis_input_moisture_pct"] = round(anfis_input["moisture"], 2)
                        decision["anfis_input_temperature_c"] = round(anfis_input["temperature"], 2)
                        decision["anfis_input_rain_mm"] = round(anfis_input["rain"], 2)
                        decision = valve_distribution.apply_cold_month_indoor_skip(decision, pot, current_date)
                        hard_stop = decision["reason_code"] in ANFIS_HARD_STOP_REASON_CODES
                        below_target = state.moisture < decision["target_moisture_pct"]
                        safety_threshold = DEFAULT_IRRIGATION_POLICY.safety_threshold(pot, day_profile, slot)
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

                snapshot_label = metrics.daily_moisture_snapshot_label(current_date, observed_local, day_profile)
                if record_date and snapshot_label:
                    metrics.record_daily_moisture_snapshot(daily_moisture_tracker, pot_states, snapshot_label)

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
                        zone: feature_builder.zone_probability_summary(zone_decisions)
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
                                    f"{feature_builder.duration_policy_note(decision)}"
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
                                    f"{feature_builder.duration_policy_note(decision)}"
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

                    for zone, trigger_decisions in zone_trigger_decisions.items():
                        trigger_pot_ids = valve_distribution.trigger_pot_ids(trigger_decisions)
                        trigger_sensor_ids = valve_distribution.trigger_sensor_ids(trigger_decisions)
                        trigger_pot_codes = valve_distribution.trigger_pot_codes(trigger_decisions)
                        zone_activation_probability = zone_probability_summary.get(zone, (slot_average_probability, slot_max_probability))[0]
                        zone_dose_factor = 1.0
                        minimum_runtime_min = 0.0
                        for decision in trigger_decisions:
                            decision["dose_factor"] = zone_dose_factor
                            decision["dose_policy_source"] = "anfis_full_dose_policy"
                            decision["reason_detail"] = (
                                f"{decision['reason_detail']} Final ANFIS zone dose is "
                                f"{zone_dose_factor * 100.0:.0f}% of calculated need."
                            )

                        def anfis_zone_decision(zone_pot: dict[str, Any], zone_decision: dict[str, Any]) -> dict[str, Any]:
                            zone_decision["should_irrigate"] = True
                            zone_decision["dose_factor"] = zone_dose_factor
                            zone_decision["dose_policy_source"] = "anfis_full_dose_policy"
                            zone_state = pot_states[zone_pot["id"]]
                            zone_decision["full_dose_start_moisture_pct"] = round(zone_state.moisture, 2)
                            return zone_decision

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
                            anfis_zone_decision,
                            DEFAULT_IRRIGATION_REQUEST_BUILDER.build,
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
                                "dose_policy_source": "anfis_full_dose_policy",
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
                                alerts.append(alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                                daily_alerts += 1
                                hourly_alerts += 1
                        else:
                            state.too_wet_hours = 0

                if record_date and metrics.uses_hourly_chart(start_date, end_date):
                    hourly_probability = hourly_probability_sum / hourly_probability_count if hourly_probability_count else 0.0
                    hourly_zone_probability = (
                        hourly_zone_signal_sum / hourly_zone_signal_count
                        if hourly_zone_signal_count
                        else hourly_probability
                    )
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
                            {
                                **state_lookback.hourly_line_metadata(selected_sensor_context, current_date, observed_local, hour_weather),
                                "predicted_probability": round(hourly_zone_probability, 4),
                                "predicted_probability_percent": round(hourly_zone_probability * 100.0, 2),
                                "trigger_probability": round(hourly_zone_signal_max or hourly_probability_max, 4),
                                "trigger_probability_percent": round((hourly_zone_signal_max or hourly_probability_max) * 100.0, 2),
                                "global_predicted_probability": round(hourly_probability, 4),
                                "global_predicted_probability_percent": round(hourly_probability * 100.0, 2),
                                "anfis_decision_threshold": round(current_decision_threshold, 4),
                                "anfis_decision_threshold_percent": round(current_decision_threshold * 100.0, 2),
                            },
                            state_environment=state_environment,
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
            moisture_summary = metrics.daily_moisture_summary(daily_moisture_tracker, pot_states)
            entries.append(
                state_aggregates.daily_aggregate_entry(
                    current_date,
                    day_profile,
                    pot_states,
                    daily_water_ml,
                    daily_events,
                    daily_decisions,
                    daily_alerts,
                    {
                        **state_lookback.daily_line_metadata(selected_sensor_context, current_date, day_weather),
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

        valve_rollup = valve_rollups.apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

        summary = results.daily_summary(
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
        summary.update(sensor_context_helpers.sensor_control_summary_fields(pots, selected_sensor_context))
        if sensor_state_anchor is not None:
            summary["anfisSensorStateAnchor"] = sensor_state_anchor
        summary["potIrrigationDecisions"] = total_irrigation_decisions
        summary["potIrrigationActions"] = len(events)
        summary["decisionLevel"] = "valve_zone"
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
        )

