sap.ui.define([
    "sap/ui/core/mvc/Controller",
    "sap/ui/model/json/JSONModel",
    "disertatie/model/apiClient",
    "disertatie/model/chartBuilder",
    "disertatie/model/formatter",
    "disertatie/model/experimentMapper",
    "disertatie/model/dialogBuilder",
    "disertatie/model/sensorPlacementBuilder",
    "disertatie/model/summaryCards",
    "disertatie/model/chartRuntime"
], (Controller, JSONModel, ApiClient, ChartBuilder, Formatter, ExperimentMapper, DialogBuilder, SensorPlacementBuilder, SummaryCards, ChartRuntime) => {
    "use strict";

    const { fetchJson, getApiUrl } = ApiClient;
    const { prepareChartResult } = ChartBuilder;
    const {
        addDays,
        defaultOverview,
        formatLocalDate,
        overviewUnavailable,
        parseLocalDate,
        prepareOverview
    } = Formatter;
    const {
        REAL_FORECAST_HORIZON_DAYS,
        defaultAnfisSummary,
        defaultExperimentFooter,
        defaultExperimentRange,
        defaultFuzzySummary,
        defaultSamplingSummary,
        experimentRange,
        formatWeatherRange,
        RECOMMENDED_SAMPLING_INTERVAL_HOURS,
        weatherRangeKey
    } = ExperimentMapper;

    return Controller.extend("disertatie.controller.View1", Object.assign({}, ChartRuntime, {
        onInit() {
            this._chartPopovers = {};
            this._chartWindowTimers = {};
            this._chartOverlayTimers = {};
            this._chartOverlayObservers = {};
            this._chartOverlayListenerCleanups = {};
            this._appliedChartWindowSignatures = {};
            this._overviewRefreshTimer = null;
            this._overviewRefreshHandler = null;
            this._sensorPlacementDialog = null;
            this._weatherUnavailableByRange = {};
            this._precomputeStartedByRange = {};
            // Experiment result caching is temporarily disabled.
            // this._experimentResultCache = {};
            this._registerChartFormatters();
            const defaultRange = defaultExperimentRange();

            const initialData = {
                helloMessage: "Welcome! Attempting to connect to irrigation service...",
                experimentSettings: {
                    start_date: defaultRange.start,
                    end_date: defaultRange.end
                },
                samplingSettings: {
                    sample_interval_hours: RECOMMENDED_SAMPLING_INTERVAL_HOURS
                },
                sensorSettings: {
                    sensor_count: null
                },
                rangeAlert: {
                    visible: false,
                    text: ""
                },
                overview: defaultOverview(),
                weatherAvailability: {
                    maxWeatherDate: null
                },
                chartVisibility: {
                    baseline: true,
                    weather: true
                },
                experimentFooter: defaultExperimentFooter(),
                sensorPlacements: [],
                sensorPlacementSummary: {
                    sensor_count: null,
                    minimum_sensor_count: 5,
                    valve_count: 5,
                    has_all_valve_zones: false,
                    stored_sensor_count: 0,
                    sensor_reading_pot_count: 0,
                    active_pot_count: 0,
                    updated_at: null,
                    loaded: false
                },
                samplingEntries: [],
                samplingChartAllEntries: [],
                samplingChartEntries: [],
                samplingPots: [],
                anfisEntries: [],
                anfisChartAllEntries: [],
                anfisChartEntries: [],
                anfisPots: [],
                fuzzyEntries: [],
                fuzzyChartAllEntries: [],
                fuzzyChartEntries: [],
                fuzzyPots: [],
                samplingSummary: defaultSamplingSummary(RECOMMENDED_SAMPLING_INTERVAL_HOURS),
                anfisSummary: defaultAnfisSummary(),
                fuzzySummary: defaultFuzzySummary(),
                activeExperiment: null,
                isSensorPlacementLoading: false,
                isSamplingLoading: false,
                isAnfisLoading: false,
                isFuzzyLoading: false
            };

            const oModel = new JSONModel(initialData);
            this.getView().setModel(oModel);
            this._sensorPlacementReady = this._loadSensorPlacements(oModel);
            this._loadWeatherAvailability(oModel);
            this._loadOverview(oModel);
            this._startOverviewRefresh(oModel);

            fetch(getApiUrl("/api/hello").toString())
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.message) {
                        oModel.setProperty("/helloMessage", result.message);
                    } else {
                        oModel.setProperty("/helloMessage", "Irrigation service connected but returned unexpected response");
                    }
                })
                .catch(() => {
                    oModel.setProperty("/helloMessage", "Irrigation service connection failed");
                });
        },

        onExit() {
            if (this._overviewRefreshTimer) {
                clearInterval(this._overviewRefreshTimer);
                this._overviewRefreshTimer = null;
            }
            if (this._overviewRefreshHandler) {
                document.removeEventListener("visibilitychange", this._overviewRefreshHandler);
                this._overviewRefreshHandler = null;
            }
        },

        _startOverviewRefresh(model) {
            this._overviewRefreshHandler = () => {
                if (!document.hidden) {
                    this._refreshOverviewIfVisible(model);
                }
            };
            document.addEventListener("visibilitychange", this._overviewRefreshHandler);
            this._overviewRefreshTimer = setInterval(() => {
                this._refreshOverviewIfVisible(model);
            }, 30000);
        },

        _refreshOverviewIfVisible(model) {
            if (!model || model.getProperty("/activeExperiment")) {
                return undefined;
            }
            return this._loadOverview(model);
        },

        _loadOverview(model) {
            const url = getApiUrl("/api/overview");
            url.searchParams.set("_", String(Date.now()));
            return fetchJson(url.toString(), { cache: "no-store" })
                .then((result) => {
                    model.setProperty("/overview", prepareOverview(result));
                    this._updateExperimentFooter(model);
                })
                .catch((error) => {
                    model.setProperty("/overview", overviewUnavailable(error));
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
                        // const settings = model.getProperty("/experimentSettings") || {};
                        model.setProperty("/weatherAvailability/maxWeatherDate", endDate);
                        // const range = experimentRange(settings);
                        // Experiment cache precompute is temporarily disabled.
                        // this._precomputeExperiments(model, range.start, range.end);
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

            const placementReady = this._sensorPlacementReady || Promise.resolve();
            placementReady
                .then(() => {
                    const sampling = model.getProperty("/samplingSettings") || {};
                    const settings = model.getProperty("/experimentSettings") || {};
                    const sampleIntervalHours = Math.min(336, Math.max(1, Math.floor(Number(sampling.sample_interval_hours) || RECOMMENDED_SAMPLING_INTERVAL_HOURS)));
                    const sensorCount = this._normalizedSensorCount(model);
                    const cacheKey = `${startDate}|${endDate}|${sampleIntervalHours}|${sensorCount}`;
                    if (this._precomputeStartedByRange[cacheKey]) {
                        return undefined;
                    }

                    this._precomputeStartedByRange[cacheKey] = true;
                    model.setProperty("/samplingSettings/sample_interval_hours", sampleIntervalHours);

                    const url = getApiUrl("/api/experiment/precompute");
                    url.searchParams.set("start", startDate);
                    url.searchParams.set("end", endDate);
                    url.searchParams.set("sample_interval_hours", sampleIntervalHours);
                    url.searchParams.set("seed", settings.scenario_seed || 2026);
                    return fetchJson(url.toString(), { method: "POST" })
                        .catch((error) => {
                            delete this._precomputeStartedByRange[cacheKey];
                            throw error;
                        });
                })
                .catch(() => {
                    // Precompute is opportunistic; explicit experiment runs still report errors.
                });
        },

        _loadSensorPlacements(model) {
            return SensorPlacementBuilder.load(model);
        },

        _syncSensorPlacements(model, silent) {
            return SensorPlacementBuilder.sync(model, silent);
        },

        _ensureSensorPlacements(model) {
            return SensorPlacementBuilder.ensure(this, model);
        },

        _normalizedSensorCount(model) {
            return SensorPlacementBuilder.normalizedSensorCount(model);
        },

        _setSensorPlacementData(model, result) {
            SensorPlacementBuilder.setSensorPlacementData(model, result);
        },

        _updateExperimentFooter(model, loadedFromCache) {
            SummaryCards.updateExperimentFooter(model, loadedFromCache);
            this._ensureExperimentContext(model);
        },

        _ensureExperimentContext(model) {
            if (
                !model
                || !model.getProperty("/activeExperiment")
                || model.getProperty("/overview/experimentSideRailHtml")
                || this._experimentOverviewLoadPending
            ) {
                return;
            }

            this._experimentOverviewLoadPending = true;
            this._loadOverview(model).finally(() => {
                this._experimentOverviewLoadPending = false;
            });
        },

        _getSensorPlacementDialog() {
            return DialogBuilder.getSensorPlacementDialog(this);
        },

        onOpenSensorPlacement() {
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

        onSensorLocationHelp() {
            return this.onOpenSensorPlacement();
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

            ranges.push((detail && detail.message) || `Stored historical weather data is incomplete for ${requested}.`);
            ranges.push(lowerRange ? `Closest earlier stored weather range: ${lowerRange}.` : "No earlier stored weather range is available.");
            ranges.push(higherRange ? `Closest later stored weather range: ${higherRange}.` : "No later stored weather range is available.");
            ranges.push("Import weather for this period or choose one of the available ranges.");

            model.setProperty("/rangeAlert", {
                visible: true,
                text: ranges.join(" ")
            });
        },

        _showKnownWeatherUnavailable(model, settings) {
            const key = weatherRangeKey(settings);
            if (this._weatherUnavailableByRange[key]) {
                delete this._weatherUnavailableByRange[key];
                this._clearRangeDataAlert(model);
            }
            return false;
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

        _experimentClientCacheKey(model, experiment, extraKey) {
            const settings = model.getProperty("/experimentSettings") || {};
            const range = experimentRange(settings);
            const items = model.getProperty("/sensorPlacements") || [];
            const placementKey = Array.isArray(items)
                ? items.map((item) => item.pot_id || item.sensor_id || item.id).join(",")
                : "";
            return [experiment, "weather-popover-v5-valves", range.start, range.end, placementKey, extraKey || ""].join("|");
        },

        _storeExperimentResult(cacheKey, result) {
            if (!cacheKey || !result) {
                return;
            }
            // Experiment result caching is temporarily disabled.
            // if (result && result.entries && result.summary) {
            //     this._experimentResultCache[cacheKey] = result;
            // }
        },

        _loadExperimentResultFromCache(model, cacheKey, applyResult) {
            if (!model || !cacheKey || !applyResult) {
                return false;
            }
            // Experiment result caching is temporarily disabled.
            // const cached = this._experimentResultCache[cacheKey];
            // if (!cached) {
            //     return false;
            // }
            // applyResult(cached, true);
            // model.refresh(true);
            // return true;
            return false;
        },

        _applySamplingResult(model, result, clientCacheHit) {
            const preparedResult = prepareChartResult(result, [
                { sourceKey: "baseline_water_usage_l", targetKey: "baseline_water_usage_chart" },
                { sourceKey: "sparse_water_usage_l", targetKey: "sparse_water_usage_chart" }
            ]);
            model.setProperty("/samplingEntries", preparedResult.tableEntries);
            model.setProperty("/samplingChartAllEntries", preparedResult.chartEntries);
            this._applyExperimentChartData("sampling");
            model.setProperty("/samplingPots", preparedResult.pots);
            model.setProperty("/samplingSummary", preparedResult.summary);
            this._setRangeDataAlertFromSummary(model, preparedResult.summary);
            this._updateExperimentFooter(model, Boolean(clientCacheHit || preparedResult.summary.cacheHit));
            model.setProperty(
                "/helloMessage",
                clientCacheHit || preparedResult.summary.cacheHit ? "Loaded cached sampling experiment" : `Database sampling experiment completed (${preparedResult.entries.length} days loaded)`
            );
            this._refreshExperimentCharts("sampling");
        },

        _applyAnfisResult(model, result, clientCacheHit) {
            const preparedResult = prepareChartResult(result, [
                { sourceKey: "baseline_water_usage_l", targetKey: "baseline_water_usage_chart" },
                { sourceKey: "anfis_water_usage_l", targetKey: "anfis_water_usage_chart" }
            ]);
            model.setProperty("/anfisEntries", preparedResult.tableEntries);
            model.setProperty("/anfisChartAllEntries", preparedResult.chartEntries);
            this._applyExperimentChartData("anfis");
            model.setProperty("/anfisPots", preparedResult.pots);
            model.setProperty("/anfisSummary", preparedResult.summary);
            this._setRangeDataAlertFromSummary(model, preparedResult.summary);
            this._updateExperimentFooter(model, Boolean(clientCacheHit || preparedResult.summary.cacheHit));
            model.setProperty(
                "/helloMessage",
                clientCacheHit || preparedResult.summary.cacheHit ? "Loaded cached ANFIS experiment" : `Database ANFIS experiment completed (${preparedResult.entries.length} days loaded)`
            );
            this._refreshExperimentCharts("anfis");
        },

        _applyFuzzyResult(model, result, clientCacheHit) {
            const preparedResult = prepareChartResult(result, [
                { sourceKey: "baseline_water_usage_l", targetKey: "baseline_water_usage_chart" },
                { sourceKey: "fuzzy_water_usage_l", targetKey: "fuzzy_water_usage_chart" }
            ]);
            model.setProperty("/fuzzyEntries", preparedResult.tableEntries);
            model.setProperty("/fuzzyChartAllEntries", preparedResult.chartEntries);
            this._applyExperimentChartData("fuzzy");
            model.setProperty("/fuzzyPots", preparedResult.pots);
            model.setProperty("/fuzzySummary", preparedResult.summary);
            this._setRangeDataAlertFromSummary(model, preparedResult.summary);
            this._updateExperimentFooter(model, Boolean(clientCacheHit || preparedResult.summary.cacheHit));
            model.setProperty(
                "/helloMessage",
                clientCacheHit || preparedResult.summary.cacheHit ? "Loaded cached fuzzy control experiment" : `Fuzzy control experiment completed (${preparedResult.entries.length} days loaded)`
            );
            this._refreshExperimentCharts("fuzzy");
        },

        onDateChange() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            // const range = experimentRange(settings);
            this._setRangeDataAlertForSettings(model, settings);
            this._updateExperimentFooter(model);
            // Experiment cache precompute is temporarily disabled.
            // this._precomputeExperiments(model, range.start, range.end);
        },

        onRunBaseline() {
            return this.onRunSampling();
        },

        onRunSampling() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            const sampling = model.getProperty("/samplingSettings") || {};
            const sampleIntervalHours = Math.min(336, Math.max(1, Math.floor(Number(sampling.sample_interval_hours) || RECOMMENDED_SAMPLING_INTERVAL_HOURS)));
            model.setProperty("/samplingSettings/sample_interval_hours", sampleIntervalHours);

            model.setProperty("/activeExperiment", "sampling");
            this._setRangeDataAlertForSettings(model, settings);
            if (this._showKnownWeatherUnavailable(model, settings, "Sampling experiment failed")) {
                return;
            }

            const cacheKey = this._experimentClientCacheKey(model, "sampling", String(sampleIntervalHours));
            if (this._loadExperimentResultFromCache(model, cacheKey, (result, clientCacheHit) => this._applySamplingResult(model, result, clientCacheHit))) {
                return;
            }

            model.setProperty("/samplingEntries", []);
            model.setProperty("/samplingChartAllEntries", []);
            model.setProperty("/samplingChartEntries", []);
            model.setProperty("/samplingPots", []);
            model.setProperty("/samplingSummary", defaultSamplingSummary(sampleIntervalHours));
            this._updateExperimentFooter(model);
            model.setProperty("/helloMessage", "Preparing sampling experiment...");
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
                        this._storeExperimentResult(this._experimentClientCacheKey(model, "sampling", String(sampleIntervalHours)), result);
                        this._applySamplingResult(model, result, false);
                        model.refresh(true);
                    }
                })
                .catch((error) => {
                    this._handleExperimentError(model, "Sampling experiment failed", error, settings);
                })
                .finally(() => {
                    model.setProperty("/isSamplingLoading", false);
                });
        },

        onRunSamplingExperiment() {
            return this.onRunSampling();
        },

        onRunAnfis() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            const scenarioSeed = settings.scenario_seed || 2026;

            model.setProperty("/activeExperiment", "anfis");
            this._setRangeDataAlertForSettings(model, settings);
            if (this._showKnownWeatherUnavailable(model, settings, "ANFIS experiment failed")) {
                return;
            }

            const cacheKey = this._experimentClientCacheKey(
                model,
                "anfis",
                `all_available|${scenarioSeed}`
            );
            if (this._loadExperimentResultFromCache(model, cacheKey, (result, clientCacheHit) => this._applyAnfisResult(model, result, clientCacheHit))) {
                return;
            }

            model.setProperty("/anfisEntries", []);
            model.setProperty("/anfisChartAllEntries", []);
            model.setProperty("/anfisChartEntries", []);
            model.setProperty("/anfisPots", []);
            model.setProperty("/anfisSummary", defaultAnfisSummary());
            this._updateExperimentFooter(model);
            model.setProperty("/helloMessage", "Preparing ANFIS experiment...");
            model.setProperty("/isAnfisLoading", true);

            this._ensureSensorPlacements(model)
                .then(() => {
                    model.setProperty("/helloMessage", "Running database ANFIS experiment...");
                    const url = getApiUrl("/api/experiment/anfis");
                    const range = experimentRange(settings);
                    url.searchParams.set("start", range.start);
                    url.searchParams.set("end", range.end);
                    url.searchParams.set("seed", scenarioSeed);
                    return fetchJson(url.toString());
                })
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        this._storeExperimentResult(cacheKey, result);
                        this._applyAnfisResult(model, result, false);
                        model.refresh(true);
                    }
                })
                .catch((error) => {
                    this._handleExperimentError(model, "ANFIS experiment failed", error, settings);
                })
                .finally(() => {
                    model.setProperty("/isAnfisLoading", false);
                });
        },

        onRunAnfisExperiment() {
            return this.onRunAnfis();
        },

        onRunFuzzyDt() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};

            model.setProperty("/activeExperiment", "fuzzy");
            this._setRangeDataAlertForSettings(model, settings);
            if (this._showKnownWeatherUnavailable(model, settings, "Fuzzy control experiment failed")) {
                return;
            }

            const cacheKey = this._experimentClientCacheKey(model, "fuzzy");
            if (this._loadExperimentResultFromCache(model, cacheKey, (result, clientCacheHit) => this._applyFuzzyResult(model, result, clientCacheHit))) {
                return;
            }

            model.setProperty("/fuzzyEntries", []);
            model.setProperty("/fuzzyChartAllEntries", []);
            model.setProperty("/fuzzyChartEntries", []);
            model.setProperty("/fuzzyPots", []);
            model.setProperty("/fuzzySummary", defaultFuzzySummary());
            this._updateExperimentFooter(model);
            model.setProperty("/helloMessage", "Preparing fuzzy control experiment...");
            model.setProperty("/isFuzzyLoading", true);

            this._ensureSensorPlacements(model)
                .then(() => {
                    model.setProperty("/helloMessage", "Running fuzzy control experiment...");
                    const url = getApiUrl("/api/experiment/fuzzy");
                    const range = experimentRange(settings);
                    url.searchParams.set("start", range.start);
                    url.searchParams.set("end", range.end);
                    return fetchJson(url.toString());
                })
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        this._storeExperimentResult(this._experimentClientCacheKey(model, "fuzzy"), result);
                        this._applyFuzzyResult(model, result, false);
                        model.refresh(true);
                    }
                })
                .catch((error) => {
                    this._handleExperimentError(model, "Fuzzy control experiment failed", error, settings);
                })
                .finally(() => {
                    model.setProperty("/isFuzzyLoading", false);
                });
        },

        onRunFuzzyExperiment() {
            return this.onRunFuzzyDt();
        },

    }));
});
