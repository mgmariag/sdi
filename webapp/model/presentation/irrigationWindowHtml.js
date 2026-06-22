sap.ui.define([
    "disertatie/model/presentation/basicFormat",
    "disertatie/model/presentation/overviewWidgets"
], (BasicFormat, OverviewWidgets) => {
    "use strict";

    const { escapeHtml } = BasicFormat;
    const {
        NO_IRRIGATION_PLANNED_LABEL,
        NO_IRRIGATION_RECORDED_LABEL,
        overviewNumber,
        overviewStateRowHtml,
        overviewStateRowHtmlValueHtml,
        overviewZoneLabel
    } = OverviewWidgets;
    function irrigationWindowValveLabel(window) {
        return window && window.activated_valves && window.activated_valves !== "none"
            ? String(window.activated_valves)
            : "";
    }

    function irrigationWindowValveLabelHtml(windowOrLabel) {
        const label = typeof windowOrLabel === "string"
            ? windowOrLabel
            : irrigationWindowValveLabel(windowOrLabel);
        return label
            ? escapeHtml(label).replace(/V(\d+)-V(\d+)/g, "V$1&ndash;V$2")
            : "";
    }

    function irrigationWindowWaterLabel(window) {
        const liters = Number(window && window.planned_volume_l);
        return Number.isFinite(liters) && liters > 0
            ? `${overviewNumber(liters, 2)} L`
            : "0 L";
    }

    function overviewLatestIrrigationHtml(window) {
        if (!window || !window.start_at) {
            return overviewStateRowHtml("irrigation", "Latest run", NO_IRRIGATION_RECORDED_LABEL);
        }
        const valves = Array.isArray(window.valves) ? window.valves : [];
        const listClass = valves.length > 3
            ? "overviewLatestIrrigationList overviewLatestIrrigationListTwoColumns"
            : "overviewLatestIrrigationList";
        const valveRowsHtml = valves.length
            ? overviewLatestIrrigationValveRowsHtml(valves)
            : overviewStateRowHtmlValueHtml("valve", "Activated valves", irrigationWindowValveLabelHtml(window) || escapeHtml("No valves"));
        return (
            `<div class="${listClass}">${valveRowsHtml}</div>` +
            "<div class=\"overviewLatestIrrigationTotal\">" +
                "<span>Total water</span>" +
                `<strong>${escapeHtml(irrigationWindowWaterLabel(window))}</strong>` +
            "</div>"
        );
    }

    function experimentPlannedValveRunsHtml(window) {
        if (!window || !window.start_at) {
            return overviewStateRowHtml("valve", "Planned runs", "No planned run");
        }
        return overviewLatestIrrigationHtml(window);
    }

    function overviewLatestIrrigationValveRowsHtml(valves) {
        const maxLiters = valves.reduce((maxValue, valve) => {
            const liters = Number(valve && valve.planned_volume_l);
            return Number.isFinite(liters) ? Math.max(maxValue, liters) : maxValue;
        }, 0);
        return valves.map((valve) => {
            const liters = Number(valve && valve.planned_volume_l);
            const safeLiters = Number.isFinite(liters) ? liters : 0;
            const share = maxLiters > 0 ? Math.max(4, Math.min(100, safeLiters / maxLiters * 100)) : 0;
            const valveNumber = Number(valve && valve.valve_number);
            const valveLabel = Number.isFinite(valveNumber) ? `V${valveNumber}` : "Valve";
            const valveName = valve && (valve.valve_name || overviewZoneLabel(valve.valve_zone));
            return (
                `<div class="overviewLatestValveRow" style="--overview-valve-share:${share.toFixed(1)}%;">` +
                    "<div class=\"overviewLatestValveHeader\">" +
                        `<span><b>${escapeHtml(valveLabel)}</b>${escapeHtml(valveName || "Unmapped")}</span>` +
                        `<strong>${escapeHtml(`${overviewNumber(safeLiters, 2)} L`)}</strong>` +
                    "</div>" +
                    "<i class=\"overviewLatestValveTrack\"><span></span></i>" +
                "</div>"
            );
        }).join("");
    }

    function compactIrrigationWindowHtml(window) {
        if (!window || !window.label) {
            return NO_IRRIGATION_PLANNED_LABEL;
        }
        const match = String(window.label).match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+-\s+(\d{2}:\d{2})$/);
        return match
            ? (
                "<span class=\"overviewIrrigationWindow\">" +
                    `<span class="overviewIrrigationWindowDate">${escapeHtml(match[1])}</span> ` +
                    `<span class="overviewIrrigationWindowTime">${escapeHtml(`${match[2]} - ${match[3]}`)}</span>` +
                "</span>"
            )
            : escapeHtml(window.label);
    }

    return {
        compactIrrigationWindowHtml,
        experimentPlannedValveRunsHtml,
        irrigationWindowValveLabelHtml,
        overviewLatestIrrigationHtml
    };
});
