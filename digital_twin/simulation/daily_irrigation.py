from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import digital_twin.simulation.metrics as metrics
import digital_twin.simulation.result_helpers as results
from digital_twin.domain.pot import Pot
from digital_twin.domain.weather import local_observed_at
import digital_twin.simulation.state.aggregates as state_aggregates
import digital_twin.simulation.state.lookback as state_lookback
import digital_twin.simulation.sensors.calibration as sensor_calibration
import digital_twin.simulation.sensors.context as sensor_context_helpers
import digital_twin.simulation.valves.rollups as valve_rollups
import digital_twin.simulation.valves.zones as valve_zones
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_BASELINE_IRRIGATION_STEP,
    DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.environment import alert_row
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import ExperimentSnapshot
from digital_twin.simulation.state.environment import StateEnvironment
from digital_twin.simulation.state.projection import StateProjector

@dataclass
class _IrrigationCounters:
    water_ml: float = 0.0
    events: int = 0
    decisions: int = 0
    alerts: int = 0

    def add(self, other: "_IrrigationCounters") -> None:
        self.water_ml += other.water_ml
        self.events += other.events
        self.decisions += other.decisions
        self.alerts += other.alerts


class DefaultDailyIrrigationRunner:
    """Runs the default threshold/weather-rule controller over a simulation timeline."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        persist: bool,
        selected_snapshot: ExperimentSnapshot,
        simulation_start_date: date,
        simulation_snapshot: ExperimentSnapshot,
        state_anchor_policy: str,
        sensor_calibration_policy: str,
        sensor_calibration_marker=None,
        state_environment: StateEnvironment | None = None,
    ) -> None:
        _ = persist
        self.start_date = start_date
        self.end_date = end_date
        self.simulation_start_date = simulation_start_date
        self.simulation_snapshot = simulation_snapshot
        self.selected_snapshot = selected_snapshot
        self.state_anchor_policy = state_anchor_policy
        self.sensor_calibration_policy = sensor_calibration_policy
        self.sensor_calibration_marker = sensor_calibration_marker or sensor_calibration.apply_sensor_calibration_marker
        self.state_environment = state_environment or StateEnvironment()

        self.weather_rows = selected_snapshot.selected_weather_rows
        self.pots = simulation_snapshot.pots
        self.zone_pots = valve_zones.pots_by_valve_zone(self.pots)
        self.sensor_context = simulation_snapshot.sensor_context
        self.selected_sensor_context = selected_snapshot.sensor_context
        self.control_pots = sensor_context_helpers.sensor_control_pots(self.pots, self.sensor_context)
        self.control_pot_ids = {int(pot["id"]) for pot in self.control_pots}
        self.weather_by_day = simulation_snapshot.weather_by_day
        self.pot_states = self.state_environment.copy_pot_states(simulation_snapshot.initial_pot_states)
        self.state_projector = StateProjector(self.state_environment)
        self.sensor_state_anchor = self.state_projector.initialize_from_first_day_sensor_readings(
            self.pot_states,
            self.pots,
            self.sensor_context,
            simulation_start_date,
        )
        self.state_at_experiment_start: dict[str, dict[str, float | int]] | None = None

        self.entries: list[dict[str, Any]] = []
        self.detail_entries: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.totals = _IrrigationCounters()

    def run(self) -> dict[str, Any]:
        current_date = self.simulation_start_date
        while current_date <= self.end_date:
            self._capture_experiment_start_state(current_date)
            day_weather = self.weather_by_day.get(current_date, [])
            if day_weather:
                self._run_day(current_date, day_weather)
            current_date += timedelta(days=1)
        return self._result()

    def _capture_experiment_start_state(self, current_date: date) -> None:
        if current_date == self.start_date and self.state_at_experiment_start is None:
            self.state_at_experiment_start = self.state_environment.serialize_pot_states(self.pot_states)

    def _run_day(self, current_date: date, day_weather: list[dict[str, Any]]) -> None:
        record_date = current_date >= self.start_date
        day_profile = self._day_profile(current_date, day_weather)
        daily = _IrrigationCounters()
        daily_moisture_tracker = metrics.new_daily_moisture_tracker()

        for hour_weather in day_weather:
            hourly = self._run_hour(
                current_date,
                hour_weather,
                day_profile,
                daily_moisture_tracker,
                record_date,
            )
            daily.add(hourly)

        if not record_date:
            return

        self.totals.add(daily)
        self._record_day_entry(current_date, day_weather, day_profile, daily, daily_moisture_tracker)

    def _day_profile(self, current_date: date, day_weather: list[dict[str, Any]]) -> dict[str, Any]:
        return (
            self.simulation_snapshot.day_profiles.get(current_date)
            or self.state_environment.day_profile(current_date, day_weather, self.weather_by_day)
        )

    def _run_hour(
        self,
        current_date: date,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        daily_moisture_tracker,
        record_date: bool,
    ) -> _IrrigationCounters:
        observed_local = local_observed_at(hour_weather)
        slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current_date, observed_local, day_profile)
        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}
        hourly = _IrrigationCounters()

        for pot in self.pots:
            state = self.pot_states[pot["id"]]
            self._apply_environment_and_calibration(pot, state, hour_weather, day_profile, current_date, observed_local)
            if slot is None and record_date and self._has_emergency_dryness(pot, state, current_date, observed_local):
                self._append_alert(pot, hour_weather, "emergency_dryness", "Emergency dryness outside watering window")
                hourly.alerts += 1

        if slot is not None:
            hourly.add(
                self._make_baseline_decisions(
                    current_date,
                    hour_weather,
                    day_profile,
                    slot,
                    record_date,
                    decision_by_pot_id,
                    zone_trigger_decisions,
                )
            )

        snapshot_label = metrics.daily_moisture_snapshot_label(current_date, observed_local, day_profile)
        if record_date and snapshot_label:
            metrics.record_daily_moisture_snapshot(daily_moisture_tracker, self.pot_states, snapshot_label)

        if slot is not None:
            hourly.add(
                self._execute_valve_zones(
                    current_date,
                    hour_weather,
                    decision_by_pot_id,
                    zone_trigger_decisions,
                    record_date,
                )
            )
            hourly.alerts += self._record_too_wet_alerts(current_date, hour_weather, record_date)

        if record_date and metrics.uses_hourly_chart(self.start_date, self.end_date):
            self._record_hourly_entry(current_date, observed_local, hour_weather, day_profile, hourly)
        return hourly

    def _apply_environment_and_calibration(
        self,
        pot: dict[str, Any],
        state,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        current_date: date,
        observed_local: datetime,
    ) -> None:
        self.state_environment.apply_hourly_environment(
            state,
            pot,
            hour_weather,
            day_profile,
            observed_local.date(),
            rain_exposure_factor=Pot.from_mapping(pot).rain_exposure_factor(observed_local.date()),
        )
        if self.sensor_calibration_policy != "initial_state_only" and int(pot["id"]) in self.control_pot_ids:
            self.sensor_calibration_marker(
                state,
                pot,
                current_date,
                observed_local,
                self.sensor_context,
                day_profile,
            )

    def _has_emergency_dryness(self, pot: dict[str, Any], state, current_date: date, observed_local: datetime) -> bool:
        return (
            int(pot["id"]) in self.control_pot_ids
            and DEFAULT_IRRIGATION_POLICY.has_emergency_dryness(state, pot, current_date, observed_local)
        )

    def _make_baseline_decisions(
        self,
        current_date: date,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
        record_date: bool,
        decision_by_pot_id: dict[int, dict[str, Any]],
        zone_trigger_decisions: dict[str, list[dict[str, Any]]],
    ) -> _IrrigationCounters:
        counters = _IrrigationCounters()
        for pot in self.control_pots:
            state = self.pot_states[pot["id"]]
            decision = DEFAULT_BASELINE_IRRIGATION_STEP.make_decision(
                state,
                pot,
                hour_weather,
                day_profile,
                slot,
                self.sensor_context,
                current_date,
            )
            decision_by_pot_id[int(pot["id"])] = decision
            if record_date:
                self.decisions.append(decision)
                counters.decisions += 1
            if decision["should_irrigate"] and valve_zones.is_valve_managed_pot(pot, current_date):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)
        return counters

    def _execute_valve_zones(
        self,
        current_date: date,
        hour_weather: dict[str, Any],
        decision_by_pot_id: dict[int, dict[str, Any]],
        zone_trigger_decisions: dict[str, list[dict[str, Any]]],
        record_date: bool,
    ) -> _IrrigationCounters:
        counters = _IrrigationCounters()
        for zone, trigger_decisions in zone_trigger_decisions.items():
            zone_events = DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR.execute(
                self.pot_states,
                self.zone_pots,
                zone,
                current_date,
                hour_weather,
                decision_by_pot_id,
                trigger_decisions,
            )
            if not record_date:
                continue
            for event in zone_events:
                self.events.append(event)
                counters.events += 1
                counters.water_ml += event["planned_volume_ml"]
        return counters

    def _record_too_wet_alerts(self, current_date: date, hour_weather: dict[str, Any], record_date: bool) -> int:
        alert_count = 0
        for pot in self.pots:
            state = self.pot_states[pot["id"]]
            if state.moisture > pot["moisture_max_pct"]:
                state.too_wet_hours += 1
                if record_date and state.too_wet_hours == 24:
                    self._append_alert(pot, hour_weather, "too_wet_too_long", "Pot stayed above maximum moisture for 24 hours")
                    alert_count += 1
            else:
                state.too_wet_hours = 0
        return alert_count

    def _append_alert(self, pot: dict[str, Any], weather: dict[str, Any], alert_type: str, title: str) -> None:
        self.alerts.append(alert_row(pot, weather, alert_type, "warning", title))

    def _record_hourly_entry(
        self,
        current_date: date,
        observed_local: datetime,
        hour_weather: dict[str, Any],
        day_profile: dict[str, Any],
        hourly: _IrrigationCounters,
    ) -> None:
        self.detail_entries.append(
            state_aggregates.hourly_aggregate_entry(
                observed_local,
                hour_weather,
                day_profile,
                self.pot_states,
                hourly.water_ml,
                hourly.events,
                hourly.decisions,
                hourly.alerts,
                state_lookback.hourly_line_metadata(
                    self.selected_sensor_context,
                    current_date,
                    observed_local,
                    hour_weather,
                ),
                state_environment=self.state_environment,
            )
        )

    def _record_day_entry(
        self,
        current_date: date,
        day_weather: list[dict[str, Any]],
        day_profile: dict[str, Any],
        daily: _IrrigationCounters,
        daily_moisture_tracker,
    ) -> None:
        moisture_summary = metrics.daily_moisture_summary(daily_moisture_tracker, self.pot_states)
        self.entries.append(
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
                "irrigation_active": daily.events > 0,
                "irrigation_events": 1 if daily.events > 0 else 0,
                "valve_runs": daily.events,
                "irrigation_decisions": daily.decisions,
                "irrigated_pots": len(
                    {event["pot_id"] for event in self.events if event["date"] == current_date.isoformat()}
                ),
                "alerts": daily.alerts,
                "water_usage_ml": round(daily.water_ml, 2),
                "water_usage_l": round(daily.water_ml / 1000.0, 2),
                **state_lookback.daily_line_metadata(self.selected_sensor_context, current_date, day_weather),
            }
        )

    def _result(self) -> dict[str, Any]:
        valve_rollup = valve_rollups.apply_valve_rollup_to_entries(
            self.entries,
            self.detail_entries,
            self.pots,
            self.decisions,
            self.events,
        )
        summary = self._summary(valve_rollup)
        chart_entries = results.chart_entries_for_range(self.start_date, self.end_date, self.entries, self.detail_entries)
        metrics.add_chart_summary(summary, chart_entries, self.start_date, self.end_date)
        return results.experiment_result(
            entries=self.entries,
            chart_entries=chart_entries,
            summary=summary,
            pots=self.pots,
            valve_rollup=valve_rollup,
            decisions=self.decisions,
            events=self.events,
            alerts=self.alerts,
            extra_fields={
                "stateAtExperimentStart": self.state_at_experiment_start
                or self.state_environment.serialize_pot_states(self.pot_states),
            },
        )

    def _summary(self, valve_rollup: dict[str, Any]) -> dict[str, Any]:
        total_days = len(self.entries)
        summary = {
            "totalEntries": total_days,
            "daysAnalyzed": total_days,
            "potsAnalyzed": len(self.pots),
            "weatherRows": len(self.weather_rows),
            "irrigationEvents": sum(int(entry.get("irrigation_events") or 0) for entry in self.entries),
            "valveRuns": sum(int(entry.get("valve_runs", entry.get("irrigation_events", 0)) or 0) for entry in self.entries),
            "irrigationDecisions": len(valve_rollup["decisions"]),
            "potIrrigationDecisions": self.totals.decisions,
            "potIrrigationActions": len(self.events),
            "decisionLevel": "valve_zone",
            "totalWaterUsage": round(self.totals.water_ml / 1000.0, 2),
            "averageDailyWaterUsage": round((self.totals.water_ml / 1000.0) / max(total_days, 1), 2),
            "emergencyAlerts": len([alert for alert in self.alerts if alert["alert_type"] == "emergency_dryness"]),
            "wetAlerts": len([alert for alert in self.alerts if alert["alert_type"] == "too_wet_too_long"]),
            "startDate": self.start_date.isoformat(),
            "endDate": self.end_date.isoformat(),
            "stateSimulationStartDate": self.simulation_start_date.isoformat(),
            "stateLookbackDays": (self.end_date - self.simulation_start_date).days + 1,
            "stateAnchorPolicy": self.state_anchor_policy,
            "baselineSensorCalibrationPolicy": self.sensor_calibration_policy,
            "source": state_lookback.experiment_source(self.selected_sensor_context),
        }
        summary.update(sensor_context_helpers.sensor_control_summary_fields(self.pots, self.selected_sensor_context))
        if self.sensor_state_anchor is not None:
            summary["stateSensorAnchor"] = self.sensor_state_anchor
        summary.update(state_lookback.sensor_summary_fields(self.selected_sensor_context))
        return summary

