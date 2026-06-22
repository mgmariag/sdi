from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.domain.pot import Pot
from digital_twin.domain.sensor import SensorSource
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_BASELINE_IRRIGATION_STEP,
    DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR,
    DEFAULT_IRRIGATION_REQUEST_BUILDER,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.environment import alert_row
from digital_twin.simulation.metrics import (
    add_chart_summary,
    daily_moisture_snapshot_label,
    daily_moisture_summary,
    new_daily_moisture_tracker,
    new_sampling_estimation_stats,
    record_daily_moisture_snapshot,
    record_sampling_estimation_error,
    sampling_estimation_summary,
    uses_hourly_chart,
)
from digital_twin.simulation.result_helpers import (
    chart_entries_for_range,
    daily_summary,
    experiment_result,
)
from digital_twin.simulation.shared.types import (
    ExperimentSnapshot,
    PotState,
)
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
from digital_twin.simulation.sensors.calibration import (
    apply_calibration_reading,
    apply_sensor_calibration_marker,
    forecast_sensor_reading_for_pot,
    sampling_calibration_at,
    sensor_date_is_future,
    sensor_reading_for_pot,
)
from digital_twin.simulation.sensors.context import (
    sensor_control_pots,
    sensor_control_summary_fields,
)
from digital_twin.simulation.valves.distribution import (
    execute_valve_zone_distribution,
    sparse_zone_dose_factor,
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


class SparseSamplingRunner:
    """Sparse sampling controller evaluated at default strategy decision slots."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        sample_interval_hours: int,
        persist: bool,
        selected_snapshot: ExperimentSnapshot,
        simulation_start_date: date,
        simulation_snapshot: ExperimentSnapshot,
        state_anchor_policy: str,
        warmup_reuse_policy: str,
        baseline_result: dict[str, Any] | None = None,
        state_environment: StateEnvironment | None = None,
    ) -> None:
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")

        self.start_date = start_date
        self.end_date = end_date
        self.sample_interval_hours = max(1, int(sample_interval_hours))
        self.persist = persist
        self.selected_snapshot = selected_snapshot
        self.simulation_start_date = simulation_start_date
        self.simulation_snapshot = simulation_snapshot
        self.state_anchor_policy = state_anchor_policy
        self.warmup_reuse_policy = warmup_reuse_policy
        self.baseline_result = baseline_result or {}
        self.state_environment = state_environment or StateEnvironment()
        self.state_projector = StateProjector(self.state_environment)
        self.baseline_valve_zone_executor = DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR
        baseline_start_states = self.baseline_result.get("stateAtExperimentStart")
        self.weather_rows = self.selected_snapshot.selected_weather_rows
        self.pots = self.simulation_snapshot.pots
        self.zone_pots = pots_by_valve_zone(self.pots)
        self.sensor_context = self.simulation_snapshot.sensor_context
        self.selected_sensor_context = self.selected_snapshot.sensor_context
        self.control_pots = sensor_control_pots(self.pots, self.sensor_context)
        self.control_pot_ids = {int(pot["id"]) for pot in self.control_pots}
        self.weather_by_day = self.simulation_snapshot.weather_by_day
        if baseline_start_states:
            self.states = self.state_environment.copy_pot_states_from_payload(
                baseline_start_states,
                self.simulation_snapshot.initial_pot_states,
            )
            self.probe_states = self.state_environment.copy_pot_states(self.states)
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
            self.states = self.state_environment.copy_pot_states(self.simulation_snapshot.initial_pot_states)
            self.probe_states = self.state_environment.copy_pot_states(self.simulation_snapshot.initial_pot_states)
            self.sensor_state_anchor = self.state_projector.initialize_from_first_day_sensor_readings(
                self.states,
                self.pots,
                self.sensor_context,
                self.simulation_start_date,
            )
            self.state_projector.initialize_from_first_day_sensor_readings(
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
        self.sampling_stats = new_sampling_estimation_stats()

    def run(self) -> dict[str, Any]:
        current_date = self.simulation_start_date
        while current_date <= self.end_date:
            self._run_day(current_date)
            current_date += timedelta(days=1)

        valve_rollup = apply_valve_rollup_to_entries(
            self.entries,
            self.detail_entries,
            self.pots,
            self.decisions,
            self.events,
        )
        summary = self._summary(valve_rollup)
        chart_entries = chart_entries_for_range(self.start_date, self.end_date, self.entries, self.detail_entries)
        add_chart_summary(summary, chart_entries, self.start_date, self.end_date)
        return experiment_result(
            entries=self.entries,
            chart_entries=chart_entries,
            summary=summary,
            pots=self.pots,
            valve_rollup=valve_rollup,
            decisions=self.decisions,
            events=self.events,
            alerts=self.alerts,
        )

    def _run_day(self, current_date: date) -> None:
        day_weather = self.weather_by_day.get(current_date, [])
        if not day_weather:
            return

        record_date = current_date >= self.start_date
        day_profile = self.simulation_snapshot.day_profiles.get(current_date) or self.state_environment.day_profile(
            current_date,
            day_weather,
            self.weather_by_day,
        )
        daily_water_ml = 0.0
        daily_events = 0
        daily_decisions = 0
        daily_alerts = 0
        daily_sensor_samples = 0
        daily_moisture_tracker = new_daily_moisture_tracker()
        record_hourly = record_date and uses_hourly_chart(self.start_date, self.end_date)

        for hour_weather in day_weather:
            observed_local = local_observed_at(hour_weather)
            slot = DEFAULT_IRRIGATION_POLICY.decision_slot(current_date, observed_local, day_profile)
            self._apply_hourly_environment(hour_weather, self.states)
            self._apply_hourly_environment(hour_weather, self.probe_states)
            self._calibrate_probe_states(current_date, observed_local, day_profile)

            hourly_water_ml = 0.0
            hourly_events = 0
            hourly_decisions = 0
            hourly_alerts = 0
            sample_now = False
            hourly_sensor_samples = 0

            if slot is not None:
                sample_now = record_date and (
                    sensor_date_is_future(self.sensor_context, current_date)
                    or self._should_sample(observed_local)
                )
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
                if sample_now and sensor_date_is_future(self.sensor_context, current_date):
                    self._sync_states_from_probe_reference()
            else:
                snapshot_label = daily_moisture_snapshot_label(current_date, observed_local, day_profile)
                if record_date and snapshot_label:
                    record_daily_moisture_snapshot(daily_moisture_tracker, self.states, snapshot_label)

            if record_hourly:
                self.detail_entries.append(
                    hourly_aggregate_entry(
                        observed_local,
                        hour_weather,
                        day_profile,
                        self.states,
                        hourly_water_ml,
                        hourly_events,
                        hourly_decisions,
                        hourly_alerts,
                        {
                            **hourly_line_metadata(self.selected_sensor_context, current_date, observed_local, hour_weather),
                            "sparse_sensor_sample": sample_now,
                            "sparse_sensor_samples": hourly_sensor_samples,
                        },
                        state_environment=self.state_environment,
                    )
                )
        if not record_date:
            return

        self.entries.append(
            daily_aggregate_entry(
                current_date,
                day_profile,
                self.states,
                daily_water_ml,
                daily_events,
                daily_decisions,
                daily_alerts,
                {
                    **daily_line_metadata(self.selected_sensor_context, current_date, day_weather),
                    "sparse_sensor_sample": daily_sensor_samples > 0,
                    "sparse_sensor_samples": daily_sensor_samples,
                },
                moisture_summary=daily_moisture_summary(daily_moisture_tracker, self.states),
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
        observed_local = local_observed_at(hour_weather)
        water_ml = 0.0
        events = 0
        decisions = 0
        alerts = 0
        decision_by_pot_id: dict[int, dict[str, Any]] = {}
        zone_trigger_decisions: dict[str, list[dict[str, Any]]] = {}

        sample_sensor_ids = (
            self._refresh_sample_states_from_sensor(current_date, observed_local, day_profile, sample_now)
            if calibrate_now
            else set()
        )

        for pot in self.control_pots:
            state = self.states[pot["id"]]

            if DEFAULT_IRRIGATION_POLICY.has_emergency_dryness(state, pot, current_date, observed_local) and record_date:
                self.alerts.append(alert_row(pot, hour_weather, "emergency_dryness", "warning", "Emergency dryness at sparse decision slot"))
                alerts += 1

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
                decisions += 1

            if decision["should_irrigate"] and is_valve_managed_pot(pot, current_date):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        snapshot_label = daily_moisture_snapshot_label(current_date, observed_local, day_profile)
        if record_date and snapshot_label:
            record_daily_moisture_snapshot(daily_moisture_tracker, self.states, snapshot_label)

        for zone, trigger_decisions in zone_trigger_decisions.items():
            trigger_pot_ids = collect_trigger_pot_ids(trigger_decisions)
            trigger_sensor_ids = collect_trigger_sensor_ids(trigger_decisions)
            trigger_pot_codes = collect_trigger_pot_codes(trigger_decisions)
            zone_dose_factor = sparse_zone_dose_factor(trigger_decisions, self.sample_interval_hours, sample_now)
            execution_decisions = zone_execution_decision_map(
                decision_by_pot_id,
                self.zone_pots,
                zone,
                current_date,
                trigger_decisions,
            )
            zone_events = execute_valve_zone_distribution(
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
                DEFAULT_IRRIGATION_REQUEST_BUILDER.build,
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

    def _refresh_sample_states_from_sensor(
        self,
        current_date: date,
        observed_local: datetime,
        day_profile: dict[str, Any],
        sample_now: bool,
    ) -> set[int]:
        sample_sensor_ids: set[int] = set()
        for pot in self.pots:
            sensor_id = self._refresh_state_from_sensor(
                self.states[pot["id"]],
                pot,
                current_date,
                observed_local,
                day_profile,
                record_stats=sample_now,
            )
            if sample_now and sensor_id is not None:
                sample_sensor_ids.add(sensor_id)
        return sample_sensor_ids

    def _refresh_state_from_sensor(
        self,
        state: PotState,
        pot: dict[str, Any],
        experiment_date: date,
        observed_at: datetime,
        day_profile: dict[str, Any],
        record_stats: bool = True,
    ) -> int | None:
        calibration_at = sampling_calibration_at(experiment_date, observed_at, day_profile)
        before = PotState(state.moisture)
        pot_id = int(pot["id"])
        association = (self.sensor_context.get("associations") or {}).get(pot_id) or {}
        reading = sensor_reading_for_pot(self.sensor_context, pot, experiment_date, calibration_at)
        if reading is None:
            reading = forecast_sensor_reading_for_pot(
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
        reading = apply_calibration_reading(state, reading, calibration_at)

        if record_stats:
            record_sampling_estimation_error(self.sampling_stats, before, state)
            self.sampling_stats["sensor_refreshes"] += 1
            if reading.get("source") == SensorSource.SPARSE_FORECAST.value:
                self.sampling_stats["forecast_refreshes"] += 1
        inferred = reading.get("association_source") == "associated_sensor"
        if record_stats and inferred:
            distance = soil.number(association.get("distance"), 1.0)
            self.sampling_stats["associated_refreshes"] += 1
            self.sampling_stats["association_distance_sum"] += distance
        elif record_stats:
            self.sampling_stats["direct_refreshes"] += 1
        return int(reading.get("associated_sensor_id") or reading.get("sensor_id") or pot["id"])

    def _apply_hourly_environment(
        self,
        hour_weather: dict[str, Any],
        states: dict[int, PotState],
    ) -> None:
        observed_local = local_observed_at(hour_weather)
        for pot in self.pots:
            self.state_environment.apply_hourly_environment(
                states[pot["id"]],
                pot,
                hour_weather,
                self.simulation_snapshot.day_profiles.get(observed_local.date(), {}),
                observed_local.date(),
                rain_exposure_factor=Pot.from_mapping(pot).rain_exposure_factor(observed_local.date()),
            )

    def _calibrate_probe_states(
        self,
        current_date: date,
        observed_local: datetime,
        day_profile: dict[str, Any],
    ) -> None:
        for pot in self.control_pots:
            apply_sensor_calibration_marker(
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
            if decision["should_irrigate"] and is_valve_managed_pot(pot, current_date):
                zone_trigger_decisions.setdefault(pot["balcony_zone"], []).append(decision)

        for zone, trigger_decisions in zone_trigger_decisions.items():
            self.baseline_valve_zone_executor.execute(
                self.probe_states,
                self.zone_pots,
                zone,
                current_date,
                hour_weather,
                decision_by_pot_id,
                trigger_decisions,
            )

    def _summary(self, valve_rollup: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        summary = daily_summary(
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
        sampling_estimation = sampling_estimation_summary(self.sampling_stats)
        summary.update(sampling_estimation)
        summary.update(sensor_control_summary_fields(self.pots, self.selected_sensor_context))
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
                "forecastSensorCalibrationPolicy": "sparse-sampling-syncs-to-default-strategy-at-forecast-decision-slots",
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

    def _sync_states_from_probe_reference(self) -> None:
        for pot_id, probe_state in self.probe_states.items():
            state = self.states.get(pot_id)
            if state is None:
                continue
            state.moisture = probe_state.moisture
            state.too_wet_hours = probe_state.too_wet_hours
