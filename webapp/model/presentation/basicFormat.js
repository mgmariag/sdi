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

    function summaryProbabilityPercent(value, decimals) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue)) {
            return `${summaryNumber(0, decimals)}%`;
        }
        const percentValue = Math.abs(numberValue) <= 1 ? numberValue * 100 : numberValue;
        return `${summaryNumber(percentValue, decimals)}%`;
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

    function summaryDuration(value) {
        const numberValue = Number(value);
        if (!Number.isFinite(numberValue) || numberValue <= 0) {
            return "0.00 s";
        }
        return numberValue < 10 ? `${summaryNumber(numberValue, 2)} s` : `${summaryNumber(numberValue, 1)} s`;
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

    return {
        addDays,
        escapeHtml,
        formatChartGranularity,
        formatLocalDate,
        parseLocalDate,
        summaryDuration,
        summaryInteger,
        summaryNumber,
        summaryPercentChange,
        summaryProbabilityPercent,
        summaryReducedCount,
        summarySignedInteger
    };
});
