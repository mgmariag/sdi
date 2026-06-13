"use strict";

sap.ui.define([
    "sap/ui/core/mvc/Controller",
    "sap/ui/model/json/JSONModel",
    "disertatie/model/ui/dialogBuilder",
    "disertatie/model/sensors/sensorPlacementBuilder",
    "disertatie/model/experiment/summaryCards",
    "disertatie/model/chart/chartRuntime",
    "disertatie/model/experiment/experimentRunner",
    "disertatie/model/view/viewState",
    "disertatie/model/overview/overviewLoader"
], (...modules) => {
    const [
        Controller,
        JSONModel,
        DialogBuilder,
        SensorPlacementBuilder,
        SummaryCards,
        ChartRuntime,
        ExperimentRunner,
        ViewState,
        OverviewLoader
    ] = modules;

    function initializeControllerState(controller) {
        controller._chartPopovers = {};
        controller._chartWindowTimers = {};
        controller._chartOverlayTimers = {};
        controller._chartOverlayFrameIds = {};
        controller._chartOverlayObservers = {};
        controller._chartOverlayListenerCleanups = {};
        controller._appliedChartWindowSignatures = {};
        controller._chartWindowSyncTimers = {};
        controller._chartWindowSyncObservers = {};
        controller._chartWindowSyncListenerCleanups = {};
        controller._syncedChartWindowSignatures = {};
        controller._overviewRefreshTimer = null;
        controller._overviewRefreshHandler = null;
        controller._sensorPlacementDialog = null;
        controller._weatherUnavailableByRange = {};
        controller._precomputeStartedByRange = {};
        controller._experimentResultCache = {};
    }

    function stopOverviewRefresh(controller) {
        if (controller._overviewRefreshTimer) {
            clearInterval(controller._overviewRefreshTimer);
            controller._overviewRefreshTimer = null;
        }
        if (controller._overviewRefreshHandler) {
            document.removeEventListener("visibilitychange", controller._overviewRefreshHandler);
            controller._overviewRefreshHandler = null;
        }
    }

    return Controller.extend("disertatie.controller.View1", Object.assign({}, ChartRuntime, {
        onInit() {
            initializeControllerState(this);
            this._registerChartFormatters();

            const oModel = new JSONModel(ViewState.initialData());
            this.getView().setModel(oModel);
            this._sensorPlacementReady = this._loadSensorPlacements(oModel);
            this._loadWeatherAvailability(oModel);
            this._loadOverview(oModel);
            this._startOverviewRefresh(oModel);
            OverviewLoader.loadHello(oModel);
        },

        onExit() {
            if (typeof this._destroyChartRuntime === "function") {
                this._destroyChartRuntime();
            }
            stopOverviewRefresh(this);
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
            return OverviewLoader.loadOverview(model, () => this._updateExperimentFooter(model));
        },

        _loadWeatherAvailability(model) {
            return OverviewLoader.loadWeatherAvailability(model);
        },

        _precomputeExperiments(model, startDate, endDate) {
            return ExperimentRunner.precompute(this, model, startDate, endDate);
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
            return ExperimentRunner.getMaxStoredWeatherDate(model);
        },

        _setRangeDataAlertForSettings(model, settings) {
            return ExperimentRunner.setRangeDataAlertForSettings(model, settings);
        },

        _setRangeDataAlertFromSummary(model, summary) {
            return ExperimentRunner.setRangeDataAlertFromSummary(model, summary);
        },

        _setWeatherUnavailableAlert(model, detail) {
            return ExperimentRunner.setWeatherUnavailableAlert(model, detail);
        },

        _showKnownWeatherUnavailable(model, settings) {
            return ExperimentRunner.showKnownWeatherUnavailable(this, model, settings);
        },

        _handleExperimentError(model, fallbackMessage, error, settings) {
            return ExperimentRunner.handleExperimentError(this, model, fallbackMessage, error, settings);
        },

        _clearRangeDataAlert(model) {
            return ExperimentRunner.clearRangeDataAlert(model);
        },

        _experimentClientCacheKey(model, experiment, extraKey) {
            return ExperimentRunner.experimentClientCacheKey(model, experiment, extraKey);
        },

        _storeExperimentResult(cacheKey, result) {
            return ExperimentRunner.storeExperimentResult(this, cacheKey, result);
        },

        _loadExperimentResultFromCache(model, cacheKey, applyResult) {
            return ExperimentRunner.loadExperimentResultFromCache(this, model, cacheKey, applyResult);
        },

        _applySamplingResult(model, result, clientCacheHit) {
            return ExperimentRunner.applySamplingResult(this, model, result, clientCacheHit);
        },

        _applyAnfisResult(model, result, clientCacheHit) {
            return ExperimentRunner.applyAnfisResult(this, model, result, clientCacheHit);
        },

        _applyFuzzyResult(model, result, clientCacheHit) {
            return ExperimentRunner.applyFuzzyResult(this, model, result, clientCacheHit);
        },

        onDateChange() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            this._setRangeDataAlertForSettings(model, settings);
            this._updateExperimentFooter(model);
        },

        onRunBaseline() {
            return this.onRunSampling();
        },

        onRunSampling() {
            return ExperimentRunner.runSampling(this);
        },

        onRunSamplingExperiment() {
            return this.onRunSampling();
        },

        onRunAnfis() {
            return ExperimentRunner.runAnfis(this);
        },

        onRunAnfisExperiment() {
            return this.onRunAnfis();
        },

        onRunFuzzyDt() {
            return ExperimentRunner.runFuzzy(this);
        },

        onRunFuzzyExperiment() {
            return this.onRunFuzzyDt();
        }
    }));
});
