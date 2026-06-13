sap.ui.define([
    "disertatie/model/chart/chartConfig",
    "disertatie/model/chart/chartVisibility",
    "disertatie/model/chart/chartOverlay",
    "disertatie/model/chart/chartPopover",
    "disertatie/model/chart/chartWindowSync",
    "sap/viz/ui5/format/ChartFormatter",
    "sap/viz/ui5/api/env/Format",
    "sap/viz/ui5/controls/common/feeds/FeedItem"
], (ChartConfig, ChartVisibility, ChartOverlay, ChartPopover, ChartWindowSync, ChartFormatter, Format, FeedItem) => {
    "use strict";

    const {
        ANFIS_SCORE_MEASURE,
        CHART_DATA_PATHS,
        CHART_FORMATS,
        CHART_MEASURES,
        CHART_SOURCE_DATA_PATHS,
        EXPERIMENT_CHART_IDS,
        FUZZY_SCORE_MEASURE
    } = ChartConfig;
    const {
        visibleChartDataShapesByAxis,
        visibleChartMeasuresByAxis,
        visibleChartPalette,
        withChartVisibility,
        chartMeasureColor
    } = ChartVisibility;
    const LEGEND_LABELS = {
        "Baseline Moisture": "Baseline",
        "Sparse Moisture": "Sparse sensing",
        "ANFIS Moisture": "ANFIS estimate",
        "Fuzzy Moisture": "Fuzzy estimate",
        [ANFIS_SCORE_MEASURE]: "ANFIS signal",
        "Baseline Irrigation (L)": "Baseline",
        "Baseline Water Usage (L)": "Baseline water",
        "Sparse-Sensing Irrigation (L)": "Sparse sensing",
        "Sparse Water Usage (L)": "Sparse water",
        "ANFIS Water Usage (L)": "ANFIS",
        "Fuzzy Water Usage (L)": "Fuzzy",
        [FUZZY_SCORE_MEASURE]: "Fuzzy score",
        "Rain (mm)": "Rain (mm)",
        "Max Temperature (°C)": "Max Temperature (°C)",
        "Max Temp (°C)": "Max Temperature (°C)"
    };
    const CHART_LEGEND_LABELS = {
        samplingBaselineWeatherChart: {
            "Baseline Moisture": "Moisture",
            "Baseline Water Usage (L)": "Water Usage"
        }
    };
    return Object.assign({}, ChartOverlay, ChartPopover, ChartWindowSync, {
        onAfterRendering() {
            this._styleCharts();
        },

        _destroyChartRuntime() {
            this._clearChartRuntimeTimers(this._chartWindowTimers, true);
            this._clearChartRuntimeTimers(this._chartOverlayTimers, true);
            this._clearChartRuntimeTimers(this._chartWindowSyncTimers, false);
            this._cancelChartRuntimeFrames(this._chartOverlayFrameIds);
            this._destroyChartRuntimeObservers(this._chartOverlayObservers);
            this._destroyChartRuntimeObservers(this._chartWindowSyncObservers);
            this._runChartRuntimeCleanups(this._chartOverlayListenerCleanups);
            this._runChartRuntimeCleanups(this._chartWindowSyncListenerCleanups);
        },

        _clearChartRuntimeTimers(timerMap, valuesAreArrays) {
            Object.keys(timerMap || {}).forEach((key) => {
                const timers = valuesAreArrays ? timerMap[key] : [timerMap[key]];
                timers.filter(Boolean).forEach((timerId) => clearTimeout(timerId));
                delete timerMap[key];
            });
        },

        _cancelChartRuntimeFrames(frameMap) {
            Object.keys(frameMap || {}).forEach((key) => {
                cancelAnimationFrame(frameMap[key]);
                delete frameMap[key];
            });
        },

        _destroyChartRuntimeObservers(observerMap) {
            Object.keys(observerMap || {}).forEach((key) => {
                const entry = observerMap[key];
                if (entry && entry.observer) {
                    entry.observer.disconnect();
                }
                delete observerMap[key];
            });
        },

        _runChartRuntimeCleanups(cleanupMap) {
            Object.keys(cleanupMap || {}).forEach((key) => {
                if (typeof cleanupMap[key] === "function") {
                    cleanupMap[key]();
                }
                delete cleanupMap[key];
            });
        },

        _styleCharts() {
            EXPERIMENT_CHART_IDS.forEach((chartId) => {
                const chart = this.byId(chartId);
                if (chart) {
                    this._styleChart(chart, chartId);
                }
            });
        },

        _styleChart(chart, chartId) {
            const formatString = CHART_FORMATS[chartId] || {};
            const visibility = this._chartVisibility();
            const dataShapes = visibleChartDataShapesByAxis(chartId, visibility);
            const valueAxis = {
                label: { style: { color: "#5d7187" } },
                title: { visible: false }
            };
            const primaryAxisScale = this._primaryAxisScale(chartId);
            if (primaryAxisScale) {
                valueAxis.scale = primaryAxisScale;
            }

            this._applyChartFeedVisibility(chart, chartId, visibility);

            chart.setVizProperties({
                plotArea: {
                    colorPalette: visibleChartPalette(chartId, visibility),
                    dataShape: {
                        primaryAxis: dataShapes.primaryAxis,
                        secondaryAxis: dataShapes.secondaryAxis
                    },
                    dataPointStyleMode: "update",
                    dataPointStyle: {
                        rules: this._chartSeriesStyleRules(chartId)
                    },
                    window: this._initialChartWindow(chartId),
                    dataLabel: {
                        visible: false,
                        formatString
                    },
                    dataPoint: {
                        visible: true
                    },
                    drawingEffect: "normal"
                },
                legend: {
                    visible: false,
                    position: "bottom",
                    label: {
                        style: { color: "#17324d" }
                    }
                },
                legendGroup: {
                    layout: {
                        position: "bottom"
                    }
                },
                title: {
                    visible: false
                },
                valueAxis,
                valueAxis2: {
                    label: { style: { color: "#5d7187" } },
                    title: this._secondaryAxisTitle(chartId),
                    scale: this._secondaryAxisScale(chartId)
                },
                categoryAxis: {
                    label: {
                        style: { color: "#5d7187" },
                        rotation: "fixed",
                        angle: 45
                    },
                    title: { visible: false }
                },
                tooltip: {
                    visible: true
                },
                interaction: {
                    zoom: {
                        enablement: "enabled"
                    }
                }
            });

            this._connectChartPopover(chart, chartId);
        },

        _secondaryAxisTitle(chartId) {
            const titles = {
                samplingBaselineWeatherChart: "Water Usage (L)",
                anfisMoistureChart: "ANFIS Signal (%)",
                anfisContextChart: "Weather (mm / °C)",
                fuzzyMoistureChart: "Fuzzy Score (%)",
                fuzzyContextChart: "Weather (mm / °C)"
            };
            return titles[chartId] ? this._axisTitle(titles[chartId]) : { visible: false };
        },

        _primaryAxisScale(chartId) {
            if (!this._isMoistureChart(chartId)) {
                return null;
            }
            const domain = this._moistureAxisDomain(chartId, this._moistureThresholdPct(chartId));
            if (!domain) {
                return null;
            }
            return {
                fixedRange: true,
                minValue: domain.min,
                maxValue: domain.max
            };
        },

        _axisTitle(text) {
            return {
                visible: true,
                text,
                style: { color: "#5d7187" }
            };
        },

        _secondaryAxisScale(chartId) {
            if (chartId === "anfisMoistureChart" || chartId === "fuzzyMoistureChart") {
                return { fixedRange: true, minValue: 0, maxValue: 100 };
            }
            if (chartId === "samplingBaselineWeatherChart") {
                return { fixedRange: true, minValue: 0, maxValue: 250 };
            }
            return { fixedRange: true, minValue: 0, maxValue: 60 };
        },

        _applyChartFeedVisibility(chart, chartId, visibility) {
            if (!chart || !chart.getFeeds || !chart.addFeed) {
                return;
            }
            const feeds = chart && chart.getFeeds && chart.getFeeds();
            if (Array.isArray(feeds) && chart.removeFeed) {
                feeds.filter((feed) => {
                    const uid = feed && feed.getUid && feed.getUid();
                    return uid === "valueAxis" || uid === "valueAxis2";
                }).forEach((feed) => {
                    chart.removeFeed(feed);
                    feed.destroy();
                });
            }

            const axes = visibleChartMeasuresByAxis(chartId, visibility);
            if (axes.primaryAxis.length) {
                chart.addFeed(new FeedItem({
                    uid: "valueAxis",
                    type: "Measure",
                    values: axes.primaryAxis
                }));
            }
            if (axes.secondaryAxis.length) {
                chart.addFeed(new FeedItem({
                    uid: "valueAxis2",
                    type: "Measure",
                    values: axes.secondaryAxis
                }));
            }
        },

        _chartVisibility() {
            const model = this.getView().getModel();
            return model ? model.getProperty("/chartVisibility") : {};
        },

        _applyVisibleChartData(chartId) {
            const model = this.getView().getModel();
            if (!model) {
                return [];
            }

            const storedRows = model.getProperty(this._chartSourceDataPath(chartId));
            const currentRows = model.getProperty(this._chartDataPath(chartId)) || [];
            const sourceRows = Array.isArray(storedRows) && storedRows.length ? storedRows : currentRows;
            const visibleRows = withChartVisibility(sourceRows, this._chartVisibility());
            model.setProperty(this._chartDataPath(chartId), visibleRows);
            return visibleRows;
        },

        _applyExperimentChartData(experiment) {
            this._experimentChartIds(experiment).forEach((chartId) => this._applyVisibleChartData(chartId));
        },

        _refreshExperimentCharts(experiment) {
            this._experimentChartIds(experiment).forEach((chartId) => this._refreshChart(chartId));
        },

        _experimentChartIds(experiment) {
            const prefix = String(experiment || "");
            return EXPERIMENT_CHART_IDS.filter((chartId) => chartId.startsWith(prefix));
        },

        _chartSeriesStyleRules(chartId) {
            const measures = CHART_MEASURES[chartId] || [];
            return measures.map((measure) => {
                const color = chartMeasureColor(chartId, measure);
                return {
                    callback: (context) => this._chartMeasureName(chartId, context) === measure,
                    properties: this._chartSeriesColorProperties(color),
                    displayName: this._chartLegendLabel(measure, chartId)
                };
            });
        },

        _chartSeriesColorProperties(color) {
            return color ? { color, lineColor: color } : {};
        },

        _chartLegendLabel(measure, chartId) {
            const chartLabels = CHART_LEGEND_LABELS[chartId] || {};
            if (chartLabels[measure]) {
                return chartLabels[measure];
            }
            return LEGEND_LABELS[measure] || measure;
        },

        _isMoistureChart(chartId) {
            return chartId === "samplingMoistureChart"
                || chartId === "samplingBaselineWeatherChart"
                || chartId === "anfisMoistureChart"
                || chartId === "fuzzyMoistureChart";
        },

        _chartMeasureName(chartId, context) {
            if (!context) {
                return "";
            }
            const measures = CHART_MEASURES[chartId] || [];
            const measureKeys = [
                "MeasureNamesDimension",
                "measureNamesDimension",
                "Measure Names",
                "Measure",
                "measure",
                "MeasureName",
                "measureName"
            ];
            for (const key of measureKeys) {
                if (typeof context[key] === "string" && measures.includes(context[key])) {
                    return context[key];
                }
            }
            return measures.find((measure) => Object.values(context).includes(measure)) || "";
        },

        _chartRowLabel(row) {
            return row && (row.chart_label || row.day_label || row.timestamp || row.date || "");
        },

        _chartDataPath(chartId) {
            if (CHART_DATA_PATHS[chartId]) {
                return CHART_DATA_PATHS[chartId];
            }
            return {
                samplingChart: "/samplingChartEntries",
                anfisChart: "/anfisChartEntries",
                fuzzyChart: "/fuzzyChartEntries"
            }[chartId] || "/samplingChartEntries";
        },

        _chartSourceDataPath(chartId) {
            if (CHART_SOURCE_DATA_PATHS[chartId]) {
                return CHART_SOURCE_DATA_PATHS[chartId];
            }
            return {
                samplingChart: "/samplingChartAllEntries",
                anfisChart: "/anfisChartAllEntries",
                fuzzyChart: "/fuzzyChartAllEntries"
            }[chartId] || "/samplingChartAllEntries";
        },

        onChartRenderComplete(event) {
            const chart = event.getSource();
            const localChartId = chart.getId().split("--").pop();
            this._connectChartPopover(chart, localChartId);
            this._trackChartOverlay(chart, localChartId);
            this._trackChartWindowSync(chart, localChartId);
            this._scheduleInitialChartWindow(localChartId);
            this._scheduleChartOverlay(localChartId);
        },

        _refreshChart(chartId) {
            const chart = this.byId(chartId);
            if (chart) {
                setTimeout(() => {
                    this._styleChart(chart, chartId);
                    this._trackChartOverlay(chart, chartId);
                    this._trackChartWindowSync(chart, chartId);
                    this._scheduleInitialChartWindow(chartId);
                    this._scheduleChartOverlay(chartId);
                }, 0);
            }
        },

        _registerChartFormatters() {
            const formatter = ChartFormatter.getInstance();
            const formatNumber = (value) => {
                const numberValue = Number(value);
                return Number.isFinite(numberValue) ? numberValue.toFixed(2) : value;
            };

            formatter.registerCustomFormatter("DT_PERCENT", (value) => `${formatNumber(value)}%`);
            formatter.registerCustomFormatter("DT_ML", (value) => `${formatNumber(value)} mL`);
            formatter.registerCustomFormatter("DT_L", (value) => `${formatNumber(value)} L`);
            formatter.registerCustomFormatter("DT_CELSIUS", (value) => `${formatNumber(value)} C`);
            formatter.registerCustomFormatter("DT_MM", (value) => `${formatNumber(value)} mm`);
            formatter.registerCustomFormatter("DT_LM2", (value) => `${formatNumber(value)} L/m²`);
            formatter.registerCustomFormatter("DT_NUMBER", (value) => formatNumber(value));
            Format.numericFormatter(formatter);
        }

    });
});
