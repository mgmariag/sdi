sap.ui.define([], () => {
    "use strict";

    const FUZZY_PRESCRIPTION_SCORE_MAX_MM = 8;

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

    function summarySignedInteger(value) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue) || Math.round(numberValue) === 0) {
            return "0";
        }
        return `${numberValue > 0 ? "+" : ""}${summaryInteger(numberValue)}`;
    }

    function summaryReducedCount(experimentValue, baselineValue) {
        return (Number(baselineValue) || 0) - (Number(experimentValue) || 0);
    }

    function samplingWaterUseAcceptable(summary, waterSavedPercent) {
        const waterSavedLiters = summaryReducedCount(summary.sparse_total_water_usage_l, summary.baseline_total_water_usage_l);
        const extraWaterLiters = Math.max(0, -waterSavedLiters);
        return waterSavedPercent >= 0 || waterSavedPercent >= -0.5 || extraWaterLiters <= 1;
    }

    function summaryDuration(value) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue) || numberValue <= 0) {
            return "0.00 s";
        }
        return numberValue < 10 ? `${summaryNumber(numberValue, 2)} s` : `${summaryNumber(numberValue, 1)} s`;
    }

    function samplingStrategyVerdict(summary, waterSavedPercent) {
        const accuracy = Number(summary.accuracy_percent) || 0;
        const missed = Number(summary.missed_valve_run_delta) || 0;
        const unnecessary = Number(summary.sparse_extra_valve_run_delta) || 0;
        const baselineRuns = Number(summary.baseline_valve_run_count) || 0;
        const sparseRuns = Number(summary.sparse_valve_run_count) || 0;
        const baselineOnlyDays = Number(summary.baseline_only_irrigation_days) || 0;
        const netValveDelta = sparseRuns - baselineRuns;
        const missedShare = baselineRuns > 0 ? missed / baselineRuns : 0;
        const unnecessaryShare = baselineRuns > 0 ? unnecessary / baselineRuns : 0;
        const netValveShare = baselineRuns > 0 ? netValveDelta / baselineRuns : 0;
        const valveDayShiftShare = baselineRuns > 0 ? (missed + unnecessary) / baselineRuns : 0;
        const waterUseAcceptable = samplingWaterUseAcceptable(summary, waterSavedPercent);
        const controlledValveIncrease =
            netValveShare <= 0.05 ||
            (waterSavedPercent >= 10 && netValveShare <= 0.1 && valveDayShiftShare <= 0.15);
        if (
            accuracy >= 95 &&
            baselineOnlyDays === 0 &&
            waterSavedPercent >= 0 &&
            netValveDelta <= 0 &&
            valveDayShiftShare <= 0.35
        ) {
            return {
                tone: "good",
                label: "GOOD TRADE-OFF"
            };
        }
        if (
            accuracy >= 92 &&
            baselineOnlyDays === 0 &&
            missedShare <= 0.18 &&
            unnecessaryShare <= 0.12 &&
            controlledValveIncrease &&
            valveDayShiftShare <= 0.25 &&
            waterUseAcceptable
        ) {
            return {
                tone: "good",
                label: "GOOD TRADE-OFF"
            };
        }
        if (
            accuracy >= 95 &&
            baselineOnlyDays === 0 &&
            missed === 0 &&
            waterSavedPercent >= 10 &&
            unnecessaryShare <= 0.15 &&
            netValveShare <= 0.15 &&
            valveDayShiftShare <= 0.2
        ) {
            return {
                tone: "good",
                label: "GOOD WATER-SAVING"
            };
        }
        if (
            accuracy < 75 ||
            baselineOnlyDays > 2 ||
            missedShare > 0.35 ||
            unnecessaryShare > 0.25 ||
            valveDayShiftShare > 0.4 ||
            waterSavedPercent < -5
        ) {
            return {
                tone: "risk",
                label: "NOT RECOMMENDED"
            };
        }
        if (
            waterUseAcceptable &&
            accuracy >= 80 &&
            baselineOnlyDays <= 1 &&
            missedShare <= 0.3 &&
            valveDayShiftShare <= 0.4
        ) {
            return {
                tone: "watch",
                label: "NEEDS REVIEW"
            };
        }
        return {
            tone: "risk",
            label: "NOT RECOMMENDED"
        };
    }

    function evidenceMetricHtml(label, value, detail) {
        return (
            `<div class="experimentEvidenceMetric">` +
                `<span>${escapeHtml(label)}</span>` +
                `<strong>${escapeHtml(value)}</strong>` +
                `<em>${escapeHtml(detail || "")}</em>` +
            `</div>`
        );
    }

    function evidencePanelHtml(title, verdict, tone, metrics, note) {
        return (
            `<section class="experimentEvidencePanel experimentEvidencePanel-${tone}">` +
                `<div class="experimentEvidenceHeader">` +
                    `<span>${escapeHtml(title)}</span>` +
                    `<strong>${escapeHtml(verdict)}</strong>` +
                `</div>` +
                `<div class="experimentEvidenceGrid">${metrics.join("")}</div>` +
                `<p>${escapeHtml(note)}</p>` +
            `</section>`
        );
    }

    function evidenceWaterLabel(savedLiters) {
        return savedLiters >= 0 ? "Water saved" : "Extra water";
    }

    function evidenceWaterValue(savedLiters, baselineLiters, fallbackPercent) {
        const percentValue = Number(fallbackPercent);
        const percentText = Number.isFinite(percentValue)
            ? `${summaryNumber(Math.abs(percentValue), 2)}%`
            : summaryPercentChange(Math.abs(savedLiters), baselineLiters);
        return `${savedLiters >= 0 ? "" : "+"}${percentText}`;
    }

    function evidenceWaterDetail(savedLiters) {
        return `${savedLiters >= 0 ? "" : "+"}${summaryNumber(Math.abs(savedLiters), 2)} L vs baseline`;
    }

    function samplingEvidenceSummaryHtml(summary) {
        const waterSaved = summaryReducedCount(summary.sparse_total_water_usage_l, summary.baseline_total_water_usage_l);
        const baselineWater = Number(summary.baseline_total_water_usage_l) || 0;
        const waterSavedPercent = baselineWater > 0 ? waterSaved / baselineWater * 100 : 0;
        const baselineValveRuns = Number(summary.baseline_valve_run_count) || 0;
        const valveRunDelta = (Number(summary.sparse_valve_run_count) || 0) - baselineValveRuns;
        const valveDayShiftCount = (Number(summary.missed_valve_run_delta) || 0) + (Number(summary.sparse_extra_valve_run_delta) || 0);
        const valveDayShiftPercent = baselineValveRuns > 0 ? valveDayShiftCount / baselineValveRuns * 100 : 0;
        const verdict = samplingStrategyVerdict(summary, waterSavedPercent);
        const metrics = [
            evidenceMetricHtml(
                evidenceWaterLabel(waterSaved),
                evidenceWaterValue(waterSaved, summary.baseline_total_water_usage_l),
                evidenceWaterDetail(waterSaved)
            ),
            evidenceMetricHtml(
                "Decision agreement",
                `${summaryNumber(summary.accuracy_percent, 1)}%`,
                `${summaryInteger(summary.mismatch_days)} mismatch days`
            ),
            evidenceMetricHtml(
                "Valve activations",
                `${summaryInteger(summary.sparse_valve_run_count)}/${summaryInteger(summary.baseline_valve_run_count)}`,
                `delta ${summarySignedInteger(valveRunDelta)} vs baseline`
            ),
            evidenceMetricHtml(
                "Valve shift",
                `${summaryNumber(valveDayShiftPercent, 1)}%`,
                `${summaryInteger(summary.missed_valve_run_delta)} missed / ${summaryInteger(summary.sparse_extra_valve_run_delta)} extra activations`
                //`missed / extra activations ${summaryNumber(valveDayShiftPercent, 1)}% gross valve-run shift`
            )
        ];
        return evidencePanelHtml(
            "Summary",
            verdict.label,
            verdict.tone,
            metrics,
            `${summaryInteger(summary.sample_interval_hours)}-hour sampling is compared against the default strategy over the same weather and pot conditions.`
        );
    }

    function anfisEvidenceSummaryHtml(summary) {
        const waterSavings = summaryReducedCount(summary.anfis_total_water_usage_l, summary.baseline_total_water_usage_l);
        const valveRunsReduced = summaryReducedCount(summary.anfis_valve_run_count, summary.baseline_valve_run_count);
        return evidencePanelHtml(
            "Summary",
            waterSavings >= 0 ? "WATER-SAVING MODEL" : "HIGHER WATER USE",
            "good",
            [
                evidenceMetricHtml(evidenceWaterLabel(waterSavings), evidenceWaterValue(waterSavings, summary.baseline_total_water_usage_l), evidenceWaterDetail(waterSavings)),
                evidenceMetricHtml("Valve activations", `${summaryInteger(summary.anfis_valve_run_count)}/${summaryInteger(summary.baseline_valve_run_count)}`, `${summaryInteger(valveRunsReduced)} fewer vs baseline`),
                evidenceMetricHtml("Test accuracy", `${summaryNumber(summary.test_accuracy_percent, 1)}%`, "calibrated ANFIS-GA model"),
                evidenceMetricHtml("Confidence", summaryNumber(summary.predicted_probability_mean, 2), `max ${summaryNumber(summary.predicted_probability_max, 2)}`)
            ],
            `ANFIS-GA is compared against the default strategy over the same weather and pot conditions. Execution time: ${summaryDuration(summary.execution_time_seconds)}.`
        );
    }

    function fuzzyEvidenceSummaryHtml(summary) {
        const valveRunsReduced = summaryReducedCount(summary.fuzzy_valve_run_count, summary.baseline_valve_run_count);
        const waterSavings = Number(summary.water_savings_l) || 0;
        const averagePrescriptionMm = Number(summary.average_prescription_mm) || 0;
        const averagePrescriptionLiters = (Number(summary.fuzzy_total_water_usage_l) || 0) / Math.max(Number(summary.daysAnalyzed) || 1, 1);
        const averageScore = Math.min(100, Math.max(0, averagePrescriptionMm / FUZZY_PRESCRIPTION_SCORE_MAX_MM * 100));
        return evidencePanelHtml(
            "Summary",
            waterSavings >= 0 ? "WATER-SAVING MODEL" : "HIGHER WATER USE",
            "good",
            [
                evidenceMetricHtml(evidenceWaterLabel(waterSavings), evidenceWaterValue(waterSavings, summary.baseline_total_water_usage_l, summary.water_savings_percent), evidenceWaterDetail(waterSavings)),
                evidenceMetricHtml("Valve activations", `${summaryInteger(summary.fuzzy_valve_run_count)}/${summaryInteger(summary.baseline_valve_run_count)}`, `${summaryInteger(valveRunsReduced)} fewer vs baseline`),
                evidenceMetricHtml("Avg. prescription", `${summaryNumber(averagePrescriptionLiters, 2)} L`, `${summaryNumber(averageScore, 1)}% fuzzy score`),
                evidenceMetricHtml("Execution time", summaryDuration(summary.execution_time_seconds), "fuzzy inference")
            ],
            "Fuzzy Control is compared against the default strategy over the same weather and pot conditions."
        );
    }

    function experimentEvidenceSummaryHtml(experiment, summary) {
        const content = {
            sampling: samplingEvidenceSummaryHtml,
            anfis: anfisEvidenceSummaryHtml,
            fuzzy: fuzzyEvidenceSummaryHtml
        }[experiment];
        return content ? content(summary || {}) : "";
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
    const NO_IRRIGATION_PLANNED_LABEL = "No irrigation planned";
    const NO_IRRIGATION_RECORDED_LABEL = "No irrigation recorded";
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

    function parseOverviewDateTime(value) {
        if (!value) {
            return null;
        }
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function overviewTimeLabel(date) {
        if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
            return "";
        }
        return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    }

    function overviewDateTimeLabel(date) {
        if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
            return "";
        }
        const dateLabel = formatLocalDate(date);
        return dateLabel === formatLocalDate(new Date())
            ? overviewTimeLabel(date)
            : `${dateLabel} ${overviewTimeLabel(date)}`;
    }

    function overviewRecommendationLabel(state) {
        const status = state.irrigation_recommendation || "OFF";
        const readyAt = parseOverviewDateTime(state.next_recommendation_ready_at);
        const readyLabel = overviewDateTimeLabel(readyAt);
        return readyLabel ? `${status}, next ready at ${readyLabel}` : status;
    }

    function overviewActivityState(state) {
        const activity = state.irrigation_activity || {};
        const fallback = activity.label ? activity : (state.next_irrigation_window || {});
        return {
            label: activity.display_label || "Most recent irrigation",
            value: fallback.label || NO_IRRIGATION_PLANNED_LABEL,
            compactHtml: compactIrrigationWindowHtml(fallback)
        };
    }

    function overviewClampPercent(value) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue)) {
            return 0;
        }
        return Math.max(0, Math.min(100, numberValue));
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
            temperature: `<path d="M10 14.5V5.8a2 2 0 1 1 4 0v8.7a4 4 0 1 1-4 0Z"/><path d="M12 8v7"/><path d="M9 19h6"/>`,
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

    function overviewStateRowHtmlValueHtml(icon, label, valueHtml) {
        return (
            `<div class="overviewRailMetric">` +
                `<span>${overviewIconSvg(icon)}<span>${escapeHtml(label)}</span></span>` +
                `<strong>${valueHtml}</strong>` +
            `</div>`
        );
    }

    function compactIrrigationWindowHtml(window) {
        if (!window || !window.label) {
            return NO_IRRIGATION_PLANNED_LABEL;
        }
        const valves = window.activated_valves && window.activated_valves !== "none"
            ? escapeHtml(window.activated_valves)
            : "";
        const match = String(window.label).match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+-\s+(\d{2}:\d{2})$/);
        const timeLabel = match
            ? `${escapeHtml(match[1])} ${escapeHtml(`${match[2]}-${match[3]}`)}`
            : escapeHtml(window.label);
        if (!valves) {
            return timeLabel;
        }
        return (
            `<span class="overviewIrrigationWindow">` +
                `<span class="overviewIrrigationWindowTime">${timeLabel}</span>` +
                `<span class="overviewIrrigationValves">Active valves: ${valves}</span>` +
            `</span>`
        );
    }

    /*
    Data reliability is disabled for now.

    function overviewReadingAgeLabel(recordedAt) {
        if (!recordedAt) {
            return "";
        }
        const recordedDate = new Date(recordedAt);
        const recordedTime = recordedDate.getTime();
        if (Number.isNaN(recordedTime)) {
            return "";
        }
        const ageMinutes = Math.max(0, Math.round((Date.now() - recordedTime) / 60000));
        if (ageMinutes < 1) {
            return "less than 1 minute ago";
        }
        if (ageMinutes < 60) {
            return `${ageMinutes} minute${ageMinutes === 1 ? "" : "s"} ago`;
        }
        const ageHours = Math.round(ageMinutes / 60);
        if (ageHours < 48) {
            return `${ageHours} hour${ageHours === 1 ? "" : "s"} ago`;
        }
        const ageDays = Math.round(ageHours / 24);
        return `${ageDays} day${ageDays === 1 ? "" : "s"} ago`;
    }

    function overviewDataReliabilityReason(freshnessPercent, recordedAt) {
        const freshness = Number(freshnessPercent) || 0;
        const ageLabel = overviewReadingAgeLabel(recordedAt);
        const suffix = ageLabel ? ` from ${ageLabel}` : "";
        if (freshness >= 95) {
            return `Fresh sensor readings${suffix}`;
        }
        if (freshness >= 80) {
            return `Recent sensor readings${suffix}`;
        }
        if (freshness >= 60) {
            return `Sensor readings are aging${suffix}`;
        }
        if (freshness > 0) {
            return `Stale sensor readings${suffix}`;
        }
        return "No recent sensor readings";
    }
    */

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
            `<section class="overviewRailCard overviewWeatherImpactCard">` +
                `<h3>Weather Impact</h3>` +
                overviewStateRowHtml("irrigation", "Irrigation reduction", impact.irrigationReductionLabel) +
                overviewStateRowHtml("temperature", "Hot-day mode", impact.hotDayModeLabel) +
                `<div class="overviewRailMetric overviewWeatherReason">` +
                    `<span>${overviewIconSvg("shield")}<span>Reason</span></span>` +
                    `<strong>${escapeHtml(impact.reasonLabel)}</strong>` +
                `</div>` +
            `</section>`
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
                const priorityLevel = overviewPriorityLevel(item.priority_score);
                const requiresRun = Boolean(item.requires_run || Number(item.immediate_pots) > 0);
                const statusLabel = requiresRun ? "Needs water" : "Standby";
                const statusClass = requiresRun ? "overviewValveTagNeedsWater" : "overviewValveTagStandby";
                return (
                    `<article class="overviewValveRow">` +
                        `<div class="overviewValveRowHeader">` +
                            `<span class="overviewValveRank">${escapeHtml(`#${item.rank || 0}`)}</span>` +
                            `<div>` +
                                `<strong>${escapeHtml(`${valveLabel} - ${overviewZoneLabel(item.zone)}`)}</strong>` +
                                `<em>${escapeHtml(`${affectedPotsInZone} pots`)}</em>` +
                            `</div>` +
                            `<span class="overviewValveTag ${statusClass}">${escapeHtml(statusLabel)}</span>` +
                        `</div>` +
                        `<div class="overviewValveStats">` +
                            `<span><b>${escapeHtml(flowLabel)}</b><em>Flow</em></span>` +
                            `<span><b>${escapeHtml(durationLabel)}</b><em>Runtime</em></span>` +
                            `<span><b>${escapeHtml(priorityLevel)}</b><em>Priority</em></span>` +
                        `</div>` +
                    `</article>`
                );
            }).join("")
            : `<div class="overviewValveEmpty">No active valve zones to prioritize.</div>`;
        const scheduleRows = schedule.length
            ? schedule.map((batch) => {
                const valves = Array.isArray(batch.valves) ? batch.valves : [];
                const valveChips = valves.length
                    ? valves.map((valve) => `<span>${escapeHtml(`V${valve.valve_number}`)}</span>`).join("")
                    : `<span>Idle</span>`;
                const duration = Number(batch.duration_min) || 0;
                const flow = Number(batch.flow_l_min) || 0;
                const durationShare = optimizedRuntime > 0 ? overviewClampPercent((duration / optimizedRuntime) * 100) : 100;
                const batchFlowShare = safeTapFlow > 0 ? overviewClampPercent((flow / safeTapFlow) * 100) : 0;
                return (
                    `<article class="overviewValveBatch" style="--batch-share:${durationShare}%; --flow-share:${batchFlowShare}%">` +
                        `<div class="overviewValveBatchHeader">` +
                            `<strong>${escapeHtml(`Batch ${batch.batch}`)}</strong>` +
                            `<span>${escapeHtml(`${overviewNumber(duration, 1)} min`)}</span>` +
                        `</div>` +
                        `<div class="overviewValveBatchTrack"><span></span></div>` +
                        `<div class="overviewValveBatchFooter">` +
                            `<div class="overviewValveChips">${valveChips}</div>` +
                            `<em>${escapeHtml(`${overviewNumber(flow, 3)} L/min`)}</em>` +
                        `</div>` +
                    `</article>`
                );
            }).join("")
            : `<div class="overviewValveEmpty">No scheduled valve batches.</div>`;

        return (
            `<div class="overviewValveHero">` +
                `<div>` +
                    `<span>Recommended run mode</span>` +
                    `<strong>${escapeHtml(recommendation)}</strong>` +
                `</div>` +
                `<em class="overviewValveStatus ${fitClass}">${escapeHtml(fitLabel)}</em>` +
            `</div>` +
            `<div class="overviewValveGauges">` +
                `<div class="overviewValveGauge" style="--gauge-share:${runtimeShare}%">` +
                    `<div><span>Optimized runtime</span><strong>${escapeHtml(`${overviewNumber(optimizedRuntime, 1)} min`)}</strong></div>` +
                    `<i><span></span></i>` +
                    `<em>${escapeHtml(runtimeDetail)}</em>` +
                `</div>` +
                `<div class="overviewValveGauge" style="--gauge-share:${flowShare}%">` +
                    `<div><span>Peak tap load</span><strong>${escapeHtml(`${overviewNumber(maxParallelFlow, 3)} L/min`)}</strong></div>` +
                    `<i><span></span></i>` +
                    `<em>${escapeHtml(`${overviewNumber(safeTapFlow, 1)} L/min safe limit`)}</em>` +
                `</div>` +
                `<div class="overviewValveGauge overviewValveGaugeWater" style="--gauge-share:100%">` +
                    `<div><span>Complete irrigation</span><strong>${escapeHtml(`${overviewNumber(completeVolume, 2)} L`)}</strong></div>` +
                    `<i><span></span></i>` +
                    `<em>${escapeHtml("All active pots, design dose")}</em>` +
                `</div>` +
            `</div>` +
            `<div class="overviewValveSectionHeader"><span>Optimized batches</span><strong>${escapeHtml(`${schedule.length}`)}</strong></div>` +
            `<div class="overviewValveSchedule">${scheduleRows}</div>` +
            `<div class="overviewValveSectionHeader"><span>Priority order</span><strong>${escapeHtml(`${priority.length} zones`)}</strong></div>` +
            `<div class="overviewValveList">${priorityRows}</div>`
        );
    }

    function overviewSideRailHtml(state, sensorCoverageHtml, plantOverviewHtml, valvePlanHtml) {
        return (
            `<aside class="overviewSideRail">` +
                `<div class="overviewRailStack">` +
                    `<section class="overviewRailCard">` +
                        `<h3>Current State</h3>` +
                        overviewStateRowHtml("moisture", "Soil moisture", state.currentMoistureLabel) +
                        overviewStateRowHtml("rain", "Forecast rain (next 3 days)", state.forecastRainLabel) +
                        overviewStateRowHtml("irrigation", "Irrigation recommendation", state.irrigationRecommendation) +
                        overviewStateRowHtml("clock", state.irrigationActivityLabel, state.irrigationActivityValue) +
                    `</section>` +
                    `<section class="overviewRailCard">` +
                        `<h3>Sensor Coverage</h3>` +
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
                nextIrrigationWindowLabel: NO_IRRIGATION_PLANNED_LABEL,
                irrigationActivityLabel: "Most recent irrigation",
                irrigationActivityValue: NO_IRRIGATION_RECORDED_LABEL,
                compactIrrigationActivityHtml: NO_IRRIGATION_RECORDED_LABEL
            },
            sensorCoverageHtml: "",
            valvePlanHtml: "",
            plantOverviewHtml: "",
            weatherImpactHtml: "",
            sideRailHtml: "",
            experimentSideRailHtml: ""
        };
    }

    function experimentSideRailHtml(state, plantOverviewHtml, weatherImpactHtml, experiment, summary) {
    return (
        `<aside class="overviewSideRail experimentOnlyOverview">` +
            experimentEvidenceSummaryHtml(experiment, summary) +
            `<details class="experimentOverviewContext" open>` +
                `<summary class="experimentOverviewContextTitle">Decision Context</summary>` +
                `<div class="experimentOverviewCards">` +
                    `<section class="overviewRailCard overviewStateContextCard">` +
                        `<h3>Current State</h3>` +
                        overviewStateRowHtml("moisture", "Current soil moisture", state.currentMoistureLabel) +
                        overviewStateRowHtml("rain", "Forecast rain (next 3 days)", state.forecastRainLabel) +
                        overviewStateRowHtmlValueHtml("clock", state.irrigationActivityLabel, state.compactIrrigationActivityHtml) +
                    `</section>` +
                    weatherImpactHtml +
                    `<section class="overviewRailCard">` +
                        `<h3>Pot &amp; Plant Overview</h3>` +
                        plantOverviewHtml +
                    `</section>` +
                `</div>` +
            `</details>` +
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
            `</div>`
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
            compactIrrigationActivityHtml: activityState.compactHtml
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
                plantOverviewHtml,
                weatherImpactHtml,
                null,
                null
            )
        };
    }

    return {
        addDays,
        defaultOverview,
        escapeHtml,
        compactIrrigationWindowHtml,
        experimentSideRailHtml,
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
