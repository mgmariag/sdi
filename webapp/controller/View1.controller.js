sap.ui.define([
    "sap/ui/core/mvc/Controller",
    "sap/ui/model/json/JSONModel",
    "sap/viz/ui5/format/ChartFormatter",
    "sap/viz/ui5/api/env/Format",
    "sap/viz/ui5/controls/Popover",
    "sap/m/Dialog",
    "sap/m/Button",
    "sap/m/Table",
    "sap/m/Column",
    "sap/m/ColumnListItem",
    "sap/m/Text"
], (Controller, JSONModel, ChartFormatter, Format, Popover, Dialog, Button, Table, Column, ColumnListItem, Text) => {
    "use strict";

    const REAL_FORECAST_HORIZON_DAYS = 15;

    function getApiUrl(path) {
        if (window.__DT_API_BASE_URL) {
            return new URL(path, window.__DT_API_BASE_URL);
        }

        if (window.location.port !== "8080" && window.location.port !== "8081") {
            return new URL(path, window.location.origin);
        }

        return new URL(path, `${window.location.protocol}//${window.location.hostname}:8000`);
    }

    function toChartNumber(value) {
        const numberValue = Number(value);
        return Number.isFinite(numberValue) ? numberValue : 0;
    }

    function withScaledChartWater(entries, sourceKey, targetKey) {
        const rows = Array.isArray(entries) ? entries : [];
        const maxWater = rows.reduce((maxValue, entry) => {
            return Math.max(maxValue, toChartNumber(entry[sourceKey]));
        }, 0);

        return rows.map((entry) => {
            const chartValue = maxWater > 0 ? (toChartNumber(entry[sourceKey]) / maxWater) * 20 : 0;
            return Object.assign({}, entry, {
                chart_label: entry.chart_label || entry.day_label || entry.timestamp || "",
                [targetKey]: Number(chartValue.toFixed(2))
            });
        });
    }

    function prepareChartResult(result, sourceKey, targetKey) {
        const entries = Array.isArray(result.entries) ? result.entries : [];
        const chartEntries = Array.isArray(result.chartEntries) && result.chartEntries.length > 0 ? result.chartEntries : entries;
        const usesDetailRows = result.summary && result.summary.chartGranularity && result.summary.chartGranularity !== "daily";
        const tableEntries = usesDetailRows ? chartEntries : entries;
        return Object.assign({}, result, {
            entries,
            chartEntries: withScaledChartWater(chartEntries, sourceKey, targetKey),
            tableEntries,
            pots: Array.isArray(result.pots) ? result.pots : []
        });
    }

    function parseLocalDate(value) {
        const parts = String(value || "").split("-").map((part) => Number(part));
        if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) {
            return null;
        }
        return new Date(parts[0], parts[1] - 1, parts[2]);
    }

    function formatLocalDate(date) {
        if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
            return "";
        }
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    function addDays(date, days) {
        const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
        copy.setDate(copy.getDate() + days);
        return copy;
    }

    function addMonths(date, months) {
        const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
        const originalDay = copy.getDate();
        copy.setDate(1);
        copy.setMonth(copy.getMonth() + months);
        const lastDay = new Date(copy.getFullYear(), copy.getMonth() + 1, 0).getDate();
        copy.setDate(Math.min(originalDay, lastDay));
        return copy;
    }

    function fetchJson(url, options) {
        return fetch(url, options).then((response) => {
            return response.json()
                .catch(() => ({}))
                .then((body) => {
                    if (!response.ok) {
                        const error = new Error(body && body.detail && body.detail.message ? body.detail.message : response.statusText);
                        error.status = response.status;
                        error.detail = body && body.detail;
                        throw error;
                    }
                    return body;
                });
        });
    }

    function formatWeatherRange(range) {
        if (!range) {
            return null;
        }
        return `${range.start} to ${range.end}`;
    }

    function experimentRange(settings) {
        const fallbackEnd = new Date();
        return {
            start: settings.start_date || formatLocalDate(addMonths(fallbackEnd, -1)),
            end: settings.end_date || formatLocalDate(fallbackEnd)
        };
    }

    function weatherRangeKey(settings) {
        const range = experimentRange(settings || {});
        return `${range.start}|${range.end}`;
    }

    const CHART_PALETTES = {
        baselineChart: ["#4d82d8", "#9dbef7"],
        samplingChart: ["#4d82d8", "#43bfd2", "#7fcf45"],
        anfisChart: ["#43bfd2", "#9dbef7", "#aee36d"]
    };

    const CHART_FORMATS = {
        baselineChart: {
            "Average Moisture": "DT_PERCENT",
            "Water Usage (scaled)": "DT_NUMBER"
        },
        samplingChart: {
            "Baseline Moisture": "DT_PERCENT",
            "Sparse Moisture": "DT_PERCENT",
            "Sparse Water Usage (scaled)": "DT_NUMBER"
        },
        anfisChart: {
            "ANFIS Moisture": "DT_PERCENT",
            "Predicted Probability": "DT_PERCENT",
            "ANFIS Water Usage (scaled)": "DT_NUMBER"
        }
    };

    return Controller.extend("disertatie.controller.View1", {
        onInit() {
            this._chartPopovers = {};
            this._sensorPlacementDialog = null;
            this._weatherUnavailableByRange = {};
            this._precomputeStartedByRange = {};
            this._registerChartFormatters();

            const initialData = {
                helloMessage: "Welcome! Attempting to connect to DT service...",
                experimentSettings: {
                    start_date: "",
                    end_date: ""
                },
                samplingSettings: {
                    sample_interval_hours: 72
                },
                sensorSettings: {
                    sensor_count: 4
                },
                rangeAlert: {
                    visible: false,
                    text: ""
                },
                weatherAvailability: {
                    maxWeatherDate: null
                },
                baselineEntries: [],
                baselineChartEntries: [],
                baselinePots: [],
                sensorPlacements: [],
                sensorPlacementSummary: {
                    sensor_count: 0,
                    active_pot_count: 0,
                    updated_at: null
                },
                samplingEntries: [],
                samplingChartEntries: [],
                samplingPots: [],
                anfisEntries: [],
                anfisChartEntries: [],
                anfisPots: [],
                summary: {
                    totalEntries: 0,
                    daysAnalyzed: 0,
                    potsAnalyzed: 0,
                    irrigationEvents: 0,
                    irrigationDecisions: 0,
                    totalWaterUsage: 0,
                    averageDailyWaterUsage: 0,
                    emergencyAlerts: 0,
                    wetAlerts: 0
                },
                samplingSummary: {
                    totalEntries: 0,
                    daysAnalyzed: 0,
                    potsAnalyzed: 0,
                    sample_interval_days: 3,
                    sample_interval_hours: 72,
                    accuracy_percent: 0,
                    mismatch_days: 0,
                    baseline_total_water_usage_l: 0,
                    sparse_total_water_usage_l: 0,
                    baseline_irrigation_event_count: 0,
                    sparse_irrigation_event_count: 0,
                    baseline_irrigation_decisions: 0,
                    sparse_irrigation_decisions: 0
                },
                anfisSummary: {
                    totalEntries: 0,
                    daysAnalyzed: 0,
                    potsAnalyzed: 0,
                    baseline_irrigation_days: 0,
                    anfis_irrigation_days: 0,
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
                isSensorPlacementLoading: false,
                isSamplingLoading: false,
                isAnfisLoading: false
            };

            const oModel = new JSONModel(initialData);
            this.getView().setModel(oModel);
            this._sensorPlacementReady = this._loadSensorPlacements(oModel);
            this._loadWeatherAvailability(oModel);

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

        _loadWeatherAvailability(model) {
            fetch(getApiUrl("/api/weather/cluj-napoca/summary").toString())
                .then((response) => response.json())
                .then((result) => {
                    const rows = Array.isArray(result.hourly_weather) ? result.hourly_weather : [];
                    const maxDate = rows.reduce((currentMax, row) => {
                        const timestamp = row.last_timestamp ? new Date(row.last_timestamp) : null;
                        if (!timestamp || Number.isNaN(timestamp.getTime())) {
                            return currentMax;
                        }
                        return !currentMax || timestamp > currentMax ? timestamp : currentMax;
                    }, null);
                    if (maxDate) {
                        const endDate = formatLocalDate(maxDate);
                        const startDate = formatLocalDate(addMonths(maxDate, -1));
                        const settings = model.getProperty("/experimentSettings") || {};
                        model.setProperty("/weatherAvailability/maxWeatherDate", endDate);
                        if (!settings.start_date) {
                            model.setProperty("/experimentSettings/start_date", startDate);
                        }
                        if (!settings.end_date) {
                            model.setProperty("/experimentSettings/end_date", endDate);
                        }
                        this._precomputeExperiments(model, startDate, endDate);
                    }
                })
                .catch(() => {
                    model.setProperty("/weatherAvailability/maxWeatherDate", null);
                });
        },

        _precomputeExperiments(model, startDate, endDate) {
            if (!startDate || !endDate) {
                return;
            }

            const sampling = model.getProperty("/samplingSettings") || {};
            const settings = model.getProperty("/experimentSettings") || {};
            const sensors = model.getProperty("/sensorSettings") || {};
            const sampleIntervalHours = Math.min(336, Math.max(1, Math.floor(Number(sampling.sample_interval_hours) || 72)));
            model.setProperty("/samplingSettings/sample_interval_hours", sampleIntervalHours);

            const cacheKey = `${startDate}|${endDate}|${sampleIntervalHours}|${sensors.sensor_count || 4}`;
            if (this._precomputeStartedByRange[cacheKey]) {
                return;
            }

            this._precomputeStartedByRange[cacheKey] = true;

            const url = getApiUrl("/api/experiment/precompute");
            url.searchParams.set("start", startDate);
            url.searchParams.set("end", endDate);
            url.searchParams.set("sample_interval_hours", sampleIntervalHours);
            url.searchParams.set("train_samples", 500);
            url.searchParams.set("test_samples", 200);
            url.searchParams.set("seed", settings.scenario_seed || 2026);

            const placementReady = this._sensorPlacementReady || Promise.resolve();
            placementReady
                .then(() => fetchJson(url.toString(), { method: "POST" }))
                .catch(() => {
                    delete this._precomputeStartedByRange[cacheKey];
                });
        },

        _loadSensorPlacements(model) {
            return fetchJson(getApiUrl("/api/sensors/placements").toString())
                .then((result) => {
                    if (result && Array.isArray(result.items) && result.items.length > 0) {
                        this._setSensorPlacementData(model, result);
                        return result;
                    }
                    return this._ensureSensorPlacements(model);
                })
                .catch(() => undefined);
        },

        _syncSensorPlacements(model, silent) {
            const count = this._normalizedSensorCount(model);
            const url = getApiUrl("/api/sensors/placements/ensure");
            url.searchParams.set("count", count);

            model.setProperty("/isSensorPlacementLoading", true);
            return fetchJson(url.toString(), { method: "POST" })
                .then((result) => {
                    this._setSensorPlacementData(model, result);
                    if (!silent) {
                        model.setProperty("/helloMessage", `Sensor locations ready (${result.items.length} selected)`);
                    }
                    return result;
                })
                .catch((error) => {
                    if (!silent) {
                        model.setProperty("/helloMessage", "Sensor location proposal failed");
                    }
                    throw error;
                })
                .finally(() => {
                    model.setProperty("/isSensorPlacementLoading", false);
                });
        },

        _ensureSensorPlacements(model) {
            const count = this._normalizedSensorCount(model);
            const items = model.getProperty("/sensorPlacements") || [];
            const storedCount = Number(model.getProperty("/sensorPlacementSummary/sensor_count"));
            if (Array.isArray(items) && items.length > 0 && storedCount === count) {
                return Promise.resolve({ items });
            }
            return this._syncSensorPlacements(model, true);
        },

        _normalizedSensorCount(model) {
            const settings = model.getProperty("/sensorSettings") || {};
            const count = Math.max(1, Math.floor(Number(settings.sensor_count) || 4));
            model.setProperty("/sensorSettings/sensor_count", count);
            return count;
        },

        _setSensorPlacementData(model, result) {
            const items = Array.isArray(result.items) ? result.items : [];
            model.setProperty("/sensorPlacements", items);
            model.setProperty("/sensorPlacementSummary", {
                sensor_count: result.sensor_count || items.length,
                active_pot_count: result.active_pot_count || 0,
                updated_at: result.updated_at || null
            });
            if (items.length > 0) {
                model.setProperty("/sensorSettings/sensor_count", items.length);
            }
        },

        _getSensorPlacementDialog() {
            if (this._sensorPlacementDialog) {
                return this._sensorPlacementDialog;
            }

            const table = new Table({
                growing: true,
                growingThreshold: 20,
                items: {
                    path: "/sensorPlacements",
                    template: new ColumnListItem({
                        cells: [
                            new Text({ text: "{rank}" }),
                            new Text({ text: "{pot_code}" }),
                            new Text({ text: "{pot_label}" }),
                            new Text({ text: "{balcony_zone}" }),
                            new Text({ text: "{sun_exposure}" }),
                            new Text({ text: "{size_class}" }),
                            new Text({ text: "{plant_type_label}" }),
                            new Text({ text: "{score}" }),
                            new Text({ text: "{reason}" })
                        ]
                    })
                }
            });
            [
                ["Rank", "4rem"],
                ["Code", "6rem"],
                ["Pot", "12rem"],
                ["Zone", "9rem"],
                ["Sun", "8rem"],
                ["Size", "7rem"],
                ["Plant", "9rem"],
                ["Score", "5rem"],
                ["Reason", "18rem"]
            ].forEach(([label, width]) => {
                table.addColumn(new Column({ width, header: new Text({ text: label }) }));
            });

            this._sensorPlacementDialog = new Dialog({
                title: "Sensor locations",
                contentWidth: "72rem",
                contentHeight: "30rem",
                resizable: true,
                draggable: true,
                content: [table],
                endButton: new Button({
                    text: "Close",
                    press: () => this._sensorPlacementDialog.close()
                })
            });
            this.getView().addDependent(this._sensorPlacementDialog);
            return this._sensorPlacementDialog;
        },

        onSensorLocationHelp() {
            const model = this.getView().getModel();
            this._ensureSensorPlacements(model)
                .catch(() => {
                    model.setProperty("/helloMessage", "Sensor location proposal failed");
                    return this._loadSensorPlacements(model);
                })
                .finally(() => {
                    this._getSensorPlacementDialog().open();
                });
        },

        _getMaxStoredWeatherDate(model) {
            const storedMaxDate = parseLocalDate(model.getProperty("/weatherAvailability/maxWeatherDate"));
            if (storedMaxDate) {
                return storedMaxDate;
            }
            return addDays(new Date(), REAL_FORECAST_HORIZON_DAYS);
        },

        _setRangeDataAlertForSettings(model, settings) {
            const range = experimentRange(settings || {});
            const startDate = parseLocalDate(range.start);
            const endDate = parseLocalDate(range.end);
            const maxWeatherDate = this._getMaxStoredWeatherDate(model);
            if (!startDate || !endDate || endDate <= maxWeatherDate) {
                this._clearRangeDataAlert(model);
                return;
            }

            const maxWeatherLabel = formatLocalDate(maxWeatherDate);
            const selectedRange = `${range.start} to ${range.end}`;
            const text = startDate > maxWeatherDate
                ? `No stored weather data is available for ${selectedRange}. Estimated weather and soil state will be generated during the experiment.`
                : `Stored weather data is available through ${maxWeatherLabel}. Estimated weather and soil state will be generated for the rest of ${selectedRange} during the experiment.`;
            model.setProperty("/rangeAlert", {
                visible: true,
                text
            });
        },

        _setRangeDataAlertFromSummary(model, summary) {
            const estimatedRows = Number(summary && summary.dbSnapshotEstimatedWeatherRows);
            if (!Number.isFinite(estimatedRows) || estimatedRows <= 0) {
                this._clearRangeDataAlert(model);
                return;
            }

            model.setProperty("/rangeAlert", {
                visible: true,
                text: `Stored weather data was unavailable for part or all of this range. The experiment generated ${estimatedRows} estimated hourly weather rows and simulated the soil state from the latest known sensor readings.`
            });
        },

        _setWeatherUnavailableAlert(model, detail) {
            const requested = detail && detail.requestedRange
                ? `${detail.requestedRange.start} to ${detail.requestedRange.end}`
                : "the selected period";
            const lowerRange = formatWeatherRange(detail && detail.closestLowerRange);
            const higherRange = formatWeatherRange(detail && detail.closestHigherRange);
            const ranges = [];

            ranges.push(`No stored historical weather data is available for ${requested}.`);
            ranges.push(lowerRange ? `Closest earlier stored weather range: ${lowerRange}.` : "No earlier stored weather range is available.");
            ranges.push(higherRange ? `Closest later stored weather range: ${higherRange}.` : "No later stored weather range is available.");
            ranges.push("Import weather for this period or choose one of the available ranges.");

            model.setProperty("/rangeAlert", {
                visible: true,
                text: ranges.join(" ")
            });
        },

        _showKnownWeatherUnavailable(model, settings, fallbackMessage) {
            const detail = this._weatherUnavailableByRange[weatherRangeKey(settings)];
            if (!detail) {
                return false;
            }
            model.setProperty("/helloMessage", fallbackMessage);
            this._setWeatherUnavailableAlert(model, detail);
            return true;
        },

        _handleExperimentError(model, fallbackMessage, error, settings) {
            model.setProperty("/helloMessage", fallbackMessage);
            if (error && error.detail && error.detail.code === "weather_data_unavailable") {
                if (settings) {
                    this._weatherUnavailableByRange[weatherRangeKey(settings)] = error.detail;
                }
                this._setWeatherUnavailableAlert(model, error.detail);
            }
        },

        _clearRangeDataAlert(model) {
            model.setProperty("/rangeAlert", {
                visible: false,
                text: ""
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
            const formatString = CHART_FORMATS[chartId] || {};

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
                        style: { color: "#17324d" }
                    }
                },
                title: {
                    visible: false
                },
                valueAxis: {
                    label: { style: { color: "#5d7187" } },
                    title: { visible: false }
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
                }
            });

            this._connectChartPopover(chart, chartId);
        },

        _connectChartPopover(chart, chartId) {
            const vizUid = chart.getVizUid && chart.getVizUid();
            if (!vizUid) {
                return;
            }

            if (!this._chartPopovers[chartId]) {
                this._chartPopovers[chartId] = new Popover({});
                this.getView().addDependent(this._chartPopovers[chartId]);
            }

            this._chartPopovers[chartId].connect(vizUid);
        },

        onChartRenderComplete(event) {
            const chart = event.getSource();
            const localChartId = chart.getId().split("--").pop();
            this._connectChartPopover(chart, localChartId);
        },

        _refreshChart(chartId) {
            const chart = this.byId(chartId);
            if (chart) {
                setTimeout(() => {
                    this._styleChart(chart, chartId);
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
            formatter.registerCustomFormatter("DT_NUMBER", (value) => formatNumber(value));
            Format.numericFormatter(formatter);
        },

        onRunExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};

            model.setProperty("/activeExperiment", "baseline");
            this._setRangeDataAlertForSettings(model, settings);
            model.setProperty("/baselineEntries", []);
            model.setProperty("/baselineChartEntries", []);
            model.setProperty("/baselinePots", []);
            if (this._showKnownWeatherUnavailable(model, settings, "Baseline experiment failed")) {
                return;
            }

            model.setProperty("/helloMessage", "Updating sensor locations...");
            model.setProperty("/isLoading", true);

            this._ensureSensorPlacements(model)
                .then(() => {
                    model.setProperty("/helloMessage", "Running baseline experiment...");
                    const url = getApiUrl("/api/experiment");
                    const range = experimentRange(settings);
                    url.searchParams.set("start", range.start);
                    url.searchParams.set("end", range.end);
                    return fetchJson(url.toString());
                })
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        const preparedResult = prepareChartResult(result, "water_usage_l", "water_usage_chart");
                        model.setProperty("/baselineEntries", preparedResult.tableEntries);
                        model.setProperty("/baselineChartEntries", preparedResult.chartEntries);
                        model.setProperty("/baselinePots", preparedResult.pots);
                        model.setProperty("/summary", preparedResult.summary);
                        this._setRangeDataAlertFromSummary(model, preparedResult.summary);
                        model.refresh(true);
                        model.setProperty(
                            "/helloMessage",
                            preparedResult.summary.cacheHit ? "Loaded cached baseline experiment" : `Baseline experiment completed (${preparedResult.entries.length} days loaded)`
                        );
                        this._refreshChart("baselineChart");
                    }
                })
                .catch((error) => {
                    this._handleExperimentError(model, "Baseline experiment failed", error, settings);
                })
                .finally(() => {
                    model.setProperty("/isLoading", false);
                });
        },

        onRunSamplingExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            const sampling = model.getProperty("/samplingSettings") || {};
            const sampleIntervalHours = Math.min(336, Math.max(1, Math.floor(Number(sampling.sample_interval_hours) || 72)));
            model.setProperty("/samplingSettings/sample_interval_hours", sampleIntervalHours);

            model.setProperty("/activeExperiment", "sampling");
            this._setRangeDataAlertForSettings(model, settings);
            model.setProperty("/samplingEntries", []);
            model.setProperty("/samplingChartEntries", []);
            model.setProperty("/samplingPots", []);
            if (this._showKnownWeatherUnavailable(model, settings, "Sampling experiment failed")) {
                return;
            }

            model.setProperty("/helloMessage", "Updating sensor locations...");
            model.setProperty("/isSamplingLoading", true);

            this._ensureSensorPlacements(model)
                .then(() => {
                    model.setProperty("/helloMessage", "Running database sampling experiment...");
                    const url = getApiUrl("/api/experiment/sampling");
                    const range = experimentRange(settings);
                    url.searchParams.set("start", range.start);
                    url.searchParams.set("end", range.end);
                    url.searchParams.set("sample_interval_hours", sampleIntervalHours);
                    return fetchJson(url.toString());
                })
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        const preparedResult = prepareChartResult(result, "sparse_water_usage_l", "sparse_water_usage_chart");
                        model.setProperty("/samplingEntries", preparedResult.tableEntries);
                        model.setProperty("/samplingChartEntries", preparedResult.chartEntries);
                        model.setProperty("/samplingPots", preparedResult.pots);
                        model.setProperty("/samplingSummary", preparedResult.summary);
                        this._setRangeDataAlertFromSummary(model, preparedResult.summary);
                        model.refresh(true);
                        model.setProperty(
                            "/helloMessage",
                            preparedResult.summary.cacheHit ? "Loaded cached sampling experiment" : `Database sampling experiment completed (${preparedResult.entries.length} days loaded)`
                        );
                        this._refreshChart("samplingChart");
                    }
                })
                .catch((error) => {
                    this._handleExperimentError(model, "Sampling experiment failed", error, settings);
                })
                .finally(() => {
                    model.setProperty("/isSamplingLoading", false);
                });
        },

        onRunAnfisExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};

            model.setProperty("/activeExperiment", "anfis");
            this._setRangeDataAlertForSettings(model, settings);
            model.setProperty("/anfisEntries", []);
            model.setProperty("/anfisChartEntries", []);
            model.setProperty("/anfisPots", []);
            if (this._showKnownWeatherUnavailable(model, settings, "ANFIS experiment failed")) {
                return;
            }

            model.setProperty("/helloMessage", "Updating sensor locations...");
            model.setProperty("/isAnfisLoading", true);

            this._ensureSensorPlacements(model)
                .then(() => {
                    model.setProperty("/helloMessage", "Running database ANFIS experiment...");
                    const url = getApiUrl("/api/experiment/anfis");
                    const range = experimentRange(settings);
                    url.searchParams.set("start", range.start);
                    url.searchParams.set("end", range.end);
                    url.searchParams.set("train_samples", 500);
                    url.searchParams.set("test_samples", 200);
                    url.searchParams.set("seed", settings.scenario_seed || 2026);
                    return fetchJson(url.toString());
                })
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        const preparedResult = prepareChartResult(result, "anfis_water_usage_l", "anfis_water_usage_chart");
                        model.setProperty("/anfisEntries", preparedResult.tableEntries);
                        model.setProperty("/anfisChartEntries", preparedResult.chartEntries);
                        model.setProperty("/anfisPots", preparedResult.pots);
                        model.setProperty("/anfisSummary", preparedResult.summary);
                        this._setRangeDataAlertFromSummary(model, preparedResult.summary);
                        model.setProperty(
                            "/helloMessage",
                            preparedResult.summary.cacheHit ? "Loaded cached ANFIS experiment" : `Database ANFIS experiment completed (${preparedResult.entries.length} days loaded)`
                        );
                        model.refresh(true);
                        this._refreshChart("anfisChart");
                    }
                })
                .catch((error) => {
                    this._handleExperimentError(model, "ANFIS experiment failed", error, settings);
                })
                .finally(() => {
                    model.setProperty("/isAnfisLoading", false);
                });
        },

    });
});
