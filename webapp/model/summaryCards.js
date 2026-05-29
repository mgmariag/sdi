sap.ui.define([
    "disertatie/model/formatter",
    "disertatie/model/experimentMapper"
], (Formatter, ExperimentMapper) => {
    "use strict";

    const {
        experimentSummaryCardsHtml,
        formatChartGranularity
    } = Formatter;
    const { experimentDisplayName } = ExperimentMapper;

    function activeSummary(model) {
        const active = model.getProperty("/activeExperiment");
        return {
            sampling: model.getProperty("/samplingSummary"),
            anfis: model.getProperty("/anfisSummary"),
            fuzzy: model.getProperty("/fuzzySummary")
        }[active] || {};
    }

    function updateExperimentFooter(model, loadedFromCache) {
        const active = model.getProperty("/activeExperiment");
        const summary = activeSummary(model) || {};
        const activePotCount = Number(model.getProperty("/sensorPlacementSummary/active_pot_count")) || 0;
        const potsAnalyzed = Number(summary.potsAnalyzed) || activePotCount;
        const experimentLabel = active
            ? `${experimentDisplayName(active)}${loadedFromCache ? " (Loaded from cache)" : ""}`
            : experimentDisplayName(active);

        model.setProperty("/experimentFooter", {
            experimentLabel,
            daysAnalyzed: Number(summary.daysAnalyzed) || 0,
            pots: potsAnalyzed,
            timeInterval: formatChartGranularity(summary.chartGranularity)
        });
        model.setProperty("/experimentSummaryCardsHtml", experimentSummaryCardsHtml(active, summary));
    }

    return {
        activeSummary,
        updateExperimentFooter
    };
});
