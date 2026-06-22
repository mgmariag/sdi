sap.ui.define([
    "disertatie/model/presentation/basicFormat",
    "disertatie/model/presentation/overviewWidgets",
    "disertatie/model/presentation/irrigationWindowHtml",
    "disertatie/model/presentation/experimentEvidenceHtml"
], (BasicFormat, OverviewWidgets, IrrigationWindowHtml, ExperimentEvidenceHtml) => {
    "use strict";

    const { escapeHtml } = BasicFormat;
    const {
        NO_IRRIGATION_PLANNED_LABEL,
        NO_IRRIGATION_RECORDED_LABEL,
        OVERVIEW_PALETTE,
        overviewClampPercent,
        overviewDonutHtml,
        overviewIconSvg,
        overviewLegendHtml,
        overviewNumber,
        overviewRecommendationLabel,
        overviewSegments,
        overviewSensorCoverageInfoHtml,
        overviewStateOptionalRowHtmlValueHtml,
        overviewStateRowHtml,
        overviewStateRowHtmlValueHtml
    } = OverviewWidgets;
    const {
        compactIrrigationWindowHtml,
        overviewLatestIrrigationHtml
    } = IrrigationWindowHtml;
    const { experimentEvidenceSummaryHtml } = ExperimentEvidenceHtml;

    function overviewActivityState(state) {
        const activity = state.irrigation_activity || {};
        const fallback = activity.label ? activity : (state.next_irrigation_window || {});
        return {
            label: activity.display_label || "Most recent irrigation",
            value: fallback.label || NO_IRRIGATION_PLANNED_LABEL,
            compactHtml: compactIrrigationWindowHtml(fallback)
        };
    }
    function overviewRainReductionPercent(rainMm) {
        const rainValue = Number(rainMm) || 0;
        if (rainValue >= 12) {
            return 50;
        }
        if (rainValue >= 1) {
            return 25;
        }
        return 0;
    }

    function overviewPriorityLevel(score) {
        const value = Number(score);
        if (!Number.isFinite(value)) {
            return "Low";
        }
        if (value >= 40) {
            return "High";
        }
        if (value >= 26) {
            return "Medium";
        }
        return "Low";
    }

    function overviewValvePriorityLevel(score, requiresRun) {
        const level = overviewPriorityLevel(score);
        return !requiresRun && level === "High" ? "Medium" : level;
    }

    function overviewZoneLabel(zone) {
        return String(zone || "Zone").replace(/_/g, " ");
    }

    function overviewWeatherImpact(state) {
        const rainMm = Number(state.forecast_rain_next_3_days_mm) || 0;
        const maxTemperature = Number(state.forecast_max_temperature_c);
        const hasTemperature = Number.isFinite(maxTemperature);
        const hotDayMode = hasTemperature && maxTemperature >= 32;
        const rainExpected = rainMm >= 1;
        let reason = "low rain expected and max temp below 32°C";

        if (!hasTemperature) {
            reason = rainExpected ? "rain expected; max temp unavailable" : "low rain expected; max temp unavailable";
        } else if (rainExpected && hotDayMode) {
            reason = "rain expected, but max temp is at least 32°C";
        } else if (rainExpected) {
            reason = "rain expected and max temp below 32°C";
        } else if (hotDayMode) {
            reason = "low rain expected, but max temp is at least 32°C";
        }

        if (hasTemperature) {
            if (rainExpected && hotDayMode) {
                reason = "rain expected, but max temp is at least 32\u00b0C";
            } else if (rainExpected) {
                reason = "rain expected and max temp below 32\u00b0C";
            } else if (hotDayMode) {
                reason = "low rain expected, but max temp is at least 32\u00b0C";
            } else {
                reason = "low rain expected and max temp below 32\u00b0C";
            }
        }

        return {
            forecastRainLabel: `${state.forecast_rain_level || "Low"}, ${overviewNumber(rainMm, 0)} mm`,
            irrigationReductionLabel: `${overviewRainReductionPercent(rainMm)}%`,
            hotDayModeLabel: hotDayMode ? "ON" : "OFF",
            reasonLabel: reason
        };
    }

    function overviewWeatherImpactHtml(impact) {
        return (
            "<section class=\"overviewRailCard overviewWeatherImpactCard\">" +
                "<h3>Weather Impact</h3>" +
                overviewStateRowHtml("irrigation", "Irrigation reduction", impact.irrigationReductionLabel) +
                overviewStateRowHtml("temperature", "Hot-day mode", impact.hotDayModeLabel) +
                "<div class=\"overviewRailMetric overviewWeatherReason\">" +
                    `<span>${overviewIconSvg("shield")}<span>Reason</span></span>` +
                    `<strong>${escapeHtml(impact.reasonLabel)}</strong>` +
                "</div>" +
            "</section>"
        );
    }

    function overviewValvePlanHtml(plan) {
        const data = plan || {};
        const priority = Array.isArray(data.priority_order) ? data.priority_order : [];
        const schedule = Array.isArray(data.optimized_schedule) ? data.optimized_schedule : [];
        const maxParallelFlow = Number(data.max_parallel_flow_l_min) || 0;
        const safeTapFlow = Number(data.safe_tap_flow_l_min) || 0;
        const optimizedRuntime = Number(data.optimized_runtime_min) || 0;
        const fullRuntime = Number(data.full_refill_runtime_min || data.design_runtime_min) || 0;
        const completeVolume = Number(data.complete_irrigation_volume_l || data.full_refill_volume_l) || 0;
        const runtimeSaved = Math.max(0, fullRuntime - optimizedRuntime);
        const nextWindowMinutes = Number(data.next_window_minutes);
        const hasWindow = Number.isFinite(nextWindowMinutes) && nextWindowMinutes > 0;
        const runtimeShare = hasWindow ? overviewClampPercent((optimizedRuntime / nextWindowMinutes) * 100) : 0;
        const flowShare = safeTapFlow > 0 ? overviewClampPercent((maxParallelFlow / safeTapFlow) * 100) : 0;
        const fitIsSplit = data.fits_next_window === false;
        const fitLabel = fitIsSplit ? "Split required" : "Fits next window";
        const fitClass = fitIsSplit ? "overviewValveStatusSplit" : "overviewValveStatusOk";
        const windowLabel = hasWindow ? `${overviewNumber(nextWindowMinutes, 1)} min window` : "No window limit";
        const runtimeDetail = `Full ${overviewNumber(fullRuntime, 1)} min; saved ${overviewNumber(runtimeSaved, 1)} min; ${windowLabel}`;
        const recommendation = data.recommendation || "Run valves sequentially by priority";
        const priorityRows = priority.length
            ? priority.map((item) => {
                const affectedPotsInZone = Number(item.affected_pots) || 0;
                const valveLabel = item.valve_number ? `V${item.valve_number}` : `#${item.rank}`;
                const flowLabel = `${overviewNumber(item.total_flow_l_min, 3)} L/min`;
                const durationLabel = `${overviewNumber(item.estimated_run_minutes, 1)} min`;
                const requiresRun = Boolean(item.requires_run || Number(item.immediate_pots) > 0);
                const priorityLevel = overviewValvePriorityLevel(item.priority_score, requiresRun);
                const statusLabel = requiresRun ? "Below minimum" : "Above minimum";
                const statusClass = requiresRun ? "overviewValveTagNeedsWater" : "overviewValveTagStandby";
                return (
                    "<article class=\"overviewValveRow\">" +
                        "<div class=\"overviewValveRowHeader\">" +
                            `<span class="overviewValveRank">${escapeHtml(`#${item.rank || 0}`)}</span>` +
                            "<div>" +
                                `<strong>${escapeHtml(`${valveLabel} ${overviewZoneLabel(item.zone)}`)}</strong>` +
                                `<em>${escapeHtml(`${affectedPotsInZone} pots`)}</em>` +
                            "</div>" +
                            `<span class="overviewValveTag ${statusClass}">${escapeHtml(statusLabel)}</span>` +
                        "</div>" +
                        "<div class=\"overviewValveStats\">" +
                            `<span><b>${escapeHtml(flowLabel)}</b><em>Flow</em></span>` +
                            `<span><b>${escapeHtml(durationLabel)}</b><em>Runtime</em></span>` +
                            `<span><b>${escapeHtml(priorityLevel)}</b><em>Priority</em></span>` +
                        "</div>" +
                    "</article>"
                );
            }).join("")
            : "<div class=\"overviewValveEmpty\">No active valve zones to prioritize.</div>";
        const scheduleRows = schedule.length
            ? schedule.map((batch) => {
                const valves = Array.isArray(batch.valves) ? batch.valves : [];
                const valveChips = valves.length
                    ? valves.map((valve) => `<span>${escapeHtml(`V${valve.valve_number}`)}</span>`).join("")
                    : "<span>Idle</span>";
                const duration = Number(batch.duration_min) || 0;
                const flow = Number(batch.flow_l_min) || 0;
                const durationShare = optimizedRuntime > 0 ? overviewClampPercent((duration / optimizedRuntime) * 100) : 100;
                const batchFlowShare = safeTapFlow > 0 ? overviewClampPercent((flow / safeTapFlow) * 100) : 0;
                return (
                    `<article class="overviewValveBatch" style="--batch-share:${durationShare}%; --flow-share:${batchFlowShare}%">` +
                        "<div class=\"overviewValveBatchHeader\">" +
                            `<strong>${escapeHtml(`Batch ${batch.batch}`)}</strong>` +
                            `<span>${escapeHtml(`${overviewNumber(duration, 1)} min`)}</span>` +
                        "</div>" +
                        "<div class=\"overviewValveBatchTrack\"><span></span></div>" +
                        "<div class=\"overviewValveBatchFooter\">" +
                            `<div class="overviewValveChips">${valveChips}</div>` +
                            `<em>${escapeHtml(`${overviewNumber(flow, 3)} L/min`)}</em>` +
                        "</div>" +
                    "</article>"
                );
            }).join("")
            : "<div class=\"overviewValveEmpty\">No scheduled valve batches.</div>";

        return (
            "<div class=\"overviewValveHero\">" +
                "<div>" +
                    "<span>Recommended run mode</span>" +
                    `<strong>${escapeHtml(recommendation)}</strong>` +
                "</div>" +
                `<em class="overviewValveStatus ${fitClass}">${escapeHtml(fitLabel)}</em>` +
            "</div>" +
            "<div class=\"overviewValveGauges\">" +
                `<div class="overviewValveGauge" style="--gauge-share:${runtimeShare}%">` +
                    `<div><span>Optimized runtime</span><strong>${escapeHtml(`${overviewNumber(optimizedRuntime, 1)} min`)}</strong></div>` +
                    "<i><span></span></i>" +
                    `<em>${escapeHtml(runtimeDetail)}</em>` +
                "</div>" +
                `<div class="overviewValveGauge" style="--gauge-share:${flowShare}%">` +
                    `<div><span>Peak tap load</span><strong>${escapeHtml(`${overviewNumber(maxParallelFlow, 3)} L/min`)}</strong></div>` +
                    "<i><span></span></i>" +
                    `<em>${escapeHtml(`${overviewNumber(safeTapFlow, 1)} L/min safe limit`)}</em>` +
                "</div>" +
                "<div class=\"overviewValveGauge overviewValveGaugeWater\" style=\"--gauge-share:100%\">" +
                    `<div><span>Total irrigation volume</span><strong>${escapeHtml(`${overviewNumber(completeVolume, 2)} L`)}</strong></div>` +
                    "<i><span></span></i>" +
                    `<em>${escapeHtml("All active pots, design dose")}</em>` +
                "</div>" +
            "</div>" +
            `<div class="overviewValveSectionHeader"><span>Optimized batches</span><strong>${escapeHtml(`${schedule.length}`)}</strong></div>` +
            `<div class="overviewValveSchedule">${scheduleRows}</div>` +
            `<div class="overviewValveSectionHeader"><span>Zone irrigation priority</span><strong>${escapeHtml(`${priority.length} zones`)}</strong></div>` +
            `<div class="overviewValveList">${priorityRows}</div>`
        );
    }

    function overviewSideRailHtml(state, sensorCoverageHtml, plantOverviewHtml, valvePlanHtml) {
        return (
            "<aside class=\"overviewSideRail\">" +
                "<div class=\"overviewRailStack\">" +
                    "<section class=\"overviewRailCard overviewCurrentStateCard\">" +
                        "<h3>Digital Twin State</h3>" +
                        overviewStateRowHtml("moisture", "Average soil moisture", state.currentMoistureLabel) +
                        overviewStateRowHtml("rain", "Forecast rain (next 3 days)", state.forecastRainLabel) +
                        overviewStateRowHtml("irrigation", "Irrigation recommendation", state.irrigationRecommendation) +
                        overviewStateRowHtmlValueHtml("clock", state.irrigationActivityLabel, state.compactIrrigationActivityHtml) +
                    "</section>" +
                    "<section class=\"overviewRailCard overviewLatestIrrigationCard\">" +
                        "<h3>Recent Activity</h3>" +
                        state.latestIrrigationHtml +
                    "</section>" +
                    "<section class=\"overviewRailCard overviewSensorCoverageCard\">" +
                        "<h3>Sensor Coverage</h3>" +
                        sensorCoverageHtml +
                    "</section>" +
                    "<section class=\"overviewRailCard overviewPlantOverviewCard\">" +
                        "<h3>Pot &amp; Plant Overview</h3>" +
                        plantOverviewHtml +
                    "</section>" +
                "</div>" +
                "<section class=\"overviewRailCard overviewValveCard\">" +
                    "<h3>Valve Priority Plan</h3>" +
                    valvePlanHtml +
                "</section>" +
            "</aside>"
        );
    }

    function overviewStatusHtml(title, detail) {
        return (
            "<aside class=\"overviewSideRail\">" +
                "<section class=\"overviewRailCard\">" +
                    `<h3>${escapeHtml(title)}</h3>` +
                    "<div class=\"overviewRailMetric\">" +
                        `<span>${overviewIconSvg("shield")}<span>Status</span></span>` +
                        `<strong>${escapeHtml(detail)}</strong>` +
                    "</div>" +
                "</section>" +
            "</aside>"
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
                nextIrrigationWindowLabel: NO_IRRIGATION_PLANNED_LABEL,
                irrigationActivityLabel: "Most recent irrigation",
                irrigationActivityValue: NO_IRRIGATION_RECORDED_LABEL,
                compactIrrigationActivityHtml: NO_IRRIGATION_RECORDED_LABEL,
                latestIrrigationHtml: overviewStateRowHtml("irrigation", "Latest run", NO_IRRIGATION_RECORDED_LABEL),
                plannedValvesHtml: ""
            },
            sensorCoverageHtml: "",
            valvePlanHtml: "",
            plantOverviewHtml: "",
            weatherImpactHtml: "",
            sideRailHtml: overviewStatusHtml("Overview loading", "Waiting for current system state"),
            experimentSideRailHtml: ""
        };
    }

    function overviewUnavailable(error) {
        const detail = error && error.message
            ? error.message
            : "Current system state could not be loaded";
        return Object.assign(defaultOverview(), {
            sideRailHtml: overviewStatusHtml("Overview unavailable", detail)
        });
    }

    function experimentSideRailHtml(state, plannedValveRunsHtml, weatherImpactHtml, experiment, summary) {
        return (
            "<aside class=\"overviewSideRail experimentOnlyOverview\">" +
                experimentEvidenceSummaryHtml(experiment, summary) +
                "<details class=\"experimentOverviewContext\" open>" +
                    "<summary class=\"experimentOverviewContextTitle\">Decision Context</summary>" +
                    "<div class=\"experimentOverviewCards\">" +
                        "<section class=\"overviewRailCard overviewStateContextCard\">" +
                            "<h3>Digital Twin State</h3>" +
                            overviewStateRowHtml("moisture", "Average soil moisture", state.currentMoistureLabel) +
                            overviewStateRowHtml("rain", "Forecast rain (next 3 days)", state.forecastRainLabel) +
                            overviewStateRowHtmlValueHtml("clock", state.irrigationActivityLabel, state.compactIrrigationActivityHtml) +
                            overviewStateOptionalRowHtmlValueHtml("valve", "Planned valves", state.plannedValvesHtml) +
                        "</section>" +
                        "<section class=\"overviewRailCard overviewLatestIrrigationCard experimentPlannedValveRunsCard\">" +
                            "<h3>Planned Valve Runs</h3>" +
                            plannedValveRunsHtml +
                        "</section>" +
                        weatherImpactHtml +
                    "</div>" +
                "</details>" +
            "</aside>"
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
            "<div class=\"overviewChartLayout\">" +
                overviewDonutHtml(coverageSegments, totalPots, totalPots || 0, "Pots", "overviewCoverageDonut") +
                `<div class="overviewLegend">${overviewLegendHtml(coverageSegments, totalPots)}</div>` +
            "</div>" +
            overviewSensorCoverageInfoHtml(coverage, coverageSegments, totalPots)
            /*
            Data reliability is disabled for now.
            `<div class="overviewSensorFooter">` +
                `<span><span>Data reliability</span><strong>${escapeHtml(overviewDataReliabilityReason(
                    coverage.data_freshness_pct,
                    state.latest_sensor_recorded_at
                ))}</strong></span>` +
            `</div>`
            */
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
                "<div class=\"overviewPlantRow\">" +
                    `<span><i style="background:${color}"></i>${escapeHtml(item.label)}</span>` +
                    `<strong>${Number(item.count) || 0}</strong>` +
                    `<strong>${overviewNumber(item.avg_moisture_pct, 0)}%</strong>` +
                "</div>"
            );
        }).join("");
        const plantOverviewHtml = (
            "<div class=\"overviewPlantLayout\">" +
                overviewDonutHtml(plantSegments, plantTotal, "", "", "overviewPlantDonut") +
                "<div class=\"overviewPlantTable\">" +
                    "<div class=\"overviewPlantHeader\"><span>Plant species</span><span>Pots</span><span>Avg. moisture</span></div>" +
                    plantRows +
                "</div>" +
            "</div>"
        );
        const valvePlanHtml = overviewValvePlanHtml(valvePlan);
        const weatherImpactHtml = overviewWeatherImpactHtml(overviewWeatherImpact(state));
        const activityState = overviewActivityState(state);

        const stateModel = {
            currentMoistureLabel: `${overviewNumber(state.current_soil_moisture_pct, 0)}%`,
            forecastRainLabel: `${state.forecast_rain_level || "Low"} (${overviewNumber(state.forecast_rain_next_3_days_mm, 0)} mm)`,
            irrigationRecommendation: overviewRecommendationLabel(state),
            confidenceLabel: overviewNumber(state.confidence, 2),
            nextIrrigationWindowLabel: state.next_irrigation_window && state.next_irrigation_window.label
                ? state.next_irrigation_window.label
                : NO_IRRIGATION_PLANNED_LABEL,
            compactNextIrrigationWindowHtml: compactIrrigationWindowHtml(state.next_irrigation_window),
            irrigationActivityLabel: activityState.label,
            irrigationActivityValue: activityState.value,
            compactIrrigationActivityHtml: activityState.compactHtml,
            latestIrrigationHtml: overviewLatestIrrigationHtml(state.recent_irrigation_window),
            plannedValvesHtml: ""
        };

        return {
            loaded: true,
            state: stateModel,
            sensorCoverageHtml,
            valvePlanHtml,
            plantOverviewHtml,
            weatherImpactHtml,
            sideRailHtml: overviewSideRailHtml(
                stateModel,
                sensorCoverageHtml,
                plantOverviewHtml,
                valvePlanHtml
            ),
            experimentSideRailHtml: experimentSideRailHtml(
                stateModel,
                "",
                weatherImpactHtml,
                null,
                null
            )
        };
    }

    return {
        defaultOverview,
        experimentSideRailHtml,
        overviewUnavailable,
        prepareOverview
    };
});
