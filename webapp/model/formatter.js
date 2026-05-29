sap.ui.define([], () => {
    "use strict";

    function formatChartGranularity(value) {
        return String(value || "daily").toLowerCase() === "hourly" ? "Hourly" : "Daily";
    }

    function summaryNumber(value, decimals) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue)) {
            return Number(0).toFixed(decimals);
        }
        return numberValue.toFixed(decimals);
    }

    function summaryInteger(value) {
        const numberValue = Number(value);
        return Number.isFinite(numberValue) ? String(Math.round(numberValue)) : "0";
    }

    function summaryPercentChange(delta, baseline) {
        const baselineValue = Number(baseline);
        if (!Number.isFinite(baselineValue) || baselineValue === 0) {
            return "0.00%";
        }
        return `${summaryNumber((Number(delta) || 0) / baselineValue * 100, 2)}%`;
    }

    function summaryEventComparison(experimentLabel, experimentEvents, baselineEvents) {
        const eventsReduced = (Number(baselineEvents) || 0) - (Number(experimentEvents) || 0);
        return (
            `${experimentLabel}: ${summaryInteger(experimentEvents)}, ` +
            `baseline: ${summaryInteger(baselineEvents)} ` +
            `(${summaryPercentChange(eventsReduced, baselineEvents)} reduced)`
        );
    }

    function summaryValveRunComparison(experimentLabel, experimentRuns, baselineRuns) {
        return `Valve runs - ${experimentLabel}: ${summaryInteger(experimentRuns)}, baseline: ${summaryInteger(baselineRuns)}`;
    }

    function summaryDuration(value) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue) || numberValue <= 0) {
            return "0.00 s";
        }
        return numberValue < 10 ? `${summaryNumber(numberValue, 2)} s` : `${summaryNumber(numberValue, 1)} s`;
    }

    function summaryIconSvg(type) {
        const paths = {
            droplet: `<path d="M12 3.5C9.3 7 7 9.9 7 13a5 5 0 0 0 10 0c0-3.1-2.3-6-5-9.5Z"/><path d="M9.8 14.1c.5 1.2 1.4 1.8 2.7 1.8"/>`,
            leaf: `<path d="M4.5 14.5C5.8 8 10.1 4.9 18.8 5.2 18.4 13.5 14.6 18 7.9 18"/><path d="M4 20c3.8-5.5 7.8-8.7 12-9.8"/>`,
            faucet: `<path d="M7 7h8"/><path d="M10 7V5h4v2"/><path d="M6 11h9a3 3 0 0 1 3 3v1"/><path d="M4 15h7"/><path d="M7.5 15v4"/><path d="M18 18.5c1.2 0 2-.8 2-1.8 0-1.1-1-2.3-2-3.5-1 1.2-2 2.4-2 3.5 0 1 .8 1.8 2 1.8Z"/>`,
            target: `<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><path d="M12 2.8v3"/><path d="M21.2 12h-3"/><path d="M12 21.2v-3"/><path d="M2.8 12h3"/>`,
            shield: `<path d="M12 3.5 19 6v5.3c0 4.3-2.8 7.7-7 9.2-4.2-1.5-7-4.9-7-9.2V6l7-2.5Z"/><path d="m9 12 2 2 4-4"/>`,
            clock: `<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>`,
            gauge: `<path d="M5 17a8 8 0 1 1 14 0"/><path d="m12 14 4-4"/><path d="M8 17h8"/>`
        };
        return (
            `<span class="summaryCardIcon summaryCardIcon-${type}">` +
                `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[type] || paths.droplet}</svg>` +
            `</span>`
        );
    }

    function summaryCardHtml(icon, tone, title, value, detail) {
        return (
            `<article class="summaryCard summaryCard-${tone}">` +
                summaryIconSvg(icon) +
                `<div class="summaryCardBody">` +
                    `<div class="summaryCardTitle">${escapeHtml(title)}</div>` +
                    `<div class="summaryCardValue">${escapeHtml(value)}</div>` +
                    `<div class="summaryCardDetail">${escapeHtml(detail || "")}</div>` +
                `</div>` +
            `</article>`
        );
    }

    function samplingSummaryCardsHtml(summary) {
        const waterSavings = (Number(summary.baseline_total_water_usage_l) || 0) - (Number(summary.sparse_total_water_usage_l) || 0);
        const eventsReduced = (Number(summary.baseline_irrigation_event_count) || 0) - (Number(summary.sparse_irrigation_event_count) || 0);
        return [
            summaryCardHtml("droplet", "blue", "Water savings (L)", summaryNumber(waterSavings, 2), `${summaryPercentChange(waterSavings, summary.baseline_total_water_usage_l)} vs baseline`),
            summaryCardHtml(
                "leaf",
                "green",
                "Irrigation windows reduced",
                summaryInteger(eventsReduced),
                `${summaryEventComparison("Sampling", summary.sparse_irrigation_event_count, summary.baseline_irrigation_event_count)}. `
                    // summaryValveRunComparison("Sampling", summary.sparse_valve_run_count, summary.baseline_valve_run_count)
            ),
            summaryCardHtml("droplet", "cyan", "Water usage (Sampling)", `${summaryNumber(summary.sparse_total_water_usage_l, 2)} L`, `vs ${summaryNumber(summary.baseline_total_water_usage_l, 2)} L baseline`),
            summaryCardHtml(
                "target",
                "yellow",
                "Sampling accuracy",
                `${summaryNumber(summary.accuracy_percent, 1)}%`,
                `Sample interval: ${summaryInteger(summary.sample_interval_hours)} h; ` +
                    `${summaryInteger(summary.mismatch_days)} mismatch days`
            ),
            summaryCardHtml("clock", "blue", "Execution time", summaryDuration(summary.execution_time_seconds), "Sampling model")
        ].join("");
    }

    function anfisSummaryCardsHtml(summary) {
        const waterSavings = (Number(summary.baseline_total_water_usage_l) || 0) - (Number(summary.anfis_total_water_usage_l) || 0);
        const eventsReduced = (Number(summary.baseline_irrigation_event_count) || 0) - (Number(summary.anfis_irrigation_event_count) || 0);
        return [
            summaryCardHtml("droplet", "blue", "Water savings (L)", summaryNumber(waterSavings, 2), `${summaryPercentChange(waterSavings, summary.baseline_total_water_usage_l)} vs baseline`),
            summaryCardHtml(
                "leaf",
                "green",
                "Irrigation windows reduced",
                summaryInteger(eventsReduced),
                `${summaryEventComparison("ANFIS", summary.anfis_irrigation_event_count, summary.baseline_irrigation_event_count)}. ` 
                    // summaryValveRunComparison("ANFIS", summary.anfis_valve_run_count, summary.baseline_valve_run_count)
            ),
            summaryCardHtml("droplet", "cyan", "Water usage (ANFIS)", `${summaryNumber(summary.anfis_total_water_usage_l, 2)} L`, `vs ${summaryNumber(summary.baseline_total_water_usage_l, 2)} L baseline`),
            summaryCardHtml("target", "yellow", "Test accuracy", `${summaryNumber(summary.test_accuracy_percent, 1)}%`, "ANFIS-GA model"),
            summaryCardHtml("shield", "blue", "Prediction confidence", summaryNumber(summary.predicted_probability_mean, 2), `max ${summaryNumber(summary.predicted_probability_max, 2)}`),
            summaryCardHtml("clock", "purple", "Execution time", summaryDuration(summary.execution_time_seconds), "Training and simulation")
        ].join("");
    }

    function fuzzySummaryCardsHtml(summary) {
        const eventsReduced = (Number(summary.baseline_irrigation_event_count) || 0) - (Number(summary.fuzzy_irrigation_event_count) || 0);
        return [
            summaryCardHtml("droplet", "blue", "Water savings (L)", summaryNumber(summary.water_savings_l, 2), `${summaryNumber(summary.water_savings_percent, 2)}% vs FAO/PM`),
            summaryCardHtml(
                "leaf",
                "green",
                "Irrigation windows reduced",
                summaryInteger(eventsReduced),
                `${summaryEventComparison("Fuzzy", summary.fuzzy_irrigation_event_count, summary.baseline_irrigation_event_count)}. ` 
                    // summaryValveRunComparison("Fuzzy", summary.fuzzy_valve_run_count, summary.baseline_valve_run_count)
            ),
            summaryCardHtml("droplet", "purple", "Water usage (Fuzzy)", `${summaryNumber(summary.fuzzy_total_water_usage_l, 2)} L`, `vs ${summaryNumber(summary.fao_total_water_usage_l, 2)} L FAO/PM`),
            summaryCardHtml("faucet", "pink", "Avg prescription", `${summaryNumber(summary.average_prescription_mm, 2)} mm`, `vs ${summaryNumber(summary.average_etc_mm, 2)} mm ETc`),
            summaryCardHtml("target", "yellow", "Avg ETc", `${summaryNumber(summary.average_etc_mm, 2)} mm`, "FAO/PM reference"),
            summaryCardHtml("clock", "blue", "Execution time", summaryDuration(summary.execution_time_seconds), "Fuzzy model")
        ].join("");
    }

    function experimentSummaryCardsHtml(experiment, summary) {
        const content = {
            sampling: samplingSummaryCardsHtml,
            anfis: anfisSummaryCardsHtml,
            fuzzy: fuzzySummaryCardsHtml
        }[experiment];
        return content ? `<section class="experimentSummaryCards">${content(summary || {})}</section>` : "";
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
        const result = new Date(date.getTime());
        result.setDate(result.getDate() + days);
        return result;
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    const OVERVIEW_PALETTE = ["#bfd4fb", "#bff5ff", "#e4ffc3", "#d6e5ff", "#c9f7df"];
    const OVERVIEW_SEGMENT_COLORS = {
        measured: "#bff5ff",
        estimated: "#bfd4fb"
    };

    function overviewPercent(count, total) {
        return total > 0 ? Math.round((Number(count) || 0) / total * 100) : 0;
    }

    function overviewNumber(value, decimals) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue)) {
            return decimals > 0 ? "0.0" : "0";
        }
        return numberValue.toFixed(decimals);
    }

    function overviewSegments(rawSegments, total, useKeyColors) {
        const segments = Array.isArray(rawSegments) ? rawSegments : [];
        return segments.map((segment, index) => Object.assign({}, segment, {
            count: Number(segment.count) || 0,
            color: useKeyColors && OVERVIEW_SEGMENT_COLORS[segment.key]
                ? OVERVIEW_SEGMENT_COLORS[segment.key]
                : OVERVIEW_PALETTE[index % OVERVIEW_PALETTE.length],
            percent: overviewPercent(segment.count, total)
        }));
    }

    function overviewGradient(segments, total) {
        if (!segments.length || total <= 0) {
            return "#edf5ff";
        }
        let cursor = 0;
        const stops = segments.map((segment) => {
            const start = cursor;
            const value = Math.max(0, (segment.count / total) * 100);
            cursor += value;
            return `${segment.color} ${start.toFixed(3)}% ${cursor.toFixed(3)}%`;
        });
        return `conic-gradient(${stops.join(", ")})`;
    }

    function overviewLegendHtml(segments, total) {
        return segments.map((segment) => (
            `<div class="overviewLegendRow">` +
                `<span class="overviewSwatch" style="background:${segment.color}"></span>` +
                `<span class="overviewLegendLabel">${escapeHtml(segment.label)}</span>` +
                `<strong>${segment.count} (${overviewPercent(segment.count, total)}%)</strong>` +
            `</div>`
        )).join("");
    }

    function overviewDonutHtml(segments, total, centerValue, centerLabel, className) {
        const gradient = overviewGradient(segments, total);
        return (
            `<div class="overviewDonut ${className || ""}" style="background:${gradient}">` +
                `<div class="overviewDonutCenter">` +
                    `<strong>${escapeHtml(centerValue)}</strong>` +
                    `<span>${escapeHtml(centerLabel)}</span>` +
                `</div>` +
            `</div>`
        );
    }

    function overviewIconSvg(type) {
        const paths = {
            moisture: `<path d="M12 3.5C9.3 7 7 9.9 7 13a5 5 0 0 0 10 0c0-3.1-2.3-6-5-9.5Z"/><path d="M9.8 14.1c.5 1.2 1.4 1.8 2.7 1.8"/>`,
            rain: `<path d="M7.5 17.5h9a4 4 0 0 0 .4-8 5.7 5.7 0 0 0-10.8-1.7A4.8 4.8 0 0 0 7.5 17.5Z"/><path d="M8 20.5v1"/><path d="M12 20.5v1"/><path d="M16 20.5v1"/>`,
            irrigation: `<path d="M4 15h8"/><path d="M7 12v6"/><path d="M12 15c3.2 0 4.8-2.6 5.5-6.5-3.9.7-6.5 2.3-6.5 5.5"/><path d="M17 8.5 20 5.5"/><path d="M15.3 18.5c1.8 0 3.2-1.2 3.2-2.8 0-1.8-1.6-3.8-3.2-5.7-1.6 1.9-3.2 3.9-3.2 5.7 0 1.6 1.4 2.8 3.2 2.8Z"/>`,
            shield: `<path d="M12 3.5 19 6v5.3c0 4.3-2.8 7.7-7 9.2-4.2-1.5-7-4.9-7-9.2V6l7-2.5Z"/><path d="m9 12 2 2 4-4"/>`,
            clock: `<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>`,
            valve: `<path d="M4 8h16"/><path d="M8 8V5h8v3"/><path d="M10 5 8 3"/><path d="m14 5 2-2"/><path d="M7 12h10"/><path d="M9 12v6"/><path d="M15 12v6"/><path d="M6 18h12"/>`
        };
        return (
            `<span class="overviewRailIcon overviewRailIcon-${type}">` +
                `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[type] || paths.moisture}</svg>` +
            `</span>`
        );
    }

    function overviewStateRowHtml(icon, label, value) {
        return (
            `<div class="overviewRailMetric">` +
                `<span>${overviewIconSvg(icon)}<span>${escapeHtml(label)}</span></span>` +
                `<strong>${escapeHtml(value)}</strong>` +
            `</div>`
        );
    }

    function overviewValvePlanHtml(plan) {
        const data = plan || {};
        const priority = Array.isArray(data.priority_order) ? data.priority_order : [];
        const schedule = Array.isArray(data.optimized_schedule) ? data.optimized_schedule : [];
        const priorityRows = priority.length
            ? priority.map((item) => {
                const topPots = Array.isArray(item.top_pots)
                    ? item.top_pots.map((pot) => pot.pot_code).join(", ")
                    : "";
                const valveLabel = item.valve_number ? `V${item.valve_number}` : `#${item.rank}`;
                const flowLabel = `${overviewNumber(item.total_flow_l_min, 3)} L/min`;
                const durationLabel = `${overviewNumber(item.estimated_run_minutes, 1)} min`;
                return (
                    `<div class="overviewValveRow">` +
                        `<strong>${escapeHtml(`#${item.rank} ${valveLabel} -> ${item.zone}`)}</strong>` +
                        `<span>${escapeHtml(`${Number(item.affected_pots) || 0} pots`)}</span>` +
                        `<em>${escapeHtml(`${flowLabel}, ${durationLabel}${topPots ? `; ${topPots}` : ""}`)}</em>` +
                    `</div>`
                );
            }).join("")
            : `<div class="overviewValveEmpty">No dry zone requires a valve run.</div>`;
        const scheduleRows = schedule.length
            ? schedule.map((batch) => {
                const valves = Array.isArray(batch.valves)
                    ? batch.valves.map((valve) => `V${valve.valve_number}`).join(" + ")
                    : "";
                return (
                    `<div class="overviewValveBatch">` +
                        `<strong>${escapeHtml(`Batch ${batch.batch}: ${valves}`)}</strong>` +
                        `<span>${escapeHtml(`${overviewNumber(batch.duration_min, 1)} min, ${overviewNumber(batch.flow_l_min, 3)} L/min`)}</span>` +
                    `</div>`
                );
            }).join("")
            : "";

        return (
            `<div class="overviewValveMetrics">` +
                overviewStateRowHtml("valve", "Required valves", `${Number(data.required_valves) || 0}`) +
                overviewStateRowHtml("irrigation", "Managed pots", `${Number(data.affected_pots) || 0}`) +
                overviewStateRowHtml("valve", "Max tap load", `${overviewNumber(data.max_parallel_flow_l_min, 3)} L/min`) +
                overviewStateRowHtml("clock", "Immediate starts", `${Number(data.valve_starts) || 0}`) +
                overviewStateRowHtml("clock", "Full refill runtime", `${overviewNumber(data.full_refill_runtime_min || data.design_runtime_min, 1)} min`) +
                overviewStateRowHtml("shield", "Optimized runtime", `${overviewNumber(data.optimized_runtime_min, 1)} min`) +
                overviewStateRowHtml("irrigation", "Parallel valves", `${Number(data.max_parallel_valves) || 0}`) +
                overviewStateRowHtml("shield", "Next window fit", data.fits_next_window === false ? "Split" : "OK") +
            `</div>` +
            `<div class="overviewValveMode">${escapeHtml(data.recommendation || "Run valves sequentially by priority")}</div>` +
            `<div class="overviewValveSchedule">${scheduleRows}</div>` +
            `<div class="overviewValveList">${priorityRows}</div>`
        );
    }

    function overviewSideRailHtml(state, sensorCoverageHtml, plantOverviewHtml, valvePlanHtml) {
        return (
            `<aside class="overviewSideRail">` +
                `<div class="overviewRailStack">` +
                    `<section class="overviewRailCard">` +
                        `<h3>Digital Twin State</h3>` +
                        overviewStateRowHtml("moisture", "Current soil moisture", state.currentMoistureLabel) +
                        overviewStateRowHtml("rain", "Forecast rain (next 3 days)", state.forecastRainLabel) +
                        overviewStateRowHtml("irrigation", "Irrigation recommendation", state.irrigationRecommendation) +
                        overviewStateRowHtml("shield", "Confidence", state.confidenceLabel) +
                        overviewStateRowHtml("clock", "Next irrigation window", state.nextIrrigationWindowLabel) +
                    `</section>` +
                    `<section class="overviewRailCard">` +
                        `<h3>Sensor &amp; Node Coverage</h3>` +
                        sensorCoverageHtml +
                    `</section>` +
                    `<section class="overviewRailCard">` +
                        `<h3>Pot &amp; Plant Overview</h3>` +
                        plantOverviewHtml +
                    `</section>` +
                `</div>` +
                `<section class="overviewRailCard overviewValveCard">` +
                    `<h3>Valve Priority Plan</h3>` +
                    valvePlanHtml +
                `</section>` +
            `</aside>`
        );
    }

    function defaultOverview() {
        return {
            loaded: false,
            state: {
                currentMoistureLabel: "0%",
                forecastRainLabel: "Low (0 mm)",
                irrigationRecommendation: "OFF",
                confidenceLabel: "0.00",
                nextIrrigationWindowLabel: "N/A"
            },
            sensorCoverageHtml: "",
            valvePlanHtml: "",
            plantOverviewHtml: "",
            sideRailHtml: "",
            experimentSideRailHtml: ""
        };
    }

    function experimentSideRailHtml(state, plantOverviewHtml) {
    return (
        `<aside class="overviewSideRail experimentOnlyOverview">` +
            `<section class="overviewRailCard">` +
                `<h3>Digital Twin State</h3>` +
                overviewStateRowHtml("moisture", "Current soil moisture", state.currentMoistureLabel) +
                overviewStateRowHtml("rain", "Forecast rain (next 3 days)", state.forecastRainLabel) +
                overviewStateRowHtml("irrigation", "Irrigation recommendation", state.irrigationRecommendation) +
                overviewStateRowHtml("shield", "Confidence", state.confidenceLabel) +
                overviewStateRowHtml("clock", "Next irrigation window", state.nextIrrigationWindowLabel) +
            `</section>` +
            `<section class="overviewRailCard">` +
                `<h3>Pot &amp; Plant Overview</h3>` +
                plantOverviewHtml +
            `</section>` +
        `</aside>`
    );
    }

    function prepareOverview(result) {
        const data = result || {};
        const state = data.state || {};
        const coverage = data.sensor_coverage || {};
        const valvePlan = data.valve_plan || {};
        const plantOverview = data.plant_overview || {};
        const totalPots = Number(coverage.total_pots) || 0;
        const coverageSegments = overviewSegments(coverage.segments, totalPots, true);
        const sensorCoverageHtml = (
            `<div class="overviewChartLayout">` +
                overviewDonutHtml(coverageSegments, totalPots, totalPots || 0, "Pots", "overviewCoverageDonut") +
                `<div class="overviewLegend">${overviewLegendHtml(coverageSegments, totalPots)}</div>` +
            `</div>` +
            `<div class="overviewSensorFooter">` +
                `<span>Sensor nodes: <strong>${Number(coverage.sensor_nodes) || 0}</strong></span>` +
                `<span><i></i>Data freshness: <strong>${Number(coverage.data_freshness_pct) || 0}%</strong></span>` +
            `</div>`
        );

        const plantItems = Array.isArray(plantOverview.items) ? plantOverview.items : [];
        const plantTotal = Number(plantOverview.total_pots) || plantItems.reduce((sum, item) => sum + (Number(item.count) || 0), 0);
        const plantSegments = overviewSegments(plantItems.map((item) => ({
            key: item.key,
            label: item.label,
            count: item.count
        })), plantTotal, false);
        const plantRows = plantItems.map((item, index) => {
            const color = OVERVIEW_PALETTE[index % OVERVIEW_PALETTE.length];
            return (
                `<div class="overviewPlantRow">` +
                    `<span><i style="background:${color}"></i>${escapeHtml(item.label)}</span>` +
                    `<strong>${Number(item.count) || 0}</strong>` +
                    `<strong>${overviewNumber(item.avg_moisture_pct, 0)}%</strong>` +
                `</div>`
            );
        }).join("");
        const plantOverviewHtml = (
            `<div class="overviewPlantLayout">` +
                overviewDonutHtml(plantSegments, plantTotal, "", "", "overviewPlantDonut") +
                `<div class="overviewPlantTable">` +
                    `<div class="overviewPlantHeader"><span>Plant species</span><span>Pots</span><span>Avg. moisture</span></div>` +
                    plantRows +
                `</div>` +
            `</div>`
        );
        const valvePlanHtml = overviewValvePlanHtml(valvePlan);

        const stateModel = {
            currentMoistureLabel: `${overviewNumber(state.current_soil_moisture_pct, 0)}%`,
            forecastRainLabel: `${state.forecast_rain_level || "Low"} (${overviewNumber(state.forecast_rain_next_3_days_mm, 0)} mm)`,
            irrigationRecommendation: state.irrigation_recommendation || "OFF",
            confidenceLabel: overviewNumber(state.confidence, 2),
            nextIrrigationWindowLabel: state.next_irrigation_window && state.next_irrigation_window.label
                ? state.next_irrigation_window.label
                : "N/A"
        };

        return {
            loaded: true,
            state: stateModel,
            sensorCoverageHtml,
            valvePlanHtml,
            plantOverviewHtml,
            sideRailHtml: overviewSideRailHtml(
                stateModel,
                sensorCoverageHtml,
                plantOverviewHtml,
                valvePlanHtml
            ),
            experimentSideRailHtml: experimentSideRailHtml(
                stateModel,
                plantOverviewHtml
            )
        };
    }

    return {
        addDays,
        defaultOverview,
        escapeHtml,
        experimentSummaryCardsHtml,
        formatChartGranularity,
        formatLocalDate,
        parseLocalDate,
        prepareOverview,
        summaryDuration,
        summaryInteger,
        summaryNumber,
        summaryPercentChange
    };
});
