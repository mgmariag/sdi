from __future__ import annotations

import time as perf_time
from datetime import date, datetime
from typing import Any

import digital_twin.simulation.anfis.experiment as anfis_experiment
import digital_twin.simulation.state.window as state_window
from digital_twin.application.control_loop.snapshots import (
    load_experiment_snapshot as load_control_loop_snapshot,
)
from digital_twin.simulation.anfis.execution import run_anfis_daily_irrigation
from digital_twin.simulation.anfis.model import ANFIS
from digital_twin.simulation.anfis.modeling import AnfisModelController
from digital_twin.simulation.baseline.execution import run_default_daily_irrigation
from digital_twin.simulation.experiment_comparison import ExperimentComparison
from digital_twin.simulation.fuzzy.execution import (
    FUZZY_ACTUATION_POLICY,
    run_fuzzy_dt_daily_irrigation,
)
from digital_twin.simulation.metrics import (
    fuzzy_comfort_threshold_pct,
    moisture_safe_savings_metrics,
    sampling_moisture_chart_summary,
    uses_hourly_chart,
)
from digital_twin.simulation.sampling.execution import run_sparse_daily_irrigation
from digital_twin.simulation.shared.constants import (
    ANFIS_DECISION_THRESHOLD,
    ANFIS_FORECAST_DECISION_THRESHOLD,
    LOCAL_TZ,
)
from digital_twin.simulation.shared.types import ExperimentSnapshot
from digital_twin.simulation.state.sensor_context import sensor_control_pots


def load_experiment_snapshot(start_date: date, end_date: date) -> ExperimentSnapshot:
    return load_control_loop_snapshot(start_date, end_date)


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
    training_start = min(start_date, state_simulation_start(end_date, end_date))
    try:
        return load_experiment_snapshot(training_start, end_date)
    except ValueError:
        return fallback_snapshot


def state_simulation_start(start_date: date, end_date: date) -> date:
    return state_window.state_simulation_start(start_date, end_date, historical_state_anchor_date)


def _active_season_start_date(day: date) -> date | None:
    return state_window.active_season_start_date(day)


def historical_state_anchor_date(end_date: date) -> date | None:
    return state_window.historical_state_anchor_date(end_date)


def resolve_simulation_snapshot(
    start_date: date,
    end_date: date,
    selected_snapshot: ExperimentSnapshot,
) -> tuple[date, ExperimentSnapshot]:
    simulation_start_date = state_simulation_start(start_date, end_date)
    if simulation_start_date >= start_date:
        return start_date, selected_snapshot

    try:
        return simulation_start_date, load_experiment_snapshot(simulation_start_date, end_date)
    except ValueError:
        return start_date, selected_snapshot


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
    """Run the database-backed default DT irrigation control strategy."""
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    if state_anchor_policy == "experiment_start_sensor_anchor":
        simulation_start_date = start_date
        simulation_snapshot = selected_snapshot
    else:
        simulation_start_date, simulation_snapshot = resolve_simulation_snapshot(start_date, end_date, selected_snapshot)

    return run_default_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        persist=persist,
        selected_snapshot=selected_snapshot,
        simulation_start_date=simulation_start_date,
        simulation_snapshot=simulation_snapshot,
        state_anchor_policy=state_anchor_policy,
        sensor_calibration_policy=sensor_calibration_policy,
    )

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
    sparse = run_sparse_daily_irrigation_with_snapshot(
        start_date=start_date,
        end_date=end_date,
        sample_interval_hours=sample_interval_hours,
        persist=persist,
        snapshot=snapshot,
        baseline_result=baseline,
    )

    comparison = ExperimentComparison(start_date, end_date, snapshot, baseline, sparse, "sparse", "sparse_water_usage_l")

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

    table_moisture_display = sampling_moisture_chart_summary(entries, sample_interval_hours, hourly=False)
    chart_moisture_display = sampling_moisture_chart_summary(
        chart_entries,
        sample_interval_hours,
        hourly=uses_hourly_chart(start_date, end_date) and bool(chart_entries),
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
    fuzzy = run_fuzzy_dt_daily_irrigation_with_snapshot(
        start_date=start_date,
        end_date=end_date,
        persist=persist,
        snapshot=snapshot,
    )

    comparison = ExperimentComparison(start_date, end_date, snapshot, baseline, fuzzy, "fuzzy", "fuzzy_water_usage_l")

    def fuzzy_row(baseline_entry: dict[str, Any], fuzzy_entry: dict[str, Any]) -> dict[str, Any]:
        prescription_volume_l = fuzzy_entry.get(
            "fuzzy_prescription_volume_l",
            fuzzy_entry.get("avg_prescription_volume_l", 0.0),
        )
        return comparison.row(
            baseline_entry,
            fuzzy_entry,
            {
                "fuzzy_prescription_volume_l": prescription_volume_l,
                "avg_prescription_volume_l": fuzzy_entry.get("avg_prescription_volume_l", prescription_volume_l),
                "fuzzy_prescription_score_pct": fuzzy_entry.get("fuzzy_prescription_score_pct"),
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
    control_pots = sensor_control_pots(snapshot.pots, snapshot.sensor_context)
    baseline_water = float(totals["baseline_total_water_usage_l"])
    fuzzy_water = float(totals["fuzzy_total_water_usage_l"])
    water_savings_l = baseline_water - fuzzy_water
    water_savings_percent = round((water_savings_l / baseline_water) * 100.0, 2) if baseline_water > 0 else 0.0
    comfort_metrics = moisture_safe_savings_metrics(
        entries,
        "fuzzy",
        control_pots,
        water_savings_percent,
        comfort_threshold_pct=fuzzy_comfort_threshold_pct(control_pots),
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
        "average_prescription_volume_l": fuzzy_summary.get("averagePrescriptionVolumeL", 0.0),
        "average_prescription_score_pct": fuzzy_summary.get("averagePrescriptionScorePct", 0.0),
        "fuzzyDataPolicy": fuzzy_summary.get("fuzzyDataPolicy", "daily-fis-volume-prescription"),
        "fuzzyControllerPolicy": fuzzy_summary.get("fuzzyControllerPolicy", "fuzzy_dt_volume_control"),
        "fuzzyDecisionPolicy": fuzzy_summary.get("fuzzyDecisionPolicy", "daily_zone_volume_prescription_control"),
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


def run_daily_anfis_experiment(
    start_date: date,
    end_date: date,
    seed: int | None = 2026,
    generations: int = 35,
    population: int = 24,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    baseline_result: dict[str, Any] | None = None,
    trained_model: AnfisModelController | None = None,
    training_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    return anfis_experiment.run_daily_anfis_experiment(
        start_date=start_date,
        end_date=end_date,
        selected_snapshot=selected_snapshot,
        training_snapshot_resolver=_resolve_anfis_training_snapshot,
        strategy_comparison_baseline=_strategy_comparison_baseline,
        anfis_daily_runner=run_anfis_daily_irrigation_with_snapshot,
        seed=seed,
        generations=generations,
        population=population,
        persist=persist,
        baseline_result=baseline_result,
        trained_model=trained_model,
        training_metadata=training_metadata,
    )


def run_sparse_daily_irrigation_with_snapshot(
    start_date: date,
    end_date: date,
    sample_interval_hours: int,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    baseline_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    baseline_result = baseline_result or {}
    if baseline_result.get("stateAtExperimentStart"):
        simulation_start_date = start_date
        simulation_snapshot = selected_snapshot
        state_anchor_policy = "baseline_warmup_reuse"
        warmup_reuse_policy = "baseline_start_state_reuse"
    else:
        simulation_start_date, simulation_snapshot = resolve_simulation_snapshot(
            start_date,
            end_date,
            selected_snapshot,
        )
        state_anchor_policy = "stable_daily_timeline"
        warmup_reuse_policy = "independent_sparse_warmup"

    return run_sparse_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        sample_interval_hours=sample_interval_hours,
        persist=persist,
        selected_snapshot=selected_snapshot,
        simulation_start_date=simulation_start_date,
        simulation_snapshot=simulation_snapshot,
        state_anchor_policy=state_anchor_policy,
        warmup_reuse_policy=warmup_reuse_policy,
        baseline_result=baseline_result,
    )

def run_fuzzy_dt_daily_irrigation_with_snapshot(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    return run_fuzzy_dt_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        persist=persist,
        selected_snapshot=selected_snapshot,
    )


def run_anfis_daily_irrigation_with_snapshot(
    start_date: date,
    end_date: date,
    model: ANFIS | AnfisModelController,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
    decision_threshold: float = ANFIS_DECISION_THRESHOLD,
    forecast_decision_threshold: float = ANFIS_FORECAST_DECISION_THRESHOLD,
) -> dict[str, Any]:
    selected_snapshot = _resolve_snapshot(start_date, end_date, snapshot)
    return run_anfis_daily_irrigation(
        start_date=start_date,
        end_date=end_date,
        model=model,
        persist=persist,
        selected_snapshot=selected_snapshot,
        decision_threshold=decision_threshold,
        forecast_decision_threshold=forecast_decision_threshold,
    )

def _local_timestamp_key(value: str | datetime) -> str:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value)
    else:
        local_value = value
    if local_value.tzinfo is not None:
        local_value = local_value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return local_value.replace(microsecond=0).isoformat()




