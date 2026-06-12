sap.ui.define([], () => {
    "use strict";

    const WATER_BAR_MAX_CHART_VALUE = 20;
    const MOISTURE_THRESHOLD_PCT = 37;
    const MOISTURE_THRESHOLD_COLOR = "#43bfd2";
    const ANFIS_SCORE_MEASURE = "ANFIS zone signal (%)";
    const FUZZY_SCORE_MEASURE = "Fuzzy irrigation score (%)";
    const FUZZY_PRESCRIPTION_SCORE_MAX_MM = 8;

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
            const prescriptionMm = toChartNumber(entry.fuzzy_prescription_mm, true);
            const prescriptionScore = prescriptionMm === null
                ? null
                : Math.min(
                    100,
                    Math.max(0, prescriptionMm / FUZZY_PRESCRIPTION_SCORE_MAX_MM * 100)
                );
            return Object.assign({}, entry, {
                fuzzy_prescription_score_pct: prescriptionScore === null ? null : Number(prescriptionScore.toFixed(2))
            });
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

    const EXPERIMENT_CHART_IDS = [
        "samplingMoistureChart",
        "samplingContextChart",
        "samplingBaselineWeatherChart",
        "anfisMoistureChart",
        "anfisContextChart",
        "fuzzyMoistureChart",
        "fuzzyContextChart"
    ];

    const CHART_DATA_PATHS = {
        samplingMoistureChart: "/samplingChartEntries",
        samplingContextChart: "/samplingChartEntries",
        samplingBaselineWeatherChart: "/samplingChartEntries",
        anfisMoistureChart: "/anfisChartEntries",
        anfisContextChart: "/anfisChartEntries",
        fuzzyMoistureChart: "/fuzzyChartEntries",
        fuzzyContextChart: "/fuzzyChartEntries"
    };

    const CHART_SOURCE_DATA_PATHS = {
        samplingMoistureChart: "/samplingChartAllEntries",
        samplingContextChart: "/samplingChartAllEntries",
        samplingBaselineWeatherChart: "/samplingChartAllEntries",
        anfisMoistureChart: "/anfisChartAllEntries",
        anfisContextChart: "/anfisChartAllEntries",
        fuzzyMoistureChart: "/fuzzyChartAllEntries",
        fuzzyContextChart: "/fuzzyChartAllEntries"
    };

    /* eslint-disable @sap-ux/fiori-tools/sap-no-hardcoded-color -- VizFrame series palettes require explicit colors. */
    const CHART_PALETTES = {
        samplingMoistureChart: ["#2FC2CC ", "#7FCF45"],
        samplingContextChart: ["#2FC2CC ", "#7FCF45", "#b7d8ff", "#FFC107"],
        anfisMoistureChart: ["#2FC2CC ", "#2b8cbe", "#7FCF45"],
        anfisContextChart: ["#2FC2CC ", "#2b8cbe", "#b7d8ff", "#FFC107"],
        fuzzyMoistureChart: ["#2FC2CC", "#ACA8F2", "#7FCF45"],
        fuzzyContextChart: ["#2FC2CC", "#ACA8F2", "#b7d8ff", "#FFC107"]
    };
    /* eslint-enable @sap-ux/fiori-tools/sap-no-hardcoded-color */
    CHART_PALETTES.samplingBaselineWeatherChart = [
        CHART_PALETTES.samplingMoistureChart[0],
        CHART_PALETTES.samplingContextChart[0],
        CHART_PALETTES.samplingContextChart[2],
        CHART_PALETTES.samplingContextChart[3]
    ];

    const CHART_FORMATS = {
        samplingMoistureChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Sparse Moisture": "DT_PERCENT"
        },
        samplingContextChart: {
            "Baseline Irrigation (L)": "DT_NUMBER",
            "Sparse-Sensing Irrigation (L)": "DT_NUMBER",
            "Rain (mm)": "DT_MM",
            "Max Temperature (°C)": "DT_CELSIUS"
        },
        samplingBaselineWeatherChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "Rain (mm)": "DT_MM",
            "Max Temperature (°C)": "DT_CELSIUS"
        },
        anfisMoistureChart: {
            "Baseline Moisture": "DT_PERCENT",
            "ANFIS Moisture": "DT_PERCENT",
            [ANFIS_SCORE_MEASURE]: "DT_PERCENT"
        },
        anfisContextChart: {
            "Baseline Irrigation (L)": "DT_NUMBER",
            "ANFIS Water Usage (L)": "DT_NUMBER",
            "Rain (mm)": "DT_MM",
            "Max Temperature (°C)": "DT_CELSIUS"
        },
        fuzzyMoistureChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Fuzzy Moisture": "DT_PERCENT",
            [FUZZY_SCORE_MEASURE]: "DT_PERCENT"
        },
        fuzzyContextChart: {
            "Baseline Irrigation (L)": "DT_NUMBER",
            "Fuzzy Water Usage (L)": "DT_NUMBER",
            "Rain (mm)": "DT_MM",
            "Max Temperature (°C)": "DT_CELSIUS"
        },
        samplingChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "Sparse Moisture": "DT_PERCENT",
            "Sparse Water Usage (L)": "DT_NUMBER",
            "Max Temp (°C)": "DT_CELSIUS",
            "Rain (mm)": "DT_MM"
        },
        anfisChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "ANFIS Moisture": "DT_PERCENT",
            "ANFIS Water Usage (L)": "DT_NUMBER",
            [ANFIS_SCORE_MEASURE]: "DT_PERCENT",
            "Max Temp (°C)": "DT_CELSIUS",
            "Rain (mm)": "DT_MM"
        },
        fuzzyChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Baseline Water Usage (L)": "DT_NUMBER",
            "Fuzzy Moisture": "DT_PERCENT",
            [FUZZY_SCORE_MEASURE]: "DT_PERCENT",
            "Fuzzy Water Usage (L)": "DT_NUMBER",
            "Max Temp (°C)": "DT_CELSIUS",
            "Rain (mm)": "DT_MM"
        }
    };

    const CHART_DATA_SHAPES = {
        samplingMoistureChart: ["line", "line"],
        samplingContextChart: ["bar", "bar", "bar", "line"],
        samplingBaselineWeatherChart: ["line", "bar", "bar", "line"],
        anfisMoistureChart: ["line", "line", "line"],
        anfisContextChart: ["bar", "bar", "bar", "line"],
        fuzzyMoistureChart: ["line", "line", "line"],
        fuzzyContextChart: ["bar", "bar", "bar", "line"],
        samplingChart: ["line", "bar", "line", "bar", "line", "bar"],
        anfisChart: ["line", "bar", "line", "bar", "line", "line", "bar"],
        fuzzyChart: ["line", "bar", "line", "line", "bar", "line", "bar"]
    };

    const CHART_BASELINE_MEASURES = new Set([
        "Baseline Moisture",
        "Baseline Water Usage (L)",
        "Baseline Irrigation (L)"
    ]);

    const CHART_WEATHER_MEASURES = new Set([
        "Max Temp (°C)",
        "Max Temperature (°C)",
        "Rain (mm)"
    ]);

    const CHART_BASELINE_FIELD_KEYS = [
        "baseline_moisture",
        "baseline_water_usage_chart",
        "baseline_water_usage_l"
    ];

    const CHART_WEATHER_FIELD_KEYS = [
        "max_temperature",
        "rain_amount"
    ];

    const CONTEXT_CHART_IDS = new Set([
        "samplingContextChart",
        "anfisContextChart",
        "fuzzyContextChart"
    ]);

    const BASELINE_WEATHER_WATER_AXIS_MEASURES = new Set([
        "Baseline Water Usage (L)"
    ]);

    const SECONDARY_AXIS_MEASURES = new Set([
        "Max Temperature (°C)",
        "Rain (mm)"
    ]);

    const CHART_MEASURES = {
        samplingMoistureChart: [
            "Baseline Moisture",
            "Sparse Moisture"
        ],
        samplingContextChart: [
            "Baseline Irrigation (L)",
            "Sparse-Sensing Irrigation (L)",
            "Rain (mm)",
            "Max Temperature (°C)"
        ],
        samplingBaselineWeatherChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "Rain (mm)",
            "Max Temperature (°C)"
        ],
        anfisMoistureChart: [
            "Baseline Moisture",
            "ANFIS Moisture",
            ANFIS_SCORE_MEASURE
        ],
        anfisContextChart: [
            "Baseline Irrigation (L)",
            "ANFIS Water Usage (L)",
            "Rain (mm)",
            "Max Temperature (°C)"
        ],
        fuzzyMoistureChart: [
            "Baseline Moisture",
            "Fuzzy Moisture",
            FUZZY_SCORE_MEASURE
        ],
        fuzzyContextChart: [
            "Baseline Irrigation (L)",
            "Fuzzy Water Usage (L)",
            "Rain (mm)",
            "Max Temperature (°C)"
        ],
        samplingChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "Sparse Moisture",
            "Sparse Water Usage (L)",
            "Max Temp (°C)",
            "Rain (mm)"
        ],
        anfisChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "ANFIS Moisture",
            "ANFIS Water Usage (L)",
            ANFIS_SCORE_MEASURE,
            "Max Temp (°C)",
            "Rain (mm)"
        ],
        fuzzyChart: [
            "Baseline Moisture",
            "Baseline Water Usage (L)",
            "Fuzzy Moisture",
            FUZZY_SCORE_MEASURE,
            "Fuzzy Water Usage (L)",
            "Max Temp (°C)",
            "Rain (mm)"
        ]
    };

    function normalizedChartVisibility(visibility) {
        const options = visibility || {};
        return {
            baseline: options.baseline !== false,
            weather: options.weather !== false
        };
    }

    function visibleChartMeasureIndexes(chartId, visibility) {
        const measures = CHART_MEASURES[chartId] || [];
        const options = normalizedChartVisibility(visibility);
        const indexes = measures.reduce((output, measure, index) => {
            const hiddenBaseline = !options.baseline && CHART_BASELINE_MEASURES.has(measure);
            const hiddenWeather = !options.weather && CHART_WEATHER_MEASURES.has(measure);
            if (!hiddenBaseline && !hiddenWeather) {
                output.push(index);
            }
            return output;
        }, []);
        return indexes.length ? indexes : measures.map((measure, index) => index);
    }

    function visibleChartMeasures(chartId, visibility) {
        const measures = CHART_MEASURES[chartId] || [];
        return visibleChartMeasureIndexes(chartId, visibility).map((index) => measures[index]);
    }

    function visibleChartDataShapes(chartId, visibility) {
        const shapes = CHART_DATA_SHAPES[chartId] || [];
        return visibleChartMeasureIndexes(chartId, visibility).map((index) => shapes[index]).filter(Boolean);
    }

    function visibleChartPalette(chartId, visibility) {
        const palette = CHART_PALETTES[chartId] || [];
        return visibleChartMeasureIndexes(chartId, visibility).map((index) => palette[index]).filter(Boolean);
    }

    function chartMeasureColor(chartId, measure) {
        const measures = CHART_MEASURES[chartId] || [];
        const index = measures.indexOf(measure);
        if (index < 0) {
            return null;
        }
        return (CHART_PALETTES[chartId] || [])[index] || null;
    }

    function isSecondaryAxisMeasure(chartId, measure) {
        if (chartId === "samplingBaselineWeatherChart") {
            return BASELINE_WEATHER_WATER_AXIS_MEASURES.has(measure);
        }
        return (CONTEXT_CHART_IDS.has(chartId) && SECONDARY_AXIS_MEASURES.has(measure))
            || (chartId === "anfisMoistureChart" && measure === ANFIS_SCORE_MEASURE)
            || (chartId === "fuzzyMoistureChart" && measure === FUZZY_SCORE_MEASURE);
    }

    function visibleChartMeasuresByAxis(chartId, visibility) {
        return visibleChartMeasureIndexes(chartId, visibility).reduce((axes, index) => {
            const measure = (CHART_MEASURES[chartId] || [])[index];
            const axisKey = isSecondaryAxisMeasure(chartId, measure) ? "secondaryAxis" : "primaryAxis";
            axes[axisKey].push(measure);
            return axes;
        }, { primaryAxis: [], secondaryAxis: [] });
    }

    function visibleChartDataShapesByAxis(chartId, visibility) {
        return visibleChartMeasureIndexes(chartId, visibility).reduce((axes, index) => {
            const measure = (CHART_MEASURES[chartId] || [])[index];
            const shape = (CHART_DATA_SHAPES[chartId] || [])[index];
            if (!shape) {
                return axes;
            }
            const axisKey = isSecondaryAxisMeasure(chartId, measure) ? "secondaryAxis" : "primaryAxis";
            axes[axisKey].push(shape);
            return axes;
        }, { primaryAxis: [], secondaryAxis: [] });
    }

    function withChartVisibility(entries, visibility) {
        const rows = Array.isArray(entries) ? entries : [];
        const options = normalizedChartVisibility(visibility);
        const hiddenKeys = [];
        if (!options.baseline) {
            hiddenKeys.push(...CHART_BASELINE_FIELD_KEYS);
        }
        if (!options.weather) {
            hiddenKeys.push(...CHART_WEATHER_FIELD_KEYS);
        }

        if (!hiddenKeys.length) {
            return rows;
        }

        return rows.map((entry) => {
            const output = Object.assign({}, entry);
            hiddenKeys.forEach((key) => {
                output[key] = null;
            });
            return output;
        });
    }

    const INITIAL_VISIBLE_CHART_DAYS = 30;

    return {
        CHART_BASELINE_MEASURES,
        CHART_BASELINE_FIELD_KEYS,
        CHART_DATA_PATHS,
        CHART_DATA_SHAPES,
        CHART_FORMATS,
        CHART_MEASURES,
        CHART_PALETTES,
        CHART_SOURCE_DATA_PATHS,
        CHART_WEATHER_FIELD_KEYS,
        CHART_WEATHER_MEASURES,
        EXPERIMENT_CHART_IDS,
        INITIAL_VISIBLE_CHART_DAYS,
        MOISTURE_THRESHOLD_COLOR,
        MOISTURE_THRESHOLD_PCT,
        WATER_BAR_MAX_CHART_VALUE,
        chartMeasureColor,
        entryTimestamp,
        prepareChartResult,
        visibleChartDataShapes,
        visibleChartDataShapesByAxis,
        visibleChartMeasures,
        visibleChartMeasuresByAxis,
        visibleChartPalette,
        withChartVisibility
    };
});
