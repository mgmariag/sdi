sap.ui.define([
    "disertatie/model/formatter",
    "disertatie/model/experimentMapper"
], (Formatter, ExperimentMapper) => {
    "use strict";

    const {
        compactIrrigationWindowHtml,
        experimentSideRailHtml,
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

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function roundedTotal(value) {
        return Number(value.toFixed(2));
    }

    function sumRows(rows, key) {
        return roundedTotal(rows.reduce((total, row) => total + numberValue(row && row[key]), 0));
    }

    function sumIntegerRows(rows, key) {
        return rows.reduce((total, row) => total + Math.round(numberValue(row && row[key])), 0);
    }

    function rowIrrigated(row, prefix) {
        return Boolean(
            row && (
                row[`${prefix}_irrigation_active`]
                || numberValue(row[`${prefix}_irrigation_events`]) > 0
                || numberValue(row[`${prefix}_valve_runs`]) > 0
            )
        );
    }

    function rowsHaveKey(rows, key) {
        return rows.some((row) => row && Object.prototype.hasOwnProperty.call(row, key));
    }

    function setSummaryValue(summary, key, value) {
        summary[key] = value;
    }

    function rowDateKey(row) {
        const value = row && (row.date || row.day_label || row.timestamp || row.chart_label);
        const match = String(value || "").match(/\d{4}-\d{2}-\d{2}/);
        return match ? match[0] : "";
    }

    function selectedPeriodRows(model, rows) {
        const sourceRows = Array.isArray(rows) ? rows : [];
        const settings = model.getProperty("/experimentSettings") || {};
        const start = settings.start_date || "";
        const end = settings.end_date || "";
        if (!start || !end) {
            return sourceRows;
        }

        const filtered = sourceRows.filter((row) => {
            const rowDate = rowDateKey(row);
            return rowDate && rowDate >= start && rowDate <= end;
        });
        return filtered.length ? filtered : sourceRows;
    }

    function activeRows(model, active) {
        const rows = {
            sampling: model.getProperty("/samplingEntries"),
            anfis: model.getProperty("/anfisEntries"),
            fuzzy: model.getProperty("/fuzzyEntries")
        }[active] || [];
        return selectedPeriodRows(model, rows);
    }

    function activeExperimentPrefix(active) {
        return {
            sampling: "sparse",
            anfis: "anfis",
            fuzzy: "fuzzy"
        }[active] || "";
    }

    function parseDateTime(value) {
        if (!value) {
            return null;
        }
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function fallbackDailyStart(row) {
        const dateKey = rowDateKey(row);
        return dateKey ? parseDateTime(`${dateKey}T06:00:00`) : null;
    }

    function rowWindowStart(row, prefix) {
        const explicitStart = parseDateTime(row && row[`${prefix}_irrigation_start_at`]);
        if (explicitStart) {
            return explicitStart;
        }
        if (row && (row.hour || /\d{2}:\d{2}/.test(String(row.day_label || row.chart_label || "")))) {
            return parseDateTime(row.timestamp);
        }
        return fallbackDailyStart(row) || parseDateTime(row && row.timestamp);
    }

    function rowWindowEnd(row, prefix, start) {
        return parseDateTime(row && row[`${prefix}_irrigation_end_at`])
            || (start ? new Date(start.getTime() + 60 * 60 * 1000) : null);
    }

    function dateTimeLabel(date) {
        if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
            return "";
        }
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        const hour = String(date.getHours()).padStart(2, "0");
        const minute = String(date.getMinutes()).padStart(2, "0");
        return `${year}-${month}-${day} ${hour}:${minute}`;
    }

    function timeLabel(date) {
        if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
            return "";
        }
        return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    }

    function activeExperimentNextWindow(model, active) {
        const prefix = activeExperimentPrefix(active);
        const rows = activeRows(model, active);
        const now = new Date();
        const candidates = rows
            .filter((row) => prefix && rowIrrigated(row, prefix))
            .map((row) => {
                const start = rowWindowStart(row, prefix);
                return {
                    row,
                    start,
                    end: rowWindowEnd(row, prefix, start)
                };
            })
            .filter((candidate) => candidate.start && candidate.start >= now)
            .sort((a, b) => a.start - b.start);

        if (!candidates.length) {
            return null;
        }

        const next = candidates[0];
        const label = next.end
            ? `${dateTimeLabel(next.start)} - ${timeLabel(next.end)}`
            : dateTimeLabel(next.start);
        const window = {
            label,
            source: active
        };
        setSummaryValue(window, "start_at", next.start.toISOString());
        setSummaryValue(window, "end_at", next.end ? next.end.toISOString() : null);
        setSummaryValue(window, "item_count", sumIntegerRows([next.row], `${prefix}_valve_runs`));
        setSummaryValue(window, "activated_valves", next.row && next.row[`${prefix}_activated_valves`]);
        setSummaryValue(window, "planned_volume_l", numberValue(next.row && next.row[`${prefix}_water_usage_l`]));
        return window;
    }

    function updateExperimentContextRail(model) {
        const active = model.getProperty("/activeExperiment");
        const overview = model.getProperty("/overview") || {};
        if (!active || !overview.loaded) {
            return;
        }

        const state = Object.assign({}, overview.state || {});
        const nextWindow = activeExperimentNextWindow(model, active);
        if (nextWindow) {
            state.nextIrrigationWindowLabel = nextWindow.label;
            state.compactNextIrrigationWindowHtml = compactIrrigationWindowHtml(nextWindow);
            state.irrigationActivityLabel = "Next planned irrigation";
            state.irrigationActivityValue = nextWindow.label;
            state.compactIrrigationActivityHtml = compactIrrigationWindowHtml(nextWindow);
        }

        model.setProperty(
            "/overview/experimentSideRailHtml",
            experimentSideRailHtml(
                state,
                overview.plantOverviewHtml || "",
                overview.weatherImpactHtml || "",
                active,
                activeDisplaySummary(model)
            )
        );
    }

    function withSamplingRowTotals(summary, rows) {
        if (!rowsHaveKey(rows, "sparse_water_usage_l")) {
            return summary;
        }

        const mismatches = rows.filter((row) => rowIrrigated(row, "baseline") !== rowIrrigated(row, "sparse"));
        const baselineOnlyRows = rows.filter((row) => rowIrrigated(row, "baseline") && !rowIrrigated(row, "sparse"));
        const sparseOnlyRows = rows.filter((row) => rowIrrigated(row, "sparse") && !rowIrrigated(row, "baseline"));
        const missedValveRuns = rows.reduce((total, row) => (
            total + Math.max(0, Math.round(numberValue(row && row.baseline_valve_runs)) - Math.round(numberValue(row && row.sparse_valve_runs)))
        ), 0);
        const extraValveRuns = rows.reduce((total, row) => (
            total + Math.max(0, Math.round(numberValue(row && row.sparse_valve_runs)) - Math.round(numberValue(row && row.baseline_valve_runs)))
        ), 0);

        const output = Object.assign({}, summary);
        setSummaryValue(output, "totalEntries", rows.length);
        setSummaryValue(output, "daysAnalyzed", rows.length);
        setSummaryValue(output, "baseline_total_water_usage_l", sumRows(rows, "baseline_water_usage_l"));
        setSummaryValue(output, "sparse_total_water_usage_l", sumRows(rows, "sparse_water_usage_l"));
        setSummaryValue(output, "baseline_irrigation_event_count", sumIntegerRows(rows, "baseline_irrigation_events"));
        setSummaryValue(output, "sparse_irrigation_event_count", sumIntegerRows(rows, "sparse_irrigation_events"));
        setSummaryValue(output, "baseline_valve_run_count", sumIntegerRows(rows, "baseline_valve_runs"));
        setSummaryValue(output, "sparse_valve_run_count", sumIntegerRows(rows, "sparse_valve_runs"));
        setSummaryValue(output, "accuracy_percent", rows.length ? roundedTotal((rows.length - mismatches.length) / rows.length * 100) : numberValue(summary.accuracy_percent));
        setSummaryValue(output, "mismatch_days", mismatches.length);
        setSummaryValue(output, "mismatch_steps", mismatches.length);
        setSummaryValue(output, "baseline_only_irrigation_days", baselineOnlyRows.length);
        setSummaryValue(output, "sparse_only_irrigation_days", sparseOnlyRows.length);
        setSummaryValue(output, "baseline_only_valve_runs", sumIntegerRows(baselineOnlyRows, "baseline_valve_runs"));
        setSummaryValue(output, "baseline_only_water_usage_l", sumRows(baselineOnlyRows, "baseline_water_usage_l"));
        setSummaryValue(output, "missed_valve_run_delta", missedValveRuns);
        setSummaryValue(output, "sparse_extra_valve_run_delta", extraValveRuns);
        return output;
    }

    function withAnfisRowTotals(summary, rows) {
        if (!rowsHaveKey(rows, "anfis_water_usage_l")) {
            return summary;
        }

        const output = Object.assign({}, summary);
        setSummaryValue(output, "totalEntries", rows.length);
        setSummaryValue(output, "daysAnalyzed", rows.length);
        setSummaryValue(output, "baseline_total_water_usage_l", sumRows(rows, "baseline_water_usage_l"));
        setSummaryValue(output, "anfis_total_water_usage_l", sumRows(rows, "anfis_water_usage_l"));
        setSummaryValue(output, "baseline_irrigation_event_count", sumIntegerRows(rows, "baseline_irrigation_events"));
        setSummaryValue(output, "anfis_irrigation_event_count", sumIntegerRows(rows, "anfis_irrigation_events"));
        if (rowsHaveKey(rows, "baseline_valve_runs") || rowsHaveKey(rows, "anfis_valve_runs")) {
            setSummaryValue(output, "baseline_valve_run_count", sumIntegerRows(rows, "baseline_valve_runs"));
            setSummaryValue(output, "anfis_valve_run_count", sumIntegerRows(rows, "anfis_valve_runs"));
        }
        return output;
    }

    function withFuzzyRowTotals(summary, rows) {
        if (!rowsHaveKey(rows, "fuzzy_water_usage_l")) {
            return summary;
        }

        const baselineWater = sumRows(rows, "baseline_water_usage_l");
        const fuzzyWater = sumRows(rows, "fuzzy_water_usage_l");
        const waterSavings = roundedTotal(baselineWater - fuzzyWater);
        const output = Object.assign({}, summary);
        setSummaryValue(output, "totalEntries", rows.length);
        setSummaryValue(output, "daysAnalyzed", rows.length);
        setSummaryValue(output, "baseline_total_water_usage_l", baselineWater);
        setSummaryValue(output, "fuzzy_total_water_usage_l", fuzzyWater);
        setSummaryValue(output, "water_savings_l", waterSavings);
        setSummaryValue(output, "water_savings_percent", baselineWater > 0 ? roundedTotal(waterSavings / baselineWater * 100) : 0);
        setSummaryValue(output, "baseline_irrigation_event_count", sumIntegerRows(rows, "baseline_irrigation_events"));
        setSummaryValue(output, "fuzzy_irrigation_event_count", sumIntegerRows(rows, "fuzzy_irrigation_events"));
        setSummaryValue(output, "baseline_valve_run_count", sumIntegerRows(rows, "baseline_valve_runs"));
        setSummaryValue(output, "fuzzy_valve_run_count", sumIntegerRows(rows, "fuzzy_valve_runs"));
        return output;
    }

    function activeDisplaySummary(model) {
        const active = model.getProperty("/activeExperiment");
        const summary = activeSummary(model) || {};
        const rows = activeRows(model, active);
        if (!Array.isArray(rows) || !rows.length) {
            return summary;
        }

        const summaryBuilder = {
            sampling: withSamplingRowTotals,
            anfis: withAnfisRowTotals,
            fuzzy: withFuzzyRowTotals
        }[active];
        return summaryBuilder ? summaryBuilder(summary, rows) : summary;
    }

    function updateExperimentFooter(model, loadedFromCache) {
        const active = model.getProperty("/activeExperiment");
        const summary = activeDisplaySummary(model) || {};
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
        updateExperimentContextRail(model);
    }

    return {
        activeSummary,
        activeDisplaySummary,
        updateExperimentFooter
    };
});
