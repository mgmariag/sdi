sap.ui.define([
    "disertatie/model/apiClient",
    "disertatie/model/summaryCards"
], (ApiClient, SummaryCards) => {
    "use strict";

    const { fetchJson, getApiUrl } = ApiClient;

    function normalizedSensorCount(model) {
        const settings = model.getProperty("/sensorSettings") || {};
        const summaryCount = Number(model.getProperty("/sensorPlacementSummary/sensor_count"));
        const items = model.getProperty("/sensorPlacements") || [];
        const rawCount = Number(settings.sensor_count) || summaryCount || (Array.isArray(items) ? items.length : 0) || 4;
        const count = Math.max(1, Math.floor(rawCount));
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
                if (result && Array.isArray(result.items) && result.items.length > 0) {
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
        if (Array.isArray(items) && items.length > 0 && storedCount === count) {
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
