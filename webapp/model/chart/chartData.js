sap.ui.define([], () => {
    "use strict";

    const WATER_BAR_MAX_CHART_VALUE = 20;

    function toChartNumber(value, preserveBlank) {
        if (preserveBlank && (value === null || value === undefined || value === "")) {
            return null;
        }
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
                const chartValue = maxWater > 0
                    ? (toChartNumber(entry[mapping.sourceKey]) / maxWater) * WATER_BAR_MAX_CHART_VALUE
                    : 0;
                output[mapping.targetKey] = Number(chartValue.toFixed(2));
            });
            return output;
        });
    }

    function withDerivedChartFields(entries) {
        const rows = Array.isArray(entries) ? entries : [];
        return rows.map((entry) => {
            const prescriptionScore = fuzzyPrescriptionScore(entry);
            const normalizedScore = prescriptionScore === null
                ? null
                : Math.min(
                    100,
                    Math.max(0, prescriptionScore)
                );
            return Object.assign({}, entry, {
                fuzzy_prescription_score_pct: normalizedScore === null ? null : Number(normalizedScore.toFixed(2))
            });
        });
    }

    function fuzzyPrescriptionScore(entry) {
        const directScore = toChartNumber(entry && entry.fuzzy_prescription_score_pct, true);
        return directScore === null ? null : directScore;
    }

    function entryTimestamp(entry) {
        const value = entry && (entry.timestamp || entry.recorded_at || entry.observed_at || entry.date || entry.day_label);
        if (!value) {
            return null;
        }
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function prepareChartResult(result, waterMappings) {
        const entries = withDerivedChartFields(Array.isArray(result.entries) ? result.entries : []);
        const chartEntries = Array.isArray(result.chartEntries) && result.chartEntries.length > 0
            ? result.chartEntries
            : entries;
        const usesDetailRows = result.summary && result.summary.chartGranularity && result.summary.chartGranularity !== "daily";
        const tableEntries = usesDetailRows ? withDerivedChartFields(chartEntries) : entries;
        const scaledChartEntries = withDerivedChartFields(withScaledChartWater(chartEntries, waterMappings));
        return Object.assign({}, result, {
            entries,
            chartEntries: scaledChartEntries,
            tableEntries,
            pots: Array.isArray(result.pots) ? result.pots : []
        });
    }

    return {
        WATER_BAR_MAX_CHART_VALUE,
        entryTimestamp,
        prepareChartResult
    };
});
