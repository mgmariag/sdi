sap.ui.define([
    "disertatie/model/presentation/basicFormat"
], (BasicFormat) => {
    "use strict";

    const { escapeHtml, formatLocalDate } = BasicFormat;
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
            "<div class=\"overviewLegendRow\">" +
                `<span class="overviewSwatch" style="background:${segment.color}"></span>` +
                `<span class="overviewLegendLabel">${escapeHtml(segment.label)}</span>` +
                `<strong>${segment.count} (${overviewPercent(segment.count, total)}%)</strong>` +
            "</div>"
        )).join("");
    }

    function overviewDonutHtml(segments, total, centerValue, centerLabel, className) {
        const gradient = overviewGradient(segments, total);
        return (
            `<div class="overviewDonut ${className || ""}" style="background:${gradient}">` +
                "<div class=\"overviewDonutCenter\">" +
                    `<strong>${escapeHtml(centerValue)}</strong>` +
                    `<span>${escapeHtml(centerLabel)}</span>` +
                "</div>" +
            "</div>"
        );
    }

    function overviewSegmentCount(segments, key) {
        const segment = segments.find((item) => item.key === key);
        return segment ? Number(segment.count) || 0 : 0;
    }

    function overviewSensorCoverageInfoHtml(coverage, segments, totalPots) {
        const sensorNodes = Number(coverage.sensor_nodes) || 0;
        const measuredPots = overviewSegmentCount(segments, "measured");
        const estimatedPots = overviewSegmentCount(segments, "estimated");
        const anchorCount = sensorNodes || measuredPots;
        const anchorText = anchorCount > 0
            ? `<strong>${escapeHtml(anchorCount)}</strong> ${escapeHtml(anchorCount === 1 ? "sensor node" : "sensor nodes")} calibrate ${escapeHtml(totalPots || 0)} active pots`
            : "No sensor nodes are configured yet";
        const estimateText = estimatedPots > 0
            ? `${estimatedPots} pots estimated from plant type, zone and weather`
            : "All active pots have same-day sensor data";
        return (
            "<div class=\"overviewSensorInfo\">" +
                `<p>${anchorText}</p>` +
                `<p>${escapeHtml(estimateText)}</p>` +
            "</div>"
        );
    }

    function overviewIconSvg(type) {
        const paths = {
            moisture: "<path d=\"M12 3.5C9.3 7 7 9.9 7 13a5 5 0 0 0 10 0c0-3.1-2.3-6-5-9.5Z\"/><path d=\"M9.8 14.1c.5 1.2 1.4 1.8 2.7 1.8\"/>",
            rain: "<path d=\"M7.5 17.5h9a4 4 0 0 0 .4-8 5.7 5.7 0 0 0-10.8-1.7A4.8 4.8 0 0 0 7.5 17.5Z\"/><path d=\"M8 20.5v1\"/><path d=\"M12 20.5v1\"/><path d=\"M16 20.5v1\"/>",
            irrigation: "<path d=\"M4 15h8\"/><path d=\"M7 12v6\"/>" +
                "<path d=\"M12 15c3.2 0 4.8-2.6 5.5-6.5-3.9.7-6.5 2.3-6.5 5.5\"/>" +
                "<path d=\"M17 8.5 20 5.5\"/>" +
                "<path d=\"M15.3 18.5c1.8 0 3.2-1.2 3.2-2.8 0-1.8-1.6-3.8-3.2-5.7-" +
                "1.6 1.9-3.2 3.9-3.2 5.7 0 1.6 1.4 2.8 3.2 2.8Z\"/>",
            shield: "<path d=\"M12 3.5 19 6v5.3c0 4.3-2.8 7.7-7 9.2-4.2-1.5-7-4.9-7-9.2V6l7-2.5Z\"/><path d=\"m9 12 2 2 4-4\"/>",
            clock: "<circle cx=\"12\" cy=\"12\" r=\"8\"/><path d=\"M12 7.5V12l3 2\"/>",
            temperature: "<path d=\"M10 14.5V5.8a2 2 0 1 1 4 0v8.7a4 4 0 1 1-4 0Z\"/><path d=\"M12 8v7\"/><path d=\"M9 19h6\"/>",
            valve: "<path d=\"M4 8h16\"/><path d=\"M8 8V5h8v3\"/><path d=\"M10 5 8 3\"/><path d=\"m14 5 2-2\"/><path d=\"M7 12h10\"/><path d=\"M9 12v6\"/><path d=\"M15 12v6\"/><path d=\"M6 18h12\"/>"
        };
        return (
            `<span class="overviewRailIcon overviewRailIcon-${type}">` +
                `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[type] || paths.moisture}</svg>` +
            "</span>"
        );
    }

    function overviewStateRowHtml(icon, label, value) {
        return (
            "<div class=\"overviewRailMetric\">" +
                `<span>${overviewIconSvg(icon)}<span>${escapeHtml(label)}</span></span>` +
                `<strong>${escapeHtml(value)}</strong>` +
            "</div>"
        );
    }

    function overviewStateRowHtmlValueHtml(icon, label, valueHtml) {
        return (
            "<div class=\"overviewRailMetric\">" +
                `<span>${overviewIconSvg(icon)}<span>${escapeHtml(label)}</span></span>` +
                `<strong>${valueHtml}</strong>` +
            "</div>"
        );
    }

    function overviewStateOptionalRowHtmlValueHtml(icon, label, valueHtml) {
        return valueHtml
            ? overviewStateRowHtmlValueHtml(icon, label, valueHtml)
            : "";
    }
    function overviewZoneLabel(zone) {
        return String(zone || "Zone").replace(/_/g, " ");
    }

    return {
        NO_IRRIGATION_PLANNED_LABEL,
        NO_IRRIGATION_RECORDED_LABEL,
        OVERVIEW_PALETTE,
        overviewClampPercent,
        overviewDonutHtml,
        overviewIconSvg,
        overviewLegendHtml,
        overviewNumber,
        overviewRecommendationLabel,
        overviewSegmentCount,
        overviewSegments,
        overviewSensorCoverageInfoHtml,
        overviewStateOptionalRowHtmlValueHtml,
        overviewStateRowHtml,
        overviewStateRowHtmlValueHtml,
        overviewZoneLabel
    };
});
