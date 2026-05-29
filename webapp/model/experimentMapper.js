sap.ui.define([
    "disertatie/model/formatter"
], (Formatter) => {
    "use strict";

    const { addDays, formatLocalDate } = Formatter;

    const REAL_FORECAST_HORIZON_DAYS = 15;

    function defaultSamplingSummary(sampleIntervalHours) {
        return {
            totalEntries: 0,
            daysAnalyzed: 0,
            potsAnalyzed: 0,
            sample_interval_days: 3,
            sample_interval_hours: sampleIntervalHours || 72,
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
            baseline_irrigation_event_count: 0,
            anfis_irrigation_event_count: 0,
            baseline_valve_run_count: 0,
            anfis_valve_run_count: 0,
            test_mse: 0,
            test_accuracy_percent: 0,
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
            fao_irrigation_days: 0,
            baseline_irrigation_days: 0,
            fuzzy_irrigation_days: 0,
            fao_total_water_usage_l: 0,
            baseline_total_water_usage_l: 0,
            fuzzy_total_water_usage_l: 0,
            water_savings_l: 0,
            water_savings_percent: 0,
            fao_irrigation_event_count: 0,
            baseline_irrigation_event_count: 0,
            fuzzy_irrigation_event_count: 0,
            fao_valve_run_count: 0,
            baseline_valve_run_count: 0,
            fuzzy_valve_run_count: 0,
            average_prescription_mm: 0,
            average_etc_mm: 0,
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
            fuzzy: "Fuzzy DT"
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
