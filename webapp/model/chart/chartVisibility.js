sap.ui.define([
    "disertatie/model/chart/chartConfig"
], (ChartConfig) => {
    "use strict";

    const {
        ANFIS_SCORE_MEASURE,
        CHART_BASELINE_FIELD_KEYS,
        CHART_BASELINE_MEASURES,
        CHART_DATA_SHAPES,
        CHART_MEASURES,
        CHART_PALETTES,
        CHART_WEATHER_FIELD_KEYS,
        CHART_WEATHER_MEASURES,
        FUZZY_SCORE_MEASURE
    } = ChartConfig;

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

    return {
        chartMeasureColor,
        visibleChartDataShapes,
        visibleChartDataShapesByAxis,
        visibleChartMeasures,
        visibleChartMeasuresByAxis,
        visibleChartPalette,
        withChartVisibility
    };
});
