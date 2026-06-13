sap.ui.define([], () => {
    "use strict";

    const MOISTURE_THRESHOLD_PCT = 37;
    const MOISTURE_THRESHOLD_COLOR = "#43bfd2";
    const ANFIS_SCORE_MEASURE = "ANFIS zone signal (%)";
    const FUZZY_SCORE_MEASURE = "Fuzzy irrigation score (%)";

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

    const INITIAL_VISIBLE_CHART_DAYS = 30;

    return {
        ANFIS_SCORE_MEASURE,
        CHART_BASELINE_FIELD_KEYS,
        CHART_BASELINE_MEASURES,
        CHART_DATA_PATHS,
        CHART_DATA_SHAPES,
        CHART_FORMATS,
        CHART_MEASURES,
        CHART_PALETTES,
        CHART_SOURCE_DATA_PATHS,
        CHART_WEATHER_FIELD_KEYS,
        CHART_WEATHER_MEASURES,
        EXPERIMENT_CHART_IDS,
        FUZZY_SCORE_MEASURE,
        INITIAL_VISIBLE_CHART_DAYS,
        MOISTURE_THRESHOLD_COLOR,
        MOISTURE_THRESHOLD_PCT
    };
});
