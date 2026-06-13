sap.ui.define([
    "disertatie/model/presentation/basicFormat"
], (BasicFormat) => {
    "use strict";

    const {
        escapeHtml,
        summaryInteger,
        summaryNumber,
        summaryPercentChange,
        summaryProbabilityPercent,
        summaryReducedCount,
        summarySignedInteger
    } = BasicFormat;
    function samplingWaterUseAcceptable(summary, waterSavedPercent) {
        const waterSavedLiters = summaryReducedCount(summary.sparse_total_water_usage_l, summary.baseline_total_water_usage_l);
        const extraWaterLiters = Math.max(0, -waterSavedLiters);
        return waterSavedPercent >= 0 || waterSavedPercent >= -0.5 || extraWaterLiters <= 1;
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
            "<div class=\"experimentEvidenceMetric\">" +
                `<span>${escapeHtml(label)}</span>` +
                `<strong>${escapeHtml(value)}</strong>` +
                `<em>${escapeHtml(detail || "")}</em>` +
            "</div>"
        );
    }

    function evidencePanelHtml(title, verdict, tone, metrics, note) {
        return (
            `<section class="experimentEvidencePanel experimentEvidencePanel-${tone}">` +
                "<div class=\"experimentEvidenceHeader\">" +
                    `<span>${escapeHtml(title)}</span>` +
                    `<strong>${escapeHtml(verdict)}</strong>` +
                "</div>" +
                `<div class="experimentEvidenceGrid">${metrics.join("")}</div>` +
                `<p>${escapeHtml(note)}</p>` +
            "</section>"
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

    function evidenceValveActivationShiftDetail(missedActivations, extraActivations) {
        const missed = Math.abs(Number(missedActivations) || 0);
        const extra = Math.abs(Number(extraActivations) || 0);
        return `valve activations ${summarySignedInteger(-missed)}/${summarySignedInteger(extra)}`;
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
        const validationSamples = Number(summary.evaluation_samples) || Number(summary.test_samples) || 0;
        const operatingThreshold = summary.anfis_probability_threshold || summary.test_decision_threshold;
        const modelSource = String(summary.anfisModelSource || "model").replace(/_/g, " ");
        const decisionAccuracy = Number(summary.test_decision_accuracy_percent) || 0;
        const probabilityFit = Number(summary.test_probability_fit_percent) || 0;
        const baselineAgreement = Number(summary.baseline_agreement_percent) || 0;
        let tone = "good";
        if (validationSamples > 0 && (decisionAccuracy < 50 || probabilityFit < 50)) {
            tone = "risk";
        } else if (validationSamples > 0 && decisionAccuracy < 70) {
            tone = "watch";
        }
        const note =
            `ANFIS-GA uses a ${modelSource} trained from sensor and weather data. ` +
            "It is compared against the default strategy under the same " +
            "weather and pot conditions.";
        return evidencePanelHtml(
            "Summary",
            "IRRIGATION PROBABILITY MODEL",
            tone,
            [
                evidenceMetricHtml(
                    "Baseline agreement",
                    `${summaryNumber(baselineAgreement, 1)}%`,
                    `${summaryInteger(summary.baseline_mismatch_days)} mismatch days; ${evidenceValveActivationShiftDetail(summary.missed_valve_run_delta, summary.anfis_extra_valve_run_delta)}`
                ),
                evidenceMetricHtml(
                    evidenceWaterLabel(waterSavings),
                    evidenceWaterValue(waterSavings, summary.baseline_total_water_usage_l, summary.water_savings_percent),
                    `${evidenceWaterDetail(waterSavings)}`
                ),
                evidenceMetricHtml(
                    "Validation agreement",
                    `${summaryNumber(summary.test_decision_accuracy_percent, 1)}%`,
                    `${summaryInteger(validationSamples)} validation samples; threshold ${summaryProbabilityPercent(operatingThreshold, 0)}`
                ),
                evidenceMetricHtml(
                    "Target probability fit",
                    `${summaryNumber(probabilityFit, 1)}%`,
                    `RMSE ${summaryNumber(summary.test_rmse, 3)}`
                )
            ],
            note
        );
    }

    function fuzzyEvidenceSummaryHtml(summary) {
        const waterSavings = Number(summary.water_savings_l) || 0;
        const averageDailyWaterLiters = (Number(summary.fuzzy_total_water_usage_l) || 0) / Math.max(Number(summary.daysAnalyzed) || 1, 1);
        const averagePrescriptionVolume = Number(summary.average_prescription_volume_l) || averageDailyWaterLiters;
        const averageScore = Math.min(100, Math.max(0, Number(summary.average_prescription_score_pct) || 0));
        const baselineAgreement = Number(summary.baseline_agreement_percent ?? summary.accuracy_percent) || 0;
        const mismatchDays = Number(summary.baseline_mismatch_days ?? summary.mismatch_days) || 0;
        const comfortDays = `${summaryInteger(summary.comfort_preserved_days)}/${summaryInteger(summary.daysAnalyzed)}`;
        let tone = "good";
        if (baselineAgreement < 65 || Number(summary.moisture_safe_savings_percent) < -5) {
            tone = "risk";
        } else if (baselineAgreement < 80) {
            tone = "watch";
        }
        return evidencePanelHtml(
            "Summary",
            "IRRIGATION PRESCRIPTION MODEL",
            tone,
            [
                evidenceMetricHtml(
                    "Baseline agreement",
                    `${summaryNumber(baselineAgreement, 1)}%`,
                    `${summaryInteger(mismatchDays)} mismatch days; ${evidenceValveActivationShiftDetail(summary.missed_valve_run_delta, summary.fuzzy_extra_valve_run_delta)}`
                ),
                evidenceMetricHtml(
                    evidenceWaterLabel(waterSavings),
                    evidenceWaterValue(waterSavings, summary.baseline_total_water_usage_l, summary.water_savings_percent),
                    `${evidenceWaterDetail(waterSavings)}`
                ),
                evidenceMetricHtml(
                    "Prescription signal",
                    `${summaryNumber(averageScore, 1)}%`,
                    `${summaryNumber(averagePrescriptionVolume, 3)} L average; ${summaryNumber(averageDailyWaterLiters, 2)} L/day`
                ),
                evidenceMetricHtml(
                    "Moisture safety",
                    `${summaryNumber(summary.moisture_safe_savings_percent, 1)}%`,
                    `${comfortDays} days preserved; floor ${summaryNumber(summary.comfort_threshold_pct, 1)}%`
                )
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

    return {
        anfisEvidenceSummaryHtml,
        experimentEvidenceSummaryHtml,
        fuzzyEvidenceSummaryHtml,
        samplingEvidenceSummaryHtml
    };
});
