sap.ui.define([
    "sap/ui/core/mvc/Controller",
    "sap/ui/model/json/JSONModel",
    "sap/viz/ui5/format/ChartFormatter",
    "sap/viz/ui5/api/env/Format"
], (Controller, JSONModel, ChartFormatter, Format) => {
    "use strict";

    function getApiUrl(path) {
        return new URL(path, `${window.location.protocol}//${window.location.hostname}:8000`);
    }

    function getSharedSettingsKey(settings) {
        return [
            settings.steps || 1000,
            settings.threshold || 35,
            settings.hysteresis || 0,
            settings.flow_rate_ml || 10,
            settings.scenario_seed || 2026
        ].join("|");
    }

    function getCacheEntry(model, key) {
        const cache = model.getProperty("/resultCache") || {};
        return cache[key];
    }

    function setCacheEntry(model, key, result) {
        const cache = Object.assign({}, model.getProperty("/resultCache") || {});
        cache[key] = result;
        model.setProperty("/resultCache", cache);
    }

    const CHART_PALETTES = {
        baselineChart: ["#4d82d8", "#9dbef7"],
        samplingChart: ["#43bfd2", "#7fcf45"],
        anfisChart: ["#43bfd2", "#9dbef7", "#aee36d"]
    };

    const CHART_FORMATS = {
        baselineChart: {
            "Moisture": "DT_PERCENT",
            "Water Usage": "DT_ML"
        },
        samplingChart: {
            "Sparse Moisture": "DT_PERCENT",
            "Sparse Water Usage": "DT_ML"
        },
        anfisChart: {
            "ANFIS Moisture": "DT_PERCENT",
            "Predicted Probability": "DT_NUMBER",
            "ANFIS Water Usage": "DT_ML"
        }
    };

    return Controller.extend("disertatie.controller.View1", {
        onInit() {
            this._registerChartFormatters();

            const initialData = {
                helloMessage: "Welcome! Attempting to connect to DT service...",
                experimentSettings: {
                    steps: 1000,
                    threshold: 35,
                    hysteresis: 4,
                    flow_rate_ml: 10,
                    scenario_seed: 2026
                },
                samplingSettings: {
                    sample_interval: 10
                },
                baselineEntries: [],
                samplingEntries: [],
                anfisEntries: [],
                summary: {
                    totalEntries: 0,
                    irrigationEvents: 0,
                    irrigationSteps: 0,
                    totalWaterUsage: 0,
                    percentTimeIrrigated: 0
                },
                samplingSummary: {
                    steps: 0,
                    sample_interval: 10,
                    accuracy_percent: 0,
                    mismatch_steps: 0,
                    baseline_total_water_usage_l: 0,
                    sparse_total_water_usage_l: 0,
                    baseline_irrigation_event_count: 0,
                    sparse_irrigation_event_count: 0
                },
                anfisSummary: {
                    steps: 0,
                    baseline_irrigation_steps: 0,
                    anfis_irrigation_steps: 0,
                    baseline_total_water_usage_l: 0,
                    anfis_total_water_usage_l: 0,
                    baseline_irrigation_event_count: 0,
                    anfis_irrigation_event_count: 0,
                    test_mse: 0,
                    test_accuracy_percent: 0,
                    test_samples: 0,
                    execution_time_seconds: 0,
                    predicted_probability_mean: 0,
                    predicted_probability_min: 0,
                    predicted_probability_max: 0
                },
                activeExperiment: null,
                isLoading: false,
                isSamplingLoading: false,
                isAnfisLoading: false,
                resultCache: {}
            };

            const oModel = new JSONModel(initialData);
            this.getView().setModel(oModel);

            fetch(getApiUrl("/api/hello").toString())
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.message) {
                        oModel.setProperty("/helloMessage", result.message);
                    } else {
                        oModel.setProperty("/helloMessage", "DT service connected but returned unexpected response");
                    }
                })
                .catch(() => {
                    oModel.setProperty("/helloMessage", "DT service connection failed");
                });
        },

        onAfterRendering() {
            this._styleCharts();
        },

        _styleCharts() {
            ["baselineChart", "samplingChart", "anfisChart"].forEach((chartId) => {
                const chart = this.byId(chartId);
                if (chart) {
                    this._styleChart(chart, chartId);
                }
            });
        },

        _styleChart(chart, chartId) {
            const formatString = CHART_FORMATS[chartId] || [];

            chart.setVizProperties({
                plotArea: {
                    colorPalette: CHART_PALETTES[chartId],
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
                    visible: true,
                    label: {
                        style: {
                            color: "#17324d"
                        }
                    }
                },
                valueAxis: {
                    label: {
                        style: {
                            color: "#5d7187"
                        }
                    },
                    title: {
                        visible: false
                    }
                },
                categoryAxis: {
                    label: {
                        style: {
                            color: "#5d7187"
                        }
                    },
                    title: {
                        visible: false
                    }
                },
                title: {
                    visible: false
                },
                tooltip: {
                    visible: true,
                    formatString
                },
                interaction: {
                    behaviorType: null
                }
            });
        },

        _registerChartFormatters() {
            const formatter = ChartFormatter.getInstance();
            const formatNumber = (value) => {
                const numberValue = Number(value);
                return Number.isFinite(numberValue) ? numberValue.toFixed(2) : value;
            };

            formatter.registerCustomFormatter("DT_PERCENT", (value) => `${formatNumber(value)}%`);
            formatter.registerCustomFormatter("DT_ML", (value) => `${formatNumber(value)} mL`);
            formatter.registerCustomFormatter("DT_NUMBER", (value) => formatNumber(value / 100));
            Format.numericFormatter(formatter);
        },

        onRunExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            const cacheKey = `baseline|${getSharedSettingsKey(settings)}`;
            const cachedResult = getCacheEntry(model, cacheKey);

            model.setProperty("/activeExperiment", "baseline");
            if (cachedResult) {
                model.setProperty("/baselineEntries", cachedResult.entries);
                model.setProperty("/summary", cachedResult.summary);
                model.setProperty("/helloMessage", "Loaded cached threshold experiment");
                this._styleCharts();
                return;
            }

            model.setProperty("/helloMessage", "Running threshold experiment...");
            model.setProperty("/isLoading", true);
            model.setProperty("/baselineEntries", []);

            const url = getApiUrl("/api/experiment");
            url.searchParams.set("steps", settings.steps || 1000);
            url.searchParams.set("threshold", settings.threshold || 35);
            url.searchParams.set("hysteresis", settings.hysteresis || 0);
            url.searchParams.set("flow_rate_ml", settings.flow_rate_ml || 10);
            url.searchParams.set("seed", settings.scenario_seed || 2026);

            fetch(url.toString())
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        setCacheEntry(model, cacheKey, result);
                        model.setProperty("/baselineEntries", result.entries);
                        model.setProperty("/summary", result.summary);
                        model.setProperty(
                            "/helloMessage",
                            result.summary.cacheHit ? "Loaded cached threshold experiment" : "Threshold experiment completed"
                        );
                        this._styleCharts();
                    }
                    model.setProperty("/isLoading", false);
                })
                .catch(() => {
                    model.setProperty("/helloMessage", "Threshold experiment failed");
                    model.setProperty("/isLoading", false);
                });
        },

        onRunSamplingExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            const sampling = model.getProperty("/samplingSettings") || {};
            const cacheKey = `sampling|${getSharedSettingsKey(settings)}|${sampling.sample_interval || 10}`;
            const cachedResult = getCacheEntry(model, cacheKey);

            model.setProperty("/activeExperiment", "sampling");
            if (cachedResult) {
                model.setProperty("/samplingEntries", cachedResult.entries);
                model.setProperty("/samplingSummary", cachedResult.summary);
                model.setProperty("/helloMessage", "Loaded cached sampling experiment");
                this._styleCharts();
                return;
            }

            model.setProperty("/helloMessage", "Running sampling experiment...");
            model.setProperty("/isSamplingLoading", true);
            model.setProperty("/samplingEntries", []);

            const url = getApiUrl("/api/experiment/sampling");
            url.searchParams.set("steps", settings.steps || 1000);
            url.searchParams.set("sample_interval", sampling.sample_interval || 10);
            url.searchParams.set("threshold", settings.threshold || 35);
            url.searchParams.set("hysteresis", settings.hysteresis || 0);
            url.searchParams.set("flow_rate_ml", settings.flow_rate_ml || 10);
            url.searchParams.set("seed", settings.scenario_seed || 2026);

            fetch(url.toString())
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        setCacheEntry(model, cacheKey, result);
                        model.setProperty("/samplingEntries", result.entries);
                        model.setProperty("/samplingSummary", result.summary);
                        model.setProperty(
                            "/helloMessage",
                            result.summary.cacheHit ? "Loaded cached sampling experiment" : "Sampling experiment completed"
                        );
                        this._styleCharts();
                    }
                    model.setProperty("/isSamplingLoading", false);
                })
                .catch(() => {
                    model.setProperty("/helloMessage", "Sampling experiment failed");
                    model.setProperty("/isSamplingLoading", false);
                });
        },

        onRunAnfisExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            const cacheKey = `anfis|${getSharedSettingsKey(settings)}|500|200`;
            const cachedResult = getCacheEntry(model, cacheKey);

            model.setProperty("/activeExperiment", "anfis");
            if (cachedResult) {
                model.setProperty("/anfisEntries", cachedResult.entries);
                model.setProperty("/anfisSummary", cachedResult.summary);
                model.setProperty("/helloMessage", "Loaded cached ANFIS experiment");
                model.refresh(true);
                this._styleCharts();
                return;
            }

            model.setProperty("/helloMessage", "Running ANFIS experiment...");
            model.setProperty("/isAnfisLoading", true);
            model.setProperty("/anfisEntries", []);

            const url = getApiUrl("/api/experiment/anfis");
            url.searchParams.set("steps", settings.steps || 1000);
            url.searchParams.set("train_samples", 500);
            url.searchParams.set("test_samples", 200);
            url.searchParams.set("threshold", settings.threshold || 35);
            url.searchParams.set("hysteresis", settings.hysteresis || 0);
            url.searchParams.set("flow_rate_ml", settings.flow_rate_ml || 10);
            url.searchParams.set("seed", settings.scenario_seed || 2026);

            fetch(url.toString())
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        setCacheEntry(model, cacheKey, result);
                        model.setProperty("/anfisEntries", result.entries);
                        model.setProperty("/anfisSummary", result.summary);
                        model.setProperty(
                            "/helloMessage",
                            result.summary.cacheHit ? "Loaded cached ANFIS experiment" : "ANFIS experiment completed"
                        );
                        model.refresh(true);
                        this._styleCharts();
                    }
                    model.setProperty("/isAnfisLoading", false);
                })
                .catch(() => {
                    model.setProperty("/helloMessage", "ANFIS experiment failed");
                    model.setProperty("/isAnfisLoading", false);
                });
        }
    });
});
