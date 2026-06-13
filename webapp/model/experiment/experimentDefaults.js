sap.ui.define([
    "disertatie/model/presentation/basicFormat"
], (BasicFormat) => {
    "use strict";

    const { addDays, formatLocalDate } = BasicFormat;

    const REAL_FORECAST_HORIZON_DAYS = 15;
    const RECOMMENDED_SAMPLING_INTERVAL_HOURS = 48;

    function defaultSamplingSummary(sampleIntervalHours) {
        const intervalHours = sampleIntervalHours || RECOMMENDED_SAMPLING_INTERVAL_HOURS;
        return {
            totalEntries: 0,
            daysAnalyzed: 0,
            potsAnalyzed: 0,
            sample_interval_days: Math.max(1, Math.round(intervalHours / 24)),
            sample_interval_hours: intervalHours,
            accuracy_percent: 0,
            mismatch_days: 0,
            baseline_total_water_usage_l: 0,
            sparse_total_water_usage_l: 0,
            baseline_irrigation_event_count: 0,
            sparse_irrigation_event_count: 0,
            baseline_valve_run_count: 0,
            sparse_valve_run_count: 0,
            baseline_irrigation_decisions: 0,
            sparse_irrigation_decisions: 0,
            baseline_only_irrigation_days: 0,
            sparse_only_irrigation_days: 0,
            baseline_only_valve_runs: 0,
            baseline_only_water_usage_l: 0,
            missed_valve_run_delta: 0,
            sparse_extra_valve_run_delta: 0,
            sensorLocationCount: 0,
            sensorAssociatedPotCount: 0,
            sampledSensorRows: 0,
            sampledSensorMoments: 0,
            sampling_moisture_mae_pct: 0,
            sampling_moisture_bias_pct: 0,
            sampling_moisture_max_error_pct: 0,
            sampling_estimation_points: 0,
            sampling_sensor_refreshes: 0,
            sampling_direct_refreshes: 0,
            sampling_associated_refreshes: 0,
            sampling_missing_refreshes: 0,
            sampling_average_association_distance: 0,
            execution_time_seconds: 0
        };
    }

    function defaultAnfisSummary() {
        return {
            totalEntries: 0,
            daysAnalyzed: 0,
            potsAnalyzed: 0,
            baseline_irrigation_days: 0,
            anfis_irrigation_days: 0,
            baseline_total_water_usage_l: 0,
            anfis_total_water_usage_l: 0,
            water_savings_l: 0,
            water_savings_percent: 0,
            comfort_threshold_pct: 0,
            comfort_preserved_days: 0,
            comfort_preserved_percent: 0,
            moisture_safe_savings_percent: 0,
            baseline_agreement_percent: 0,
            baseline_mismatch_days: 0,
            baseline_only_irrigation_days: 0,
            anfis_only_irrigation_days: 0,
            missed_valve_run_delta: 0,
            anfis_extra_valve_run_delta: 0,
            baseline_irrigation_event_count: 0,
            anfis_irrigation_event_count: 0,
            baseline_valve_run_count: 0,
            anfis_valve_run_count: 0,
            test_mse: 0,
            test_rmse: 0,
            test_probability_fit_percent: 0,
            test_accuracy_percent: 0,
            test_decision_accuracy_percent: 0,
            test_decision_threshold: 0,
            test_samples: 0,
            execution_time_seconds: 0,
            predicted_probability_mean: 0,
            predicted_probability_min: 0,
            predicted_probability_max: 0
        };
    }

    function defaultFuzzySummary() {
        return {
            totalEntries: 0,
            daysAnalyzed: 0,
            potsAnalyzed: 0,
            baseline_irrigation_days: 0,
            fuzzy_irrigation_days: 0,
            baseline_total_water_usage_l: 0,
            fuzzy_total_water_usage_l: 0,
            water_savings_l: 0,
            water_savings_percent: 0,
            accuracy_percent: 0,
            mismatch_days: 0,
            baseline_agreement_percent: 0,
            baseline_mismatch_days: 0,
            baseline_only_irrigation_days: 0,
            fuzzy_only_irrigation_days: 0,
            missed_valve_run_delta: 0,
            fuzzy_extra_valve_run_delta: 0,
            comfort_threshold_pct: 0,
            comfort_preserved_days: 0,
            comfort_preserved_percent: 0,
            moisture_safe_savings_percent: 0,
            baseline_irrigation_event_count: 0,
            fuzzy_irrigation_event_count: 0,
            baseline_valve_run_count: 0,
            fuzzy_valve_run_count: 0,
            average_prescription_volume_l: 0,
            average_prescription_score_pct: 0,
            execution_time_seconds: 0
        };
    }

    function defaultExperimentFooter() {
        return {
            experimentLabel: "No experiment selected",
            daysAnalyzed: 0,
            pots: 0,
            timeInterval: "Daily"
        };
    }

    function experimentDisplayName(experiment) {
        return {
            sampling: "Sampling",
            anfis: "ANFIS-GA",
            fuzzy: "Fuzzy Control"
        }[experiment] || "No experiment selected";
    }

    function formatWeatherRange(range) {
        if (!range) {
            return null;
        }
        return `${range.start} to ${range.end}`;
    }

    function experimentRange(settings) {
        const fallback = defaultExperimentRange();
        return {
            start: settings.start_date || fallback.start,
            end: settings.end_date || fallback.end
        };
    }

    function defaultExperimentRange() {
        const today = new Date();
        return {
            start: formatLocalDate(addDays(today, -14)),
            end: formatLocalDate(addDays(today, 14))
        };
    }

    function weatherRangeKey(settings) {
        const range = experimentRange(settings || {});
        return `${range.start}|${range.end}`;
    }

    return {
        REAL_FORECAST_HORIZON_DAYS,
        RECOMMENDED_SAMPLING_INTERVAL_HOURS,
        defaultAnfisSummary,
        defaultExperimentFooter,
        defaultExperimentRange,
        defaultFuzzySummary,
        defaultSamplingSummary,
        experimentDisplayName,
        experimentRange,
        formatWeatherRange,
        weatherRangeKey
    };
});
