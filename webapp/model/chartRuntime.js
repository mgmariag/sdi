sap.ui.define([
    "disertatie/model/chartBuilder",
    "sap/viz/ui5/format/ChartFormatter",
    "sap/viz/ui5/api/env/Format",
    "sap/viz/ui5/controls/Popover",
    "sap/ui/core/HTML"
], (ChartBuilder, ChartFormatter, Format, Popover, HTML) => {
    "use strict";

    const {
        CHART_DATA_SHAPES,
        CHART_FORMATS,
        CHART_MEASURES,
        CHART_PALETTES,
        INITIAL_VISIBLE_CHART_DAYS,
        SENSOR_DEPENDENT_LINE_MEASURES,
        WEATHER_LINE_MEASURES,
        entryTimestamp
    } = ChartBuilder;

    return {
        onAfterRendering() {
            this._styleCharts();
        },

        _styleCharts() {
            ["samplingChart", "anfisChart", "fuzzyChart"].forEach((chartId) => {
                const chart = this.byId(chartId);
                if (chart) {
                    this._styleChart(chart, chartId);
                }
            });
        },

        _styleChart(chart, chartId) {
            const formatString = CHART_FORMATS[chartId] || {};

            chart.setVizProperties({
                plotArea: {
                    colorPalette: CHART_PALETTES[chartId],
                    dataShape: {
                        primaryAxis: CHART_DATA_SHAPES[chartId] || []
                    },
                    dataPointStyleMode: "update",
                    dataPointStyle: {
                        rules: this._predictionLineStyleRules(chartId)
                    },
                    window: this._initialChartWindow(chartId),
                    dataLabel: {
                        visible: false,
                        formatString
                    },
                    dataPoint: {
                        visible: true
                    },
                    drawingEffect: "normal"
                },
                legend: {
                    visible: true,
                    position: "right",
                    label: {
                        style: { color: "#17324d" }
                    }
                },
                legendGroup: {
                    layout: {
                        position: "right"
                    }
                },
                title: {
                    visible: false
                },
                valueAxis: {
                    label: { style: { color: "#5d7187" } },
                    title: { visible: false }
                },
                categoryAxis: {
                    label: {
                        style: { color: "#5d7187" },
                        rotation: "fixed",
                        angle: 45
                    },
                    title: { visible: false }
                },
                tooltip: {
                    visible: true
                },
                interaction: {
                    zoom: {
                        enablement: "enabled"
                    }
                }
            });

            this._connectChartPopover(chart, chartId);
        },

        _initialChartWindow(chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            if (!Array.isArray(rows) || rows.length === 0) {
                return {
                    start: "firstDataPoint",
                    end: "lastDataPoint"
                };
            }

            const endRow = rows[rows.length - 1];
            const endDate = entryTimestamp(endRow);
            if (!endDate) {
                return {
                    start: "firstDataPoint",
                    end: "lastDataPoint"
                };
            }

            const startDate = new Date(endDate.getTime() - INITIAL_VISIBLE_CHART_DAYS * 24 * 60 * 60 * 1000);
            const startRow = rows.find((row) => {
                const rowDate = entryTimestamp(row);
                return rowDate && rowDate >= startDate;
            }) || rows[0];

            if (startRow === rows[0] && endRow === rows[rows.length - 1]) {
                return {
                    start: "firstDataPoint",
                    end: "lastDataPoint"
                };
            }

            return {
                start: {
                    categoryAxis: {
                        "Date/Time": this._chartRowLabel(startRow)
                    }
                },
                end: {
                    categoryAxis: {
                        "Date/Time": this._chartRowLabel(endRow)
                    }
                }
            };
        },

        _predictionLineStyleRules(chartId) {
            return [
                {
                    callback: (context) => this._shouldDashChartPoint(chartId, context),
                    properties: {
                        lineType: "dash"
                    },
                    displayName: "Prediction / no sensor reading"
                }
            ];
        },

        _shouldDashChartPoint(chartId, context) {
            const label = context && context["Date/Time"];
            if (!label) {
                return false;
            }
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            const row = Array.isArray(rows) ? rows.find((item) => item && this._chartRowLabel(item) === label) : null;
            if (!row) {
                return false;
            }

            const measureName = this._chartMeasureName(chartId, context);
            if (WEATHER_LINE_MEASURES.has(measureName)) {
                return Boolean(row.is_weather_prediction);
            }
            if (SENSOR_DEPENDENT_LINE_MEASURES.has(measureName)) {
                return Boolean(row.is_sensor_missing_reading);
            }
            return false;
        },

        _chartMeasureName(chartId, context) {
            if (!context) {
                return "";
            }
            const measures = CHART_MEASURES[chartId] || [];
            const measureKeys = [
                "MeasureNamesDimension",
                "measureNamesDimension",
                "Measure Names",
                "Measure",
                "measure",
                "MeasureName",
                "measureName"
            ];
            for (const key of measureKeys) {
                if (typeof context[key] === "string" && measures.includes(context[key])) {
                    return context[key];
                }
            }
            return measures.find((measure) => Object.values(context).includes(measure)) || "";
        },

        _chartWindowSignature(chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            if (!Array.isArray(rows) || rows.length === 0) {
                return "";
            }
            const endRow = rows[rows.length - 1];
            const endDate = entryTimestamp(endRow);
            const startDate = endDate ? new Date(endDate.getTime() - INITIAL_VISIBLE_CHART_DAYS * 24 * 60 * 60 * 1000) : null;
            const startIndex = startDate
                ? Math.max(0, rows.findIndex((row) => {
                    const rowDate = entryTimestamp(row);
                    return rowDate && rowDate >= startDate;
                }))
                : 0;
            return [
                rows.length,
                this._chartRowLabel(rows[startIndex]),
                this._chartRowLabel(rows[rows.length - 1])
            ].join("|");
        },

        _applyInitialChartWindow(chartId) {
            const chart = this.byId(chartId);
            const signature = this._chartWindowSignature(chartId);
            if (!chart || !signature) {
                return;
            }

            chart.setVizProperties({
                plotArea: {
                    window: this._initialChartWindow(chartId)
                }
            });
            this._appliedChartWindowSignatures[chartId] = signature;
        },

        _scheduleInitialChartWindow(chartId) {
            const signature = this._chartWindowSignature(chartId);
            if (!signature || this._appliedChartWindowSignatures[chartId] === signature) {
                return;
            }

            (this._chartWindowTimers[chartId] || []).forEach((timerId) => clearTimeout(timerId));
            this._chartWindowTimers[chartId] = [0, 100, 300].map((delay) => setTimeout(() => {
                this._applyInitialChartWindow(chartId);
            }, delay));
        },

        _chartRowLabel(row) {
            return row && (row.chart_label || row.day_label || row.timestamp || row.date || "");
        },

        _connectChartPopover(chart, chartId) {
            const vizUid = chart.getVizUid && chart.getVizUid();
            if (!vizUid) {
                return;
            }

            if (!this._chartPopovers[chartId]) {
                this._chartPopovers[chartId] = new Popover({
                    customDataControl: (data) => this._weatherDetailPopover(data, chartId)
                });
                this.getView().addDependent(this._chartPopovers[chartId]);
            }

            this._chartPopovers[chartId].connect(vizUid);
        },

        _weatherDetailPopover(data, chartId) {
            const measureName = this._extractPopoverMeasureName(data);
            const row = this._findPopoverChartRow(data, chartId);
            const lines = [];

            if (row && measureName === "Max Temp (C)") {
                lines.push(["Temperature", `${this._formatPopoverNumber(row.max_temperature)} C`]);
                lines.push(["Humidity", `${this._formatPopoverNumber(row.humidity)}%`]);
            }
            if (row && measureName === "Rain (L/m2)") {
                lines.push(["Rain", `${this._formatPopoverNumber(row.rain_amount)} L/m2`]);
                lines.push(["Cloud cover", `${this._formatPopoverNumber(row.cloud_cover_pct)}%`]);
            }

            if (!lines.length) {
                return new HTML({ content: "" });
            }

            const content = lines.map(([label, value]) => (
                `<div style="margin:4px 18px 8px 18px;white-space:nowrap;">` +
                `<span style="color:#5d7187;">${label}</span>` +
                `<span style="float:right;margin-left:24px;font-weight:600;color:#17324d;">${value}</span>` +
                `</div>`
            )).join("");
            return new HTML({ content });
        },

        _extractPopoverMeasureName(data) {
            const matches = this._flattenPopoverValues(data);
            if (matches.includes("Max Temp (C)")) {
                return "Max Temp (C)";
            }
            if (matches.includes("Rain (L/m2)")) {
                return "Rain (L/m2)";
            }
            return "";
        },

        _findPopoverChartRow(data, chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            const values = this._flattenPopoverValues(data);
            const strings = values.filter((value) => typeof value === "string");

            return rows.find((row) => strings.includes(row.chart_label))
                || rows.find((row) => strings.includes(row.day_label))
                || rows.find((row) => strings.includes(row.timestamp))
                || rows.find((row) => strings.includes(row.date))
                || this._findPopoverRowByIndex(values, rows);
        },

        _findPopoverRowByIndex(values, rows) {
            const index = values.find((value) => Number.isInteger(value) && value >= 0 && value < rows.length);
            return Number.isInteger(index) ? rows[index] : null;
        },

        _flattenPopoverValues(value) {
            const output = [];
            const visit = (item) => {
                if (item === null || item === undefined) {
                    return;
                }
                if (typeof item === "string" || typeof item === "number") {
                    output.push(item);
                    return;
                }
                if (Array.isArray(item)) {
                    item.forEach(visit);
                    return;
                }
                if (typeof item === "object") {
                    Object.keys(item).forEach((key) => visit(item[key]));
                }
            };
            visit(value);
            return output;
        },

        _chartDataPath(chartId) {
            return {
                samplingChart: "/samplingChartEntries",
                anfisChart: "/anfisChartEntries",
                fuzzyChart: "/fuzzyChartEntries"
            }[chartId] || "/samplingChartEntries";
        },

        _formatPopoverNumber(value) {
            const numberValue = Number(value);
            return Number.isFinite(numberValue) ? numberValue.toFixed(2) : "N/A";
        },

        onChartRenderComplete(event) {
            const chart = event.getSource();
            const localChartId = chart.getId().split("--").pop();
            this._connectChartPopover(chart, localChartId);
            this._trackChartOverlay(chart, localChartId);
            this._scheduleInitialChartWindow(localChartId);
            this._scheduleChartOverlay(localChartId);
        },

        _refreshChart(chartId) {
            const chart = this.byId(chartId);
            if (chart) {
                setTimeout(() => {
                    this._styleChart(chart, chartId);
                    this._trackChartOverlay(chart, chartId);
                    this._scheduleInitialChartWindow(chartId);
                    this._scheduleChartOverlay(chartId);
                }, 0);
            }
        },

        _scheduleChartOverlay(chartId) {
            (this._chartOverlayTimers[chartId] || []).forEach((timerId) => clearTimeout(timerId));
            this._chartOverlayTimers[chartId] = [0, 80, 220, 420].map((delay) => (
                setTimeout(() => this._drawChartOverlay(chartId), delay)
            ));
        },

        _trackChartOverlay(chart, chartId) {
            const chartDom = chart && chart.getDomRef && chart.getDomRef();
            if (!chartDom) {
                return;
            }

            if (!chartDom.dataset.dtOverlayListeners) {
                const schedule = () => this._scheduleChartOverlay(chartId);
                const eventNames = ["wheel", "mouseup", "mouseleave", "touchend", "keyup", "dblclick"];
                eventNames.forEach((eventName) => {
                    chartDom.addEventListener(eventName, schedule, { passive: true });
                });
                this._chartOverlayListenerCleanups[chartId] = () => {
                    eventNames.forEach((eventName) => chartDom.removeEventListener(eventName, schedule));
                };
                chartDom.dataset.dtOverlayListeners = "true";
            }

            const svg = chartDom.querySelector("svg");
            if (!svg || (this._chartOverlayObservers[chartId] && this._chartOverlayObservers[chartId].svg === svg)) {
                return;
            }
            if (this._chartOverlayObservers[chartId]) {
                this._chartOverlayObservers[chartId].observer.disconnect();
            }

            let scheduled = false;
            const observer = new MutationObserver(() => {
                if (scheduled) {
                    return;
                }
                scheduled = true;
                requestAnimationFrame(() => {
                    scheduled = false;
                    this._scheduleChartOverlay(chartId);
                });
            });
            observer.observe(svg, {
                attributes: true,
                childList: true,
                subtree: true
            });
            this._chartOverlayObservers[chartId] = { observer, svg };
        },

        _drawChartOverlay(chartId) {
            const chart = this.byId(chartId);
            const chartDom = chart && chart.getDomRef && chart.getDomRef();
            if (!chartDom) {
                return;
            }

            this._removeSemanticLegendEntries(chartDom);
            const overlay = this._chartOverlayElement(chartDom);
            const geometry = this._chartOverlayGeometry(chartDom, chartId);
            if (!geometry) {
                overlay.innerHTML = "";
                return;
            }

            overlay.innerHTML = "";
            const futureBand = document.createElement("div");
            futureBand.className = "dtChartFutureBand";
            futureBand.style.left = `${geometry.nowX}px`;
            futureBand.style.top = `${geometry.top}px`;
            futureBand.style.width = `${Math.max(0, geometry.right - geometry.nowX)}px`;
            futureBand.style.height = `${geometry.bottom - geometry.top}px`;

            const nowLine = document.createElement("div");
            nowLine.className = "dtChartNowLine";
            nowLine.style.left = `${geometry.nowX}px`;
            nowLine.style.top = `${geometry.top}px`;
            nowLine.style.height = `${geometry.bottom - geometry.top}px`;

            const nowBadge = document.createElement("div");
            nowBadge.className = "dtChartNowBadge";
            nowBadge.textContent = "NOW";
            nowBadge.style.left = `${geometry.nowX}px`;
            nowBadge.style.top = `${Math.max(0, geometry.top - 23)}px`;

            overlay.appendChild(futureBand);
            overlay.appendChild(nowLine);
            overlay.appendChild(nowBadge);
        },

        _chartOverlayElement(chartDom) {
            chartDom.style.position = "relative";
            let overlay = chartDom.querySelector(":scope > .dtChartOverlay");
            if (!overlay) {
                overlay = document.createElement("div");
                overlay.className = "dtChartOverlay";
                chartDom.appendChild(overlay);
            }
            return overlay;
        },

        _chartOverlayGeometry(chartDom, chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            if (!Array.isArray(rows) || rows.length < 2) {
                return null;
            }

            const width = chartDom.clientWidth || 0;
            const height = chartDom.clientHeight || 0;
            if (width <= 0 || height <= 0) {
                return null;
            }

            const plotBounds = this._renderedPlotBounds(chartDom);
            const left = plotBounds ? plotBounds.left : Math.max(38, Math.round(width * 0.045));
            const right = plotBounds ? plotBounds.right : Math.max(left + 1, width - Math.max(26, Math.round(width * 0.035)));
            const top = plotBounds ? plotBounds.top : Math.max(20, Math.round(height * 0.08));
            const bottom = plotBounds ? plotBounds.bottom : Math.max(top + 1, height - Math.max(84, Math.round(height * 0.2)));
            const now = new Date();
            const axisGeometry = this._axisOverlayGeometry(chartDom, rows, { left, right, top, bottom }, now);
            if (axisGeometry) {
                return axisGeometry.visible ? axisGeometry.geometry : null;
            }

            const visibleRows = this._visibleChartRowsForOverlay(rows);
            if (visibleRows.length < 2) {
                return null;
            }

            const startDate = entryTimestamp(visibleRows[0]);
            const endDate = entryTimestamp(visibleRows[visibleRows.length - 1]);
            if (!startDate || !endDate || endDate <= startDate || now < startDate || now > endDate) {
                return null;
            }

            const ratio = Math.max(0, Math.min(1, (now.getTime() - startDate.getTime()) / (endDate.getTime() - startDate.getTime())));
            return {
                left,
                right,
                top,
                bottom,
                nowX: left + (right - left) * ratio
            };
        },

        _axisOverlayGeometry(chartDom, rows, plotBounds, now) {
            const points = this._visibleAxisLabelPoints(chartDom, rows, plotBounds);
            if (points.length < 2) {
                return null;
            }

            const first = points[0];
            const last = points[points.length - 1];
            if (last.date <= first.date || last.x === first.x) {
                return null;
            }

            const projectedNowX = first.x
                + ((now.getTime() - first.date.getTime()) / (last.date.getTime() - first.date.getTime()))
                * (last.x - first.x);
            if (projectedNowX < plotBounds.left - 2 || projectedNowX > plotBounds.right + 2) {
                return { visible: false };
            }

            return {
                visible: true,
                geometry: {
                    left: plotBounds.left,
                    right: plotBounds.right,
                    top: plotBounds.top,
                    bottom: plotBounds.bottom,
                    nowX: Math.max(plotBounds.left, Math.min(plotBounds.right, projectedNowX))
                }
            };
        },

        _visibleAxisLabelPoints(chartDom, rows, plotBounds) {
            const labelRows = new Map();
            rows.forEach((row) => {
                const label = this._chartRowLabel(row);
                const timestamp = entryTimestamp(row);
                if (label && timestamp) {
                    labelRows.set(label, row);
                }
            });
            if (!labelRows.size) {
                return [];
            }

            const chartRect = chartDom.getBoundingClientRect();
            const pointsByTime = new Map();
            chartDom.querySelectorAll("svg text").forEach((node) => {
                const label = (node.textContent || "").trim();
                const row = labelRows.get(label);
                if (!row || !node.getBoundingClientRect) {
                    return;
                }
                const rect = node.getBoundingClientRect();
                if (!rect.width || !rect.height) {
                    return;
                }

                const centerX = rect.left - chartRect.left + rect.width / 2;
                const centerY = rect.top - chartRect.top + rect.height / 2;
                const axisLabel = centerY >= plotBounds.bottom - 10
                    && centerY <= (chartDom.clientHeight || chartRect.height || 0) + 2
                    && centerX >= plotBounds.left - 12
                    && centerX <= plotBounds.right + 12;
                if (!axisLabel) {
                    return;
                }

                const timestamp = entryTimestamp(row);
                if (!timestamp) {
                    return;
                }
                const time = timestamp.getTime();
                const existing = pointsByTime.get(time) || { date: timestamp, xValues: [] };
                existing.xValues.push(centerX);
                pointsByTime.set(time, existing);
            });

            return Array.from(pointsByTime.values())
                .map((point) => ({
                    date: point.date,
                    x: point.xValues.reduce((sum, value) => sum + value, 0) / point.xValues.length
                }))
                .sort((a, b) => a.date - b.date);
        },

        _renderedPlotBounds(chartDom) {
            const svg = chartDom.querySelector("svg");
            if (!svg || !svg.getBoundingClientRect) {
                return null;
            }

            const chartRect = chartDom.getBoundingClientRect();
            const width = chartDom.clientWidth || chartRect.width || 0;
            const height = chartDom.clientHeight || chartRect.height || 0;
            if (width <= 0 || height <= 0) {
                return null;
            }

            const candidates = Array.from(svg.querySelectorAll("rect,path,polygon,polyline")).map((node) => {
                const rect = node.getBoundingClientRect && node.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) {
                    return null;
                }
                return {
                    left: rect.left - chartRect.left,
                    right: rect.right - chartRect.left,
                    top: rect.top - chartRect.top,
                    bottom: rect.bottom - chartRect.top,
                    width: rect.width,
                    height: rect.height,
                    area: rect.width * rect.height
                };
            }).filter((rect) => {
                if (!rect) {
                    return false;
                }
                const insideChart = rect.left >= -2 && rect.top >= -2 && rect.right <= width + 2 && rect.bottom <= height + 2;
                const plausiblePlot = rect.width >= width * 0.35 && rect.height >= height * 0.35 && rect.area <= width * height * 0.88;
                const leavesAxisOrLegendSpace = rect.left >= width * 0.025 || rect.right <= width * 0.96 || rect.bottom <= height * 0.9;
                return insideChart && plausiblePlot && leavesAxisOrLegendSpace;
            });

            if (!candidates.length) {
                return null;
            }

            const best = candidates.sort((a, b) => b.area - a.area)[0];
            return {
                left: Math.max(0, Math.round(best.left)),
                right: Math.min(width, Math.round(best.right)),
                top: Math.max(0, Math.round(best.top)),
                bottom: Math.min(height, Math.round(best.bottom))
            };
        },

        _visibleChartRowsForOverlay(rows) {
            const endRow = rows[rows.length - 1];
            const endDate = entryTimestamp(endRow);
            if (!endDate) {
                return rows;
            }
            const startDate = new Date(endDate.getTime() - INITIAL_VISIBLE_CHART_DAYS * 24 * 60 * 60 * 1000);
            const visibleRows = rows.filter((row) => {
                const rowDate = entryTimestamp(row);
                return rowDate && rowDate >= startDate && rowDate <= endDate;
            });
            return visibleRows.length >= 2 ? visibleRows : rows;
        },

        _removeSemanticLegendEntries(chartDom) {
            chartDom.querySelectorAll("text").forEach((node) => {
                const text = (node.textContent || "").trim();
                if (/^Semantic Range\d*$/.test(text)) {
                    const group = node.closest("g");
                    if (group) {
                        group.style.display = "none";
                    }
                }
            });
        },

        _registerChartFormatters() {
            const formatter = ChartFormatter.getInstance();
            const formatNumber = (value) => {
                const numberValue = Number(value);
                return Number.isFinite(numberValue) ? numberValue.toFixed(2) : value;
            };

            formatter.registerCustomFormatter("DT_PERCENT", (value) => `${formatNumber(value)}%`);
            formatter.registerCustomFormatter("DT_ML", (value) => `${formatNumber(value)} mL`);
            formatter.registerCustomFormatter("DT_L", (value) => `${formatNumber(value)} L`);
            formatter.registerCustomFormatter("DT_CELSIUS", (value) => `${formatNumber(value)} C`);
            formatter.registerCustomFormatter("DT_MM", (value) => `${formatNumber(value)} mm`);
            formatter.registerCustomFormatter("DT_LM2", (value) => `${formatNumber(value)} L/m2`);
            formatter.registerCustomFormatter("DT_NUMBER", (value) => formatNumber(value));
            Format.numericFormatter(formatter);
        },

    };
});
