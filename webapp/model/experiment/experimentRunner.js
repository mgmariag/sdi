sap.ui.define([
    "disertatie/model/api/apiClient",
    "disertatie/model/chart/chartData",
    "disertatie/model/presentation/basicFormat",
    "disertatie/model/experiment/experimentDefaults"
], (ApiClient, ChartData, BasicFormat, ExperimentDefaults) => {
    "use strict";

    const { fetchJson, getApiUrl } = ApiClient;
    const { prepareChartResult } = ChartData;
    const { addDays, formatLocalDate, parseLocalDate } = BasicFormat;
    const {
        REAL_FORECAST_HORIZON_DAYS,
        RECOMMENDED_SAMPLING_INTERVAL_HOURS,
        defaultAnfisSummary,
        defaultFuzzySummary,
        defaultSamplingSummary,
        experimentRange,
        formatWeatherRange,
        weatherRangeKey
    } = ExperimentDefaults;

    const EXPERIMENTS = {
        sampling: {
            apiPath: "/api/experiment/sampling",
            loadingPath: "/isSamplingLoading",
            entriesPath: "/samplingEntries",
            chartAllPath: "/samplingChartAllEntries",
            chartPath: "/samplingChartEntries",
            potsPath: "/samplingPots",
            summaryPath: "/samplingSummary",
            preparingMessage: "Preparing sampling experiment...",
            runningMessage: "Running database sampling experiment...",
            failureMessage: "Sampling experiment failed",
            cachedMessage: "Loaded cached sampling experiment",
            defaultSummary: (state) => defaultSamplingSummary(state.sampleIntervalHours),
            completedMessage: (daysLoaded) => `Database sampling experiment completed (${daysLoaded} days loaded)`,
            resultMeasures: [
                { sourceKey: "baseline_water_usage_l", targetKey: "baseline_water_usage_chart" },
                { sourceKey: "sparse_water_usage_l", targetKey: "sparse_water_usage_chart" }
            ],
            cacheExtra: (state) => String(state.sampleIntervalHours),
            setSearchParams: (url, state) => {
                url.searchParams.set("sample_interval_hours", state.sampleIntervalHours);
            }
        },
        anfis: {
            apiPath: "/api/experiment/anfis",
            loadingPath: "/isAnfisLoading",
            entriesPath: "/anfisEntries",
            chartAllPath: "/anfisChartAllEntries",
            chartPath: "/anfisChartEntries",
            potsPath: "/anfisPots",
            summaryPath: "/anfisSummary",
            preparingMessage: "Preparing ANFIS experiment...",
            runningMessage: "Running database ANFIS experiment...",
            failureMessage: "ANFIS experiment failed",
            cachedMessage: "Loaded cached ANFIS experiment",
            defaultSummary: () => defaultAnfisSummary(),
            completedMessage: (daysLoaded) => `Database ANFIS experiment completed (${daysLoaded} days loaded)`,
            resultMeasures: [
                { sourceKey: "baseline_water_usage_l", targetKey: "baseline_water_usage_chart" },
                { sourceKey: "anfis_water_usage_l", targetKey: "anfis_water_usage_chart" }
            ],
            cacheExtra: (state) => `all_available|${state.scenarioSeed}|auto_model`,
            setSearchParams: (url, state) => {
                url.searchParams.set("seed", state.scenarioSeed);
            }
        },
        fuzzy: {
            apiPath: "/api/experiment/fuzzy",
            loadingPath: "/isFuzzyLoading",
            entriesPath: "/fuzzyEntries",
            chartAllPath: "/fuzzyChartAllEntries",
            chartPath: "/fuzzyChartEntries",
            potsPath: "/fuzzyPots",
            summaryPath: "/fuzzySummary",
            preparingMessage: "Preparing fuzzy control experiment...",
            runningMessage: "Running fuzzy control experiment...",
            failureMessage: "Fuzzy control experiment failed",
            cachedMessage: "Loaded cached fuzzy control experiment",
            defaultSummary: () => defaultFuzzySummary(),
            completedMessage: (daysLoaded) => `Fuzzy control experiment completed (${daysLoaded} days loaded)`,
            resultMeasures: [
                { sourceKey: "baseline_water_usage_l", targetKey: "baseline_water_usage_chart" },
                { sourceKey: "fuzzy_water_usage_l", targetKey: "fuzzy_water_usage_chart" }
            ],
            cacheExtra: () => undefined,
            setSearchParams: () => {}
        }
    };

    function normalizedSamplingInterval(model) {
        const sampling = model.getProperty("/samplingSettings") || {};
        const interval = Math.min(
            336,
            Math.max(1, Math.floor(Number(sampling.sample_interval_hours) || RECOMMENDED_SAMPLING_INTERVAL_HOURS))
        );
        model.setProperty("/samplingSettings/sample_interval_hours", interval);
        return interval;
    }

    function getMaxStoredWeatherDate(model) {
        const storedMaxDate = parseLocalDate(model.getProperty("/weatherAvailability/maxWeatherDate"));
        if (storedMaxDate) {
            return storedMaxDate;
        }
        return addDays(new Date(), REAL_FORECAST_HORIZON_DAYS);
    }

    function clearRangeDataAlert(model) {
        model.setProperty("/rangeAlert", {
            visible: false,
            text: ""
        });
    }

    function setRangeDataAlertForSettings(model, settings) {
        const range = experimentRange(settings || {});
        const startDate = parseLocalDate(range.start);
        const endDate = parseLocalDate(range.end);
        const maxWeatherDate = getMaxStoredWeatherDate(model);
        if (!startDate || !endDate || endDate <= maxWeatherDate) {
            clearRangeDataAlert(model);
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
    }

    function setRangeDataAlertFromSummary(model, summary) {
        const estimatedRows = Number(summary && summary.dbSnapshotEstimatedWeatherRows);
        if (!Number.isFinite(estimatedRows) || estimatedRows <= 0) {
            clearRangeDataAlert(model);
            return;
        }

        model.setProperty("/rangeAlert", {
            visible: true,
            text: [
                "Stored weather data was unavailable for part or all of this range.",
                `The experiment generated ${estimatedRows} estimated hourly weather rows`,
                "and simulated the soil state from the latest known sensor readings."
            ].join(" ")
        });
    }

    function setWeatherUnavailableAlert(model, detail) {
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
    }

    function showKnownWeatherUnavailable(controller, model, settings) {
        const key = weatherRangeKey(settings);
        if (controller._weatherUnavailableByRange[key]) {
            delete controller._weatherUnavailableByRange[key];
            clearRangeDataAlert(model);
        }
        return false;
    }

    function handleExperimentError(controller, model, fallbackMessage, error, settings) {
        model.setProperty("/helloMessage", fallbackMessage);
        if (error && error.detail && error.detail.code === "weather_data_unavailable") {
            if (settings) {
                controller._weatherUnavailableByRange[weatherRangeKey(settings)] = error.detail;
            }
            setWeatherUnavailableAlert(model, error.detail);
        }
    }

    function experimentClientCacheKey(model, experiment, extraKey) {
        const settings = model.getProperty("/experimentSettings") || {};
        const range = experimentRange(settings);
        const items = model.getProperty("/sensorPlacements") || [];
        const placementKey = Array.isArray(items)
            ? items.map((item) => item.pot_id || item.sensor_id || item.id).join(",")
            : "";
        return [experiment, "sampling-sync-v17", range.start, range.end, placementKey, extraKey || ""].join("|");
    }

    function experimentResultCache(controller) {
        if (!controller) {
            return null;
        }
        if (!controller._experimentResultCache) {
            controller._experimentResultCache = {};
        }
        return controller._experimentResultCache;
    }

    function storeExperimentResult(controller, cacheKey, result) {
        const cache = experimentResultCache(controller);
        if (!cache || !cacheKey || !result) {
            return;
        }
        if (result.entries && result.summary) {
            cache[cacheKey] = result;
        }
    }

    function loadExperimentResultFromCache(controller, model, cacheKey, applyResult) {
        const cache = experimentResultCache(controller);
        if (!model || !cacheKey || !applyResult) {
            return false;
        }
        const cached = cache && cache[cacheKey];
        if (!cached) {
            return false;
        }
        applyResult(cached, true);
        model.refresh(true);
        return true;
    }

    function resetExperimentState(controller, model, experiment, state) {
        const config = EXPERIMENTS[experiment];
        model.setProperty(config.entriesPath, []);
        model.setProperty(config.chartAllPath, []);
        model.setProperty(config.chartPath, []);
        model.setProperty(config.potsPath, []);
        model.setProperty(config.summaryPath, config.defaultSummary(state || {}));
        controller._updateExperimentFooter(model);
    }

    function applyExperimentResult(controller, model, experiment, result, clientCacheHit) {
        const config = EXPERIMENTS[experiment];
        const preparedResult = prepareChartResult(result, config.resultMeasures);
        model.setProperty(config.entriesPath, preparedResult.tableEntries);
        model.setProperty(config.chartAllPath, preparedResult.chartEntries);
        controller._applyExperimentChartData(experiment);
        model.setProperty(config.potsPath, preparedResult.pots);
        model.setProperty(config.summaryPath, preparedResult.summary);
        setRangeDataAlertFromSummary(model, preparedResult.summary);
        controller._updateExperimentFooter(model, Boolean(clientCacheHit || preparedResult.summary.cacheHit));
        model.setProperty(
            "/helloMessage",
            clientCacheHit || preparedResult.summary.cacheHit
                ? config.cachedMessage
                : config.completedMessage(preparedResult.entries.length)
        );
        controller._refreshExperimentCharts(experiment);
    }

    function setBaseExperimentParams(url, settings) {
        const range = experimentRange(settings);
        url.searchParams.set("start", range.start);
        url.searchParams.set("end", range.end);
    }

    function runExperiment(controller, experiment, state) {
        const config = EXPERIMENTS[experiment];
        const model = controller.getView().getModel();
        const settings = model.getProperty("/experimentSettings") || {};

        model.setProperty("/activeExperiment", experiment);
        setRangeDataAlertForSettings(model, settings);
        if (showKnownWeatherUnavailable(controller, model, settings)) {
            return undefined;
        }

        const cacheKey = experimentClientCacheKey(model, experiment, config.cacheExtra(state || {}));
        if (loadExperimentResultFromCache(controller, model, cacheKey, (result, clientCacheHit) => {
            applyExperimentResult(controller, model, experiment, result, clientCacheHit);
        })) {
            return undefined;
        }

        resetExperimentState(controller, model, experiment, state);
        model.setProperty("/helloMessage", config.preparingMessage);
        model.setProperty(config.loadingPath, true);

        return controller._ensureSensorPlacements(model)
            .then(() => {
                model.setProperty("/helloMessage", config.runningMessage);
                const url = getApiUrl(config.apiPath);
                setBaseExperimentParams(url, settings);
                config.setSearchParams(url, state || {});
                return fetchJson(url.toString());
            })
            .then((result) => {
                if (result && result.entries && result.summary) {
                    storeExperimentResult(controller, cacheKey, result);
                    applyExperimentResult(controller, model, experiment, result, false);
                    model.refresh(true);
                }
            })
            .catch((error) => {
                handleExperimentError(controller, model, config.failureMessage, error, settings);
            })
            .finally(() => {
                model.setProperty(config.loadingPath, false);
            });
    }

    function runSampling(controller) {
        const model = controller.getView().getModel();
        return runExperiment(controller, "sampling", {
            sampleIntervalHours: normalizedSamplingInterval(model)
        });
    }

    function runAnfis(controller) {
        const model = controller.getView().getModel();
        const settings = model.getProperty("/experimentSettings") || {};
        return runExperiment(controller, "anfis", {
            scenarioSeed: settings.scenario_seed || 2026
        });
    }

    function runFuzzy(controller) {
        return runExperiment(controller, "fuzzy", {});
    }

    function runPrecompute(controller, model, startDate, endDate) {
        const settings = model.getProperty("/experimentSettings") || {};
        const sampleIntervalHours = normalizedSamplingInterval(model);
        const sensorCount = controller._normalizedSensorCount(model);
        const cacheKey = `${startDate}|${endDate}|${sampleIntervalHours}|${sensorCount}`;
        if (controller._precomputeStartedByRange[cacheKey]) {
            return undefined;
        }

        controller._precomputeStartedByRange[cacheKey] = true;

        const url = getApiUrl("/api/experiment/precompute");
        url.searchParams.set("start", startDate);
        url.searchParams.set("end", endDate);
        url.searchParams.set("sample_interval_hours", sampleIntervalHours);
        url.searchParams.set("seed", settings.scenario_seed || 2026);
        return fetchJson(url.toString(), { method: "POST" })
            .catch((error) => {
                delete controller._precomputeStartedByRange[cacheKey];
                throw error;
            });
    }

    function ignorePrecomputeError(result) {
        if (result && typeof result.catch === "function") {
            return result.catch(() => {
                // Precompute is opportunistic; explicit experiment runs still report errors.
            });
        }
        return result;
    }

    function precompute(controller, model, startDate, endDate) {
        if (!startDate || !endDate) {
            return undefined;
        }

        const placementReady = controller._sensorPlacementReady;
        if (placementReady && typeof placementReady.then === "function") {
            return placementReady
                .then(() => runPrecompute(controller, model, startDate, endDate))
                .catch(() => {
                    // Precompute is opportunistic; explicit experiment runs still report errors.
                });
        }
        return ignorePrecomputeError(runPrecompute(controller, model, startDate, endDate));
    }

    return {
        applyAnfisResult: (controller, model, result, clientCacheHit) => applyExperimentResult(controller, model, "anfis", result, clientCacheHit),
        applyFuzzyResult: (controller, model, result, clientCacheHit) => applyExperimentResult(controller, model, "fuzzy", result, clientCacheHit),
        applySamplingResult: (controller, model, result, clientCacheHit) => applyExperimentResult(controller, model, "sampling", result, clientCacheHit),
        clearRangeDataAlert,
        experimentClientCacheKey,
        getMaxStoredWeatherDate,
        handleExperimentError,
        loadExperimentResultFromCache,
        precompute,
        runAnfis,
        runFuzzy,
        runSampling,
        setRangeDataAlertForSettings,
        setRangeDataAlertFromSummary,
        setWeatherUnavailableAlert,
        showKnownWeatherUnavailable,
        storeExperimentResult
    };
});
