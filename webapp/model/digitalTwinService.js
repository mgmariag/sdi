sap.ui.define([], function () {
    "use strict";

    function computeIrrigationState(entry, threshold, hysteresisWidth, previousState) {
        const moisture = Number(entry.moisture);
        const rainPrediction = Boolean(entry.rain_prediction);

        if (hysteresisWidth && hysteresisWidth > 0) {
            const half = hysteresisWidth / 2;
            const lower = threshold - half;
            const upper = threshold + half;

            if (moisture < lower && !rainPrediction) {
                return true;
            }

            if (moisture > upper || rainPrediction) {
                return false;
            }

            return Boolean(previousState);
        }

        return moisture < threshold && !rainPrediction;
    }

    function applyDigitalTwin(entries, settings) {
        const threshold = Number(settings.threshold) || 35;
        const hysteresisWidth = Number(settings.hysteresis_width) || 0;
        const flowRate = Number(settings.flow_rate_l_per_step) || 1.0;

        let previousState = false;
        let irrigationEvents = 0;
        let irrigationSteps = 0;
        const enriched = entries.map((entry) => {
            const irrigationActive = computeIrrigationState(entry, threshold, hysteresisWidth, previousState);
            const waterUsage = irrigationActive ? flowRate : 0;
            if (irrigationActive && !previousState) {
                irrigationEvents += 1;
            }
            if (irrigationActive) {
                irrigationSteps += 1;
            }
            previousState = irrigationActive;

            return Object.assign({}, entry, {
                irrigation_active: irrigationActive,
                water_usage_l: waterUsage
            });
        });

        return {
            entries: enriched,
            summary: {
                totalEntries: entries.length,
                irrigationEvents: irrigationEvents,
                irrigationSteps: irrigationSteps,
                totalWaterUsage: parseFloat((irrigationSteps * flowRate).toFixed(2)),
                percentTimeIrrigated: parseFloat((irrigationSteps / Math.max(entries.length, 1) * 100).toFixed(1)),
                threshold: threshold,
                hysteresisWidth: hysteresisWidth,
                flowRate: flowRate
            }
        };
    }

    return {
        applyDigitalTwin: applyDigitalTwin
    };
});
