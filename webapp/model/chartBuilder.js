sap.ui.define([], () => {
    "use strict";

    function toChartNumber(value) {
        const numberValue = Number(value);
        return Number.isFinite(numberValue) ? numberValue : 0;
    }

    function normalizeWaterMappings(waterMappings) {
        if (Array.isArray(waterMappings)) {
            return waterMappings.filter((mapping) => mapping && mapping.sourceKey && mapping.targetKey);
        }
        if (waterMappings && waterMappings.sourceKey && waterMappings.targetKey) {
            return [waterMappings];
        }
        return [];
    }

    function withScaledChartWater(entries, waterMappings) {
        const rows = Array.isArray(entries) ? entries : [];
        const mappings = normalizeWaterMappings(waterMappings);
        const maxWater = rows.reduce((maxValue, entry) => {
            return Math.max(maxValue, ...mappings.map((mapping) => toChartNumber(entry[mapping.sourceKey])));
        }, 0);

        return rows.map((entry) => {
            const output = Object.assign({}, entry, {
                chart_label: entry.chart_label || entry.day_label || entry.timestamp || ""
            });
            mappings.forEach((mapping) => {
                const chartValue = maxWater > 0 ? (toChartNumber(entry[mapping.sourceKey]) / maxWater) * 20 : 0;
                output[mapping.targetKey] = Number(chartValue.toFixed(2));
            });
            return output;
        });
    }

    function entryTimestamp(entry) {
        const value = entry && (entry.timestamp || entry.recorded_at || entry.observed_at || entry.date || entry.day_label);
        if (!value) {
            return null;
        }
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function withLineStyleMetadata(entries, summary) {
        const rows = Array.isArray(entries) ? entries : [];
        const now = new Date();

        return rows.map((entry) => {
            const output = Object.assign({}, entry);
            const timestamp = entryTimestamp(entry);
            const hasBackendProvenance = Object.prototype.hasOwnProperty.call(entry, "has_prediction_or_simulation")
                || Object.prototype.hasOwnProperty.call(entry, "is_weather_prediction")
                || Object.prototype.hasOwnProperty.call(entry, "is_sensor_prediction")
                || Object.prototype.hasOwnProperty.call(entry, "is_sensor_simulated")
                || Object.prototype.hasOwnProperty.call(entry, "is_sensor_missing_reading");
            const fallbackPrediction = Boolean(!hasBackendProvenance && timestamp && timestamp > now);
            const isPrediction = Boolean(entry.is_weather_prediction || entry.is_sensor_prediction || fallbackPrediction);
            const isSimulatedHistory = Boolean(entry.is_sensor_simulated && timestamp && timestamp <= now);
            output.is_prediction = isPrediction;
            output.is_simulated_history = isSimulatedHistory;
            output.has_interrupted_line = Boolean(
                entry.has_prediction_or_simulation
                || isPrediction
                || entry.is_sensor_simulated
                || entry.is_sensor_missing_reading
            );
            return output;
        });
    }

    function prepareChartResult(result, waterMappings) {
        const entries = Array.isArray(result.entries) ? result.entries : [];
        const chartEntries = Array.isArray(result.chartEntries) && result.chartEntries.length > 0 ? result.chartEntries : entries;
        const usesDetailRows = result.summary && result.summary.chartGranularity && result.summary.chartGranularity !== "daily";
        const tableEntries = usesDetailRows ? chartEntries : entries;
        const scaledChartEntries = withScaledChartWater(chartEntries, waterMappings);
        return Object.assign({}, result, {
            entries,
            chartEntries: withLineStyleMetadata(scaledChartEntries, result.summary),
            tableEntries,
            pots: Array.isArray(result.pots) ? result.pots : []
        });
    }

    const CHART_PALETTES = {
        samplingChart: ["#4d82d8", "#9dbef7", "#43bfd2", "#b9e98e", "#f7df8a", "#56ccf2"],
        anfisChart: ["#4d82d8", "#9dbef7", "#43bfd2", "#cdefa7", "#b9e98e", "#f7df8a", "#56ccf2"],
        fuzzyChart: ["#4d82d8", "#9dbef7", "#43bfd2", "#d7b6dd", "#d5addd", "#f7df8a", "#56ccf2"]
    };

    const CHART_FORMATS = {
        samplingChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "Sparse Moisture": "DT_PERCENT",
            "Sparse Water Usage (L)": "DT_NUMBER",
            "Max Temp (C)": "DT_CELSIUS",
            "Rain (L/m2)": "DT_LM2"
        },
        anfisChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "ANFIS Moisture": "DT_PERCENT",
            "Predicted Probability": "DT_PERCENT",
            "ANFIS Water Usage (L)": "DT_NUMBER",
            "Max Temp (C)": "DT_CELSIUS",
            "Rain (L/m2)": "DT_LM2"
        },
        fuzzyChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "Fuzzy Moisture": "DT_PERCENT",
            "Fuzzy Prescription (mm)": "DT_MM",
            "Fuzzy Water Usage (L)": "DT_NUMBER",
            "Max Temp (C)": "DT_CELSIUS",
            "Rain (L/m2)": "DT_LM2"
        }
    };

    const CHART_DATA_SHAPES = {
        samplingChart: ["line", "bar", "line", "bar", "line", "bar"],
        anfisChart: ["line", "bar", "line", "line", "bar", "line", "bar"],
        fuzzyChart: ["line", "bar", "line", "line", "bar", "line", "bar"]
    };

    const SENSOR_DEPENDENT_LINE_MEASURES = new Set([
        "Baseline Moisture",
        "Sparse Moisture",
        "ANFIS Moisture",
        "Fuzzy Moisture"
    ]);

    const WEATHER_LINE_MEASURES = new Set([
        "Max Temp (C)"
    ]);

    const CHART_MEASURES = {
        samplingChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "Sparse Moisture",
            "Sparse Water Usage (L)",
            "Max Temp (C)",
            "Rain (L/m2)"
        ],
        anfisChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "ANFIS Moisture",
            "Predicted Probability",
            "ANFIS Water Usage (L)",
            "Max Temp (C)",
            "Rain (L/m2)"
        ],
        fuzzyChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "Fuzzy Moisture",
            "Fuzzy Prescription (mm)",
            "Fuzzy Water Usage (L)",
            "Max Temp (C)",
            "Rain (L/m2)"
        ]
    };

    const INITIAL_VISIBLE_CHART_DAYS = 30;

    return {
        CHART_DATA_SHAPES,
        CHART_FORMATS,
        CHART_MEASURES,
        CHART_PALETTES,
        INITIAL_VISIBLE_CHART_DAYS,
        SENSOR_DEPENDENT_LINE_MEASURES,
        WEATHER_LINE_MEASURES,
        entryTimestamp,
        prepareChartResult
    };
});
