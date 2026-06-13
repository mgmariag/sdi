sap.ui.define([
    "disertatie/model/api/apiClient",
    "disertatie/model/experiment/summaryCards"
], (ApiClient, SummaryCards) => {
    "use strict";

    const { fetchJson, getApiUrl } = ApiClient;
    const FALLBACK_MIN_SENSOR_COUNT = 5;

    function minimumSensorCount(model) {
        const summaryMin = Number(model.getProperty("/sensorPlacementSummary/minimum_sensor_count"));
        return Number.isFinite(summaryMin) && summaryMin > 0 ? Math.floor(summaryMin) : FALLBACK_MIN_SENSOR_COUNT;
    }

    function normalizedSensorCount(model) {
        const settings = model.getProperty("/sensorSettings") || {};
        const summaryCount = Number(model.getProperty("/sensorPlacementSummary/sensor_count"));
        const items = model.getProperty("/sensorPlacements") || [];
        const rawCount = Number(settings.sensor_count) || summaryCount || (Array.isArray(items) ? items.length : 0) || minimumSensorCount(model);
        const count = Math.max(minimumSensorCount(model), Math.floor(rawCount));
        model.setProperty("/sensorSettings/sensor_count", count);
        return count;
    }

    function setSensorPlacementData(model, result) {
        const data = result || {};
        const items = Array.isArray(data.items) ? data.items : [];
        const resultCount = Number(data.sensor_count);
        const sensorCount = Number.isFinite(resultCount) && resultCount > 0 ? Math.floor(resultCount) : items.length;
        model.setProperty("/sensorPlacements", items);
        model.setProperty("/sensorPlacementSummary", {
            sensor_count: sensorCount || null,
            minimum_sensor_count: data.minimum_sensor_count || FALLBACK_MIN_SENSOR_COUNT,
            valve_count: data.valve_count || data.minimum_sensor_count || FALLBACK_MIN_SENSOR_COUNT,
            has_all_valve_zones: data.has_all_valve_zones !== false,
            stored_sensor_count: data.stored_sensor_count || 0,
            sensor_reading_pot_count: data.sensor_reading_pot_count || 0,
            active_pot_count: data.active_pot_count || 0,
            updated_at: data.updated_at || null,
            loaded: true
        });
        if (sensorCount > 0) {
            model.setProperty("/sensorSettings/sensor_count", sensorCount);
        }
        SummaryCards.updateExperimentFooter(model);
    }

    function sync(model, silent) {
        const count = normalizedSensorCount(model);
        const url = getApiUrl("/api/sensors/placements/ensure");
        url.searchParams.set("count", count);

        model.setProperty("/isSensorPlacementLoading", true);
        return fetchJson(url.toString(), { method: "POST" })
            .then((result) => {
                setSensorPlacementData(model, result);
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
    }

    function load(model) {
        return fetchJson(getApiUrl("/api/sensors/placements").toString())
            .then((result) => {
                setSensorPlacementData(model, result);
                const items = result && Array.isArray(result.items) ? result.items : [];
                const completeValveCoverage = result && result.has_all_valve_zones !== false;
                if (items.length > 0 && items.length >= minimumSensorCount(model) && completeValveCoverage) {
                    return result;
                }
                return sync(model, true);
            })
            .catch(() => {
                model.setProperty("/sensorPlacementSummary/loaded", true);
                return undefined;
            });
    }

    function ensure(controller, model) {
        if (!model.getProperty("/sensorPlacementSummary/loaded") && controller._sensorPlacementReady) {
            return controller._sensorPlacementReady.then(() => ensure(controller, model));
        }
        const count = normalizedSensorCount(model);
        const items = model.getProperty("/sensorPlacements") || [];
        const storedCount = Number(model.getProperty("/sensorPlacementSummary/sensor_count"));
        const hasAllValveZones = model.getProperty("/sensorPlacementSummary/has_all_valve_zones") !== false;
        if (Array.isArray(items) && items.length > 0 && storedCount === count && hasAllValveZones) {
            return Promise.resolve({ items });
        }
        return sync(model, true);
    }

    return {
        ensure,
        load,
        normalizedSensorCount,
        setSensorPlacementData,
        sync
    };
});
