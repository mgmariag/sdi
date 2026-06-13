from __future__ import annotations

import time as perf_time
from collections.abc import Callable
from datetime import date
from typing import Any

import digital_twin.simulation.anfis.modeling as modeling
import digital_twin.simulation.metrics as metrics
import digital_twin.simulation.state.lookback as state_lookback
import digital_twin.simulation.state.sensor_context as sensor_context_helpers
from digital_twin.simulation.anfis.model import probability_category
from digital_twin.simulation.experiment_comparison import ExperimentComparison
from digital_twin.simulation.shared.constants import (
    ANFIS_DECISION_THRESHOLD,
    ANFIS_FORECAST_DECISION_THRESHOLD,
)
from digital_twin.simulation.shared.types import ExperimentSnapshot

TrainingSnapshotResolver = Callable[[date, date, ExperimentSnapshot], ExperimentSnapshot]
BaselineResolver = Callable[[date, date, ExperimentSnapshot, dict[str, Any] | None], dict[str, Any]]
AnfisDailyRunner = Callable[..., dict[str, Any]]


def train_anfis_model_from_snapshot_context(
    start_date: date,
    end_date: date,
    selected_snapshot: ExperimentSnapshot,
    training_snapshot_resolver: TrainingSnapshotResolver,
    seed: int | None = 2026,
    generations: int = 35,
    population: int = 24,
) -> modeling.AnfisTrainingResult:
    """Train and evaluate the ANFIS controller from database sensor/weather examples."""
    training_snapshot = training_snapshot_resolver(start_date, end_date, selected_snapshot)
    anfis_dataset = modeling.generate_database_anfis_dataset(
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
    fit_dataset, calibration_dataset = modeling.split_anfis_training_calibration(train_dataset, seed)
    model = modeling.train_anfis_controller(
        fit_dataset,
        calibration_dataset,
        generations=generations,
        population=population,
        seed=seed,
    )
    evaluation = modeling.evaluate_anfis_model(model, train_dataset)
    metadata = {
        "train_samples": len(train_dataset),
        "fit_samples": len(fit_dataset),
        "weighted_fit_samples": len(modeling.expand_anfis_training_dataset(fit_dataset)),
        "calibration_samples": len(calibration_dataset),
        "test_samples": len(train_dataset),
        "evaluation_samples": len(train_dataset),
        "training_sample_policy": "all_available_sensor_readings",
        "evaluation_sample_policy": "all_available_sensor_readings",
        "training_dataset_version": modeling.ANFIS_TRAINING_DATASET_VERSION,
        "seed": seed,
        "generations": generations,
        "population": population,
        "training_start_date": training_snapshot.start_date.isoformat(),
        "training_end_date": training_snapshot.end_date.isoformat(),
        "training_lookback_days": (training_snapshot.end_date - training_snapshot.start_date).days + 1,
        "training_history_days": (training_snapshot.end_date - training_snapshot.start_date).days + 1,
        "training_signals": modeling.anfis_training_signal_summary(train_dataset),
        "anfis_input_features": list(model.global_model.input_names),
        "evaluation": evaluation,
    }
    return modeling.AnfisTrainingResult(model=model, evaluation=evaluation, metadata=metadata)


def run_daily_anfis_experiment(
    start_date: date,
    end_date: date,
    selected_snapshot: ExperimentSnapshot,
    training_snapshot_resolver: TrainingSnapshotResolver,
    strategy_comparison_baseline: BaselineResolver,
    anfis_daily_runner: AnfisDailyRunner,
    seed: int | None = 2026,
    generations: int = 35,
    population: int = 24,
    persist: bool = False,
    baseline_result: dict[str, Any] | None = None,
    trained_model: modeling.AnfisModelController | None = None,
    training_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply an ANFIS controller to the same database weather/pot simulation."""
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    sensor_context = selected_snapshot.sensor_context

    start_time = perf_time.perf_counter()
    if trained_model is None:
        training_result = train_anfis_model_from_snapshot_context(
            start_date=start_date,
            end_date=end_date,
            selected_snapshot=selected_snapshot,
            training_snapshot_resolver=training_snapshot_resolver,
            seed=seed,
            generations=generations,
            population=population,
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

    decision_threshold = ANFIS_DECISION_THRESHOLD
    forecast_decision_threshold = ANFIS_FORECAST_DECISION_THRESHOLD
    threshold_calibration = {
        "threshold": decision_threshold,
        "raw_threshold": ANFIS_DECISION_THRESHOLD,
        "forecast_threshold": forecast_decision_threshold,
        "raw_forecast_threshold": ANFIS_FORECAST_DECISION_THRESHOLD,
        "calibrated": False,
        "reason": "fixed_threshold",
    }

    baseline = strategy_comparison_baseline(start_date, end_date, selected_snapshot, baseline_result)
    anfis = anfis_daily_runner(
        start_date=start_date,
        end_date=end_date,
        model=model,
        persist=persist,
        snapshot=selected_snapshot,
        decision_threshold=decision_threshold,
        forecast_decision_threshold=forecast_decision_threshold,
    )

    comparison = ExperimentComparison(start_date, end_date, selected_snapshot, baseline, anfis, "anfis", "anfis_water_usage_l")

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
    control_pots = sensor_context_helpers.sensor_control_pots(selected_snapshot.pots, selected_snapshot.sensor_context)
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
    comfort_metrics = metrics.moisture_safe_savings_metrics(entries, "anfis", control_pots, water_savings_percent)
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
    summary.update(state_lookback.sensor_summary_fields(sensor_context))
    summary["source"] = state_lookback.experiment_source(sensor_context)
    return comparison.payload(entries, chart_entries, summary)

