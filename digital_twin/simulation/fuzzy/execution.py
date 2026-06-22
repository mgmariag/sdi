from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from digital_twin.domain.pot import Pot
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_FUZZY_POLICY,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.environment import alert_row
from digital_twin.simulation.metrics import (
    add_chart_summary,
    uses_hourly_chart,
)
from digital_twin.simulation.result_helpers import (
    chart_entries_for_range,
    daily_summary,
    experiment_result,
)
from digital_twin.simulation.shared.types import ExperimentSnapshot
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at
from digital_twin.simulation.state.aggregates import (
    daily_aggregate_entry,
    hourly_aggregate_entry,
)
from digital_twin.simulation.state.environment import StateEnvironment
from digital_twin.simulation.state.lookback import (
    daily_line_metadata,
    hourly_line_metadata,
)
from digital_twin.simulation.state.projection import StateProjector
from digital_twin.simulation.sensors.context import (
    sensor_control_pots,
    sensor_control_summary_fields,
    with_sensor_key,
)
from digital_twin.simulation.valves.distribution import (
    apply_cold_month_indoor_skip,
    execute_valve_zone_distribution,
    trigger_pot_codes as collect_trigger_pot_codes,
    trigger_pot_ids as collect_trigger_pot_ids,
    trigger_sensor_ids as collect_trigger_sensor_ids,
    zone_execution_decision_map,
)
from digital_twin.simulation.valves.rollups import apply_valve_rollup_to_entries
from digital_twin.simulation.valves.zones import (
    is_valve_managed_pot,
    pots_by_valve_zone,
)


@dataclass(frozen=True)
class FuzzyActuationPolicy:
    minimum_runtime_min: float = 2.0
    safety_minimum_runtime_min: float = 0.5

    def has_safety_need(self, trigger_decisions: list[dict[str, Any]]) -> bool:
        return any(
            float(decision.get("current_moisture_pct") or 0.0)
            <= float(decision.get("fuzzy_safety_floor_pct") or decision.get("target_moisture_pct") or 0.0)
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
    ) -> bool:
        return (
            slot == "evening"
            and (
                bool(day_profile.get("heatwave_day"))
                or soil.number(day_profile.get("max_temperature_c"), 20.0) >= 35.0
            )
        )
    def summary(self) -> dict[str, Any]:
        return {
            "minimum_valve_runtime_min": self.minimum_runtime_min,
            "safety_minimum_valve_runtime_min": self.safety_minimum_runtime_min,
            "daily_execution_policy": "one_daily_volume_prescription_plus_heatwave_evening_supplement",
            "heatwave_supplement_policy": "hot_evening_for_triggered_zone",
            "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
        }


FUZZY_ACTUATION_POLICY = FuzzyActuationPolicy()


class FuzzyDailyIrrigationRunner:
    """Runs the run fuzzy dt daily irrigation workflow."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        persist: bool,
        selected_snapshot: ExperimentSnapshot,
        state_environment: StateEnvironment | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.persist = persist
        self.selected_snapshot = selected_snapshot
        self.state_environment = state_environment

    def run(self) -> dict[str, Any]:
        start_date = self.start_date
        end_date = self.end_date
        persist = self.persist
        selected_snapshot = self.selected_snapshot
        state_environment = self.state_environment
        _ = persist
        state_environment = state_environment or StateEnvironment()
        simulation_start_date = start_date
        simulation_snapshot = selected_snapshot
        weather_rows = selected_snapshot.selected_weather_rows
        pots = simulation_snapshot.pots
        zone_pots = pots_by_valve_zone(pots)
        sensor_context = simulation_snapshot.sensor_context
        selected_sensor_context = selected_snapshot.sensor_context
        control_pots = sensor_control_pots(pots, sensor_context)
        control_pot_ids = {int(pot["id"]) for pot in control_pots}
        weather_by_day = simulation_snapshot.weather_by_day
        pot_states = state_environment.copy_pot_states(simulation_snapshot.initial_pot_states)
        state_projector = StateProjector(state_environment)
        sensor_state_anchor = state_projector.initialize_from_first_day_sensor_readings(
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
        prescription_volume_ml_sum = 0.0
        prescription_score_sum = 0.0
        prescription_count = 0

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
            daily_prescription_volume_ml_sum = 0.0
            daily_prescription_score_sum = 0.0
            daily_prescription_count = 0
            daily_prescribed_zones: set[str] = set()

            for hour_weather in day_weather:
                observed_local = local_observed_at(hour_weather)
                hourly_water_ml = 0.0
                hourly_events = 0
                hourly_decisions = 0
                hourly_alerts = 0
                hourly_prescription_volume_ml_sum = 0.0
                hourly_prescription_score_sum = 0.0
                hourly_prescription_count = 0
                slot = DEFAULT_FUZZY_POLICY.decision_slot(current_date, observed_local, day_profile)
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
                if slot is not None:
                    for pot in control_pots:
                        state = pot_states[pot["id"]]
                        decision = DEFAULT_FUZZY_POLICY.make_decision(state, pot, hour_weather, day_profile, slot)
                        decision = with_sensor_key(decision, pot, sensor_context)
                        decision = apply_cold_month_indoor_skip(decision, pot, current_date)
                        decision_by_pot_id[int(pot["id"])] = decision

                        prescription_volume_ml = float(decision.get("prescription_volume_ml") or 0.0)
                        prescription_score_pct = float(decision.get("prescription_score_pct") or 0.0)
                        if record_date:
                            decisions.append(decision)
                            daily_decisions += 1
                            hourly_decisions += 1
                            total_irrigation_decisions += 1
                            prescription_volume_ml_sum += prescription_volume_ml
                            prescription_score_sum += prescription_score_pct
                            prescription_count += 1
                            daily_prescription_volume_ml_sum += prescription_volume_ml
                            daily_prescription_score_sum += prescription_score_pct
                            daily_prescription_count += 1
                            hourly_prescription_volume_ml_sum += prescription_volume_ml
                            hourly_prescription_score_sum += prescription_score_pct
                            hourly_prescription_count += 1

                        if decision["should_irrigate"] and is_valve_managed_pot(pot, current_date):
                            zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

                if slot is not None:
                    for zone, trigger_decisions in zone_trigger_decisions.items():
                        if (
                            zone in daily_prescribed_zones
                            and not FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                                slot,
                                day_profile,
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

                        trigger_pot_ids = collect_trigger_pot_ids(trigger_decisions)
                        trigger_sensor_ids = collect_trigger_sensor_ids(trigger_decisions)
                        trigger_pot_codes = collect_trigger_pot_codes(trigger_decisions)
                        trigger_prescription_volumes_ml = [
                            float(decision.get("prescription_volume_ml") or 0.0)
                            for decision in trigger_decisions
                        ]
                        trigger_prescription_scores = [
                            float(decision.get("prescription_score_pct") or 0.0)
                            for decision in trigger_decisions
                        ]
                        zone_max_prescription_volume_ml = max(trigger_prescription_volumes_ml, default=0.0)
                        zone_average_prescription_volume_ml = (
                            sum(trigger_prescription_volumes_ml) / max(len(trigger_prescription_volumes_ml), 1)
                        )
                        zone_max_prescription_score_pct = max(trigger_prescription_scores, default=0.0)
                        zone_average_prescription_score_pct = (
                            sum(trigger_prescription_scores) / max(len(trigger_prescription_scores), 1)
                        )
                        execution_decisions = zone_execution_decision_map(
                            decision_by_pot_id,
                            zone_pots,
                            zone,
                            current_date,
                            trigger_decisions,
                        )

                        def fuzzy_zone_decision(zone_pot: dict[str, Any], zone_decision: dict[str, Any]) -> dict[str, Any]:
                            zone_state = pot_states[int(zone_pot["id"])]
                            prescription_score_pct = float(
                                zone_decision.get("prescription_score_pct")
                                or zone_average_prescription_score_pct
                            )
                            planned_volume_ml = DEFAULT_FUZZY_POLICY.prescribed_volume_ml(
                                zone_state,
                                zone_pot,
                                prescription_score_pct,
                            )
                            zone_decision["prescription_score_pct"] = round(prescription_score_pct, 2)
                            zone_decision["prescription_volume_ml"] = round(planned_volume_ml, 2)
                            zone_decision["planned_volume_ml"] = round(planned_volume_ml, 2)
                            if zone_decision.get("should_irrigate"):
                                zone_decision["reason_code"] = "valve_zone_prescription"
                                zone_decision["reason_detail"] = (
                                    f"Valve zone {zone} is triggered by {len(trigger_pot_ids)} pot(s); "
                                    "the fuzzy volume request is applied as a valve-zone runtime budget."
                                )
                            else:
                                zone_decision["reason_code"] = "valve_zone_passive_delivery"
                                zone_decision["reason_detail"] = (
                                    f"Valve zone {zone} is triggered by another pot; this pot keeps its own "
                                    "zone-scaled fuzzy volume request while the valve is open."
                                )
                            return zone_decision

                        zone_events = execute_valve_zone_distribution(
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
                                "zone_prescription_volume_l": round(zone_average_prescription_volume_ml / 1000.0, 3),
                                "zone_max_prescription_volume_l": round(zone_max_prescription_volume_ml / 1000.0, 3),
                                "zone_prescription_score_pct": round(zone_average_prescription_score_pct, 2),
                                "zone_max_prescription_score_pct": round(zone_max_prescription_score_pct, 2),
                                "zone": zone,
                                "zone_activation_policy": "sensor_pot_trigger",
                                "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
                                "zone_daily_execution_policy": (
                                    "heatwave_evening_supplement"
                                    if zone in daily_prescribed_zones and slot == "evening"
                                    else "daily_zone_volume_prescription"
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
                                alerts.append(alert_row(pot, hour_weather, "too_wet_too_long", "warning", "Pot stayed above maximum moisture for 24 hours"))
                                daily_alerts += 1
                                hourly_alerts += 1
                        else:
                            state.too_wet_hours = 0

                if record_date and uses_hourly_chart(start_date, end_date):
                    hourly_avg_prescription_volume_l = (
                        round(hourly_prescription_volume_ml_sum / hourly_prescription_count / 1000.0, 3)
                        if hourly_prescription_count > 0
                        else None
                    )
                    hourly_avg_prescription_score_pct = (
                        round(hourly_prescription_score_sum / hourly_prescription_count, 2)
                        if hourly_prescription_count > 0
                        else None
                    )
                    detail_entries.append(
                        hourly_aggregate_entry(
                            observed_local,
                            hour_weather,
                            day_profile,
                            pot_states,
                            hourly_water_ml,
                            hourly_events,
                            hourly_decisions,
                            hourly_alerts,
                            {
                                **hourly_line_metadata(selected_sensor_context, current_date, observed_local, hour_weather),
                                "fuzzy_prescription_volume_l": hourly_avg_prescription_volume_l,
                                "avg_prescription_volume_l": hourly_avg_prescription_volume_l,
                                "fuzzy_prescription_score_pct": hourly_avg_prescription_score_pct,
                            },
                            state_environment=state_environment,
                        )
                    )

            if not record_date:
                current_date += timedelta(days=1)
                continue

            daily_avg_prescription_volume_l = (
                round(daily_prescription_volume_ml_sum / daily_prescription_count / 1000.0, 3)
                if daily_prescription_count > 0
                else None
            )
            daily_avg_prescription_score_pct = (
                round(daily_prescription_score_sum / daily_prescription_count, 2)
                if daily_prescription_count > 0
                else None
            )
            entries.append(
                daily_aggregate_entry(
                    current_date,
                    day_profile,
                    pot_states,
                    daily_water_ml,
                    daily_events,
                    daily_decisions,
                    daily_alerts,
                    {
                        **daily_line_metadata(selected_sensor_context, current_date, day_weather),
                        "fuzzy_prescription_volume_l": daily_avg_prescription_volume_l,
                        "avg_prescription_volume_l": daily_avg_prescription_volume_l,
                        "fuzzy_prescription_score_pct": daily_avg_prescription_score_pct,
                    },
                )
            )
            total_water_ml += daily_water_ml
            current_date += timedelta(days=1)

        valve_rollup = apply_valve_rollup_to_entries(entries, detail_entries, pots, decisions, events)

        summary = daily_summary(
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
        summary["averagePrescriptionVolumeL"] = round(prescription_volume_ml_sum / max(prescription_count, 1) / 1000.0, 3)
        summary["averagePrescriptionScorePct"] = round(prescription_score_sum / max(prescription_count, 1), 2)
        summary["fuzzyDataPolicy"] = "daily-fis-volume-prescription"
        summary["fuzzyControllerPolicy"] = "fuzzy_dt_volume_control"
        summary["fuzzyDecisionPolicy"] = "daily_zone_volume_prescription_with_heatwave_evening_supplement"
        summary["fuzzyActuationPolicy"] = FUZZY_ACTUATION_POLICY.summary()
        summary["fuzzySensorCalibrationPolicy"] = "initial_state_only"
        summary["stateSimulationStartDate"] = simulation_start_date.isoformat()
        summary["stateLookbackDays"] = (end_date - simulation_start_date).days + 1
        summary["stateAnchorPolicy"] = "experiment_start_sensor_anchor"
        summary.update(sensor_control_summary_fields(pots, selected_sensor_context))
        if sensor_state_anchor is not None:
            summary["stateSensorAnchor"] = sensor_state_anchor
        chart_entries = chart_entries_for_range(start_date, end_date, entries, detail_entries)
        add_chart_summary(summary, chart_entries, start_date, end_date)
        return experiment_result(
            entries=entries,
            chart_entries=chart_entries,
            summary=summary,
            pots=pots,
            valve_rollup=valve_rollup,
            decisions=decisions,
            events=events,
            alerts=alerts,
        )
