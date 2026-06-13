sap.ui.define([
    "disertatie/model/chart/chartConfig",
    "disertatie/model/chart/chartData",
    "disertatie/model/chart/chartVisibility"
], (ChartConfig, ChartData, ChartVisibility) => {
    "use strict";

    const {
        CHART_MEASURES,
        INITIAL_VISIBLE_CHART_DAYS,
        MOISTURE_THRESHOLD_COLOR,
        MOISTURE_THRESHOLD_PCT
    } = ChartConfig;
    const { entryTimestamp } = ChartData;
    const {
        chartMeasureColor,
        visibleChartDataShapesByAxis,
        visibleChartMeasures
    } = ChartVisibility;

    function chartOverlayMarkup(geometry, chartId, helpers) {
        const px = helpers.px;
        const top = px(geometry.top);
        const chartHeight = px(geometry.bottom - geometry.top);
        const markup = [];
        const futureBandLabel = String(chartId || "").includes("MoistureChart")
            ? "Simulation"
            : "Forecast / Simulation";

        if (Number.isFinite(geometry.nowX)) {
            const nowX = px(geometry.nowX);
            const futureWidth = px(Math.max(0, geometry.right - geometry.nowX));
            markup.push(
                `<div class="dtChartFutureBand" style="left:${nowX};top:${top};width:${futureWidth};height:${chartHeight};">` +
                `<span class="dtChartFutureBandLabel">${futureBandLabel}</span>` +
                "</div>"
            );
        }

        if (Number.isFinite(geometry.thresholdY) && Number.isFinite(geometry.thresholdPct)) {
            const thresholdY = px(geometry.thresholdY);
            const thresholdLeft = px(geometry.left);
            const thresholdWidth = px(geometry.right - geometry.left);
            const thresholdPct = helpers.formatThresholdPct(geometry.thresholdPct);
            const thresholdTitle = `Comfort threshold ${thresholdPct}%`;
            markup.push(
                `<div class="dtChartThresholdLine" title="${thresholdTitle}" ` +
                `aria-label="${thresholdTitle}" tabindex="0" ` +
                `style="left:${thresholdLeft};top:${thresholdY};width:${thresholdWidth};` +
                `border-color:${MOISTURE_THRESHOLD_COLOR};">` +
                `<span class="dtChartThresholdLabel">${thresholdTitle}</span>` +
                "</div>"
            );
        }

        if (Number.isFinite(geometry.nowX)) {
            const nowX = px(geometry.nowX);
            markup.push(
                `<div class="dtChartNowLine" style="left:${nowX};top:${top};height:${chartHeight};"></div>`
            );
            if (helpers.showsBoundaryBadge(chartId)) {
                const badgeTop = px(Math.max(0, geometry.top - 23));
                markup.push(
                    `<div class="dtChartNowBadge" style="left:${nowX};top:${badgeTop};">Forecast boundary</div>`
                );
            }
        }

        return markup.join("");
    }

    return {
        _scheduleChartOverlay(chartId) {
            this._requestChartOverlayFrame(chartId);
            (this._chartOverlayTimers[chartId] || []).forEach((timerId) => clearTimeout(timerId));
            this._chartOverlayTimers[chartId] = [80, 220, 420, 800].map((delay) => (
                setTimeout(() => this._requestChartOverlayFrame(chartId), delay)
            ));
        },

        _requestChartOverlayFrame(chartId) {
            this._chartOverlayFrameIds = this._chartOverlayFrameIds || {};
            if (this._chartOverlayFrameIds[chartId]) {
                return;
            }
            this._chartOverlayFrameIds[chartId] = requestAnimationFrame(() => {
                delete this._chartOverlayFrameIds[chartId];
                this._drawChartOverlay(chartId);
            });
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

            this._thinCategoryAxisLabels(chartDom, chartId);
            this._syncCustomChartLegend(chartDom, chartId);
            const overlay = this._chartOverlayElement(chartDom);
            if (!overlay) {
                return;
            }
            const geometry = this._chartOverlayGeometry(chartDom, chartId);
            if (!geometry) {
                if (!this._hasChartOverlayRows(chartId)) {
                    this._clearChartOverlay(overlay);
                }
                return;
            }

            this._clearChartOverlay(overlay);
            overlay.insertAdjacentHTML("beforeend", this._chartOverlayMarkup(geometry, chartId));
        },

        _hasChartOverlayRows(chartId) {
            const model = this.getView().getModel();
            const rows = model ? model.getProperty(this._chartDataPath(chartId)) : [];
            return Array.isArray(rows) && rows.length >= 2;
        },

        _chartOverlayElement(chartDom) {
            chartDom.style.position = "relative";
            let overlay = chartDom.querySelector(":scope > .dtChartOverlay");
            if (!overlay) {
                chartDom.insertAdjacentHTML("beforeend", "<div class=\"dtChartOverlay\"></div>");
                overlay = chartDom.querySelector(":scope > .dtChartOverlay");
            }
            return overlay;
        },

        _clearChartOverlay(overlay) {
            while (overlay.firstChild) {
                overlay.removeChild(overlay.firstChild);
            }
        },

        _chartOverlayMarkup(geometry, chartId) {
            return chartOverlayMarkup(geometry, chartId, {
                formatThresholdPct: (value) => this._formatThresholdPct(value),
                px: (value) => this._chartOverlayPx(value),
                showsBoundaryBadge: (id) => this._showsBoundaryBadge(id)
            });
        },

        _thinCategoryAxisLabels(chartDom, chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            const rowLabels = new Set(rows.map((row) => this._chartRowLabel(row)).filter(Boolean));
            const rowDateByLabel = this._categoryAxisDateMap(rows);
            const labels = Array.from(chartDom.querySelectorAll("svg text"))
                .filter((element) => this._isCategoryAxisLabel(element, rowLabels));
            if (labels.length < 2) {
                return;
            }

            const labelDates = labels.map((element) => (
                this._categoryAxisLabelDate(String(element.textContent || "").trim(), rowDateByLabel)
            ));
            const firstDate = labelDates.find(Boolean);
            if (!firstDate) {
                this._applyCategoryAxisLabelStep(labels, 2);
                return;
            }

            const firstDay = this._categoryAxisDayStart(firstDate);
            const shownDayKeys = new Set();
            labels.forEach((element, index) => {
                const labelDate = labelDates[index];
                const dayStart = labelDate ? this._categoryAxisDayStart(labelDate) : null;
                const dayOffset = dayStart ? Math.round((dayStart.getTime() - firstDay.getTime()) / 86400000) : NaN;
                const dayKey = dayStart ? this._categoryAxisDayKey(dayStart) : "";
                const shouldShow = dayStart && dayOffset % 2 === 0 && !shownDayKeys.has(dayKey);
                if (shouldShow) {
                    shownDayKeys.add(dayKey);
                }
                this._setCategoryAxisLabelVisibility(element, shouldShow);
            });
        },

        _applyCategoryAxisLabelStep(labels, step) {
            labels.forEach((element, index) => {
                const shouldShow = index % step === 0;
                this._setCategoryAxisLabelVisibility(element, shouldShow);
            });
        },

        _setCategoryAxisLabelVisibility(element, shouldShow) {
            const visibility = shouldShow ? "" : "hidden";
            if (element.style.visibility !== visibility) {
                element.style.visibility = visibility;
            }
        },

        _categoryAxisDateMap(rows) {
            return rows.reduce((dateByLabel, row) => {
                const date = entryTimestamp(row);
                if (!date) {
                    return dateByLabel;
                }
                [
                    row.chart_label,
                    row.day_label,
                    row.timestamp,
                    row.date
                ].filter(Boolean).forEach((label) => {
                    dateByLabel.set(String(label), date);
                });
                return dateByLabel;
            }, new Map());
        },

        _categoryAxisLabelDate(label, rowDateByLabel) {
            const mappedDate = rowDateByLabel.get(label);
            if (mappedDate) {
                return mappedDate;
            }

            const fullDate = label.match(/^(\d{4})-(\d{2})-(\d{2})(?:\s+\d{2}:\d{2})?$/);
            if (fullDate) {
                return new Date(Number(fullDate[1]), Number(fullDate[2]) - 1, Number(fullDate[3]));
            }

            const shortDate = label.match(/^(\d{2})-(\d{2})(?:\s+\d{2}:\d{2})?$/);
            if (shortDate) {
                return new Date(new Date().getFullYear(), Number(shortDate[1]) - 1, Number(shortDate[2]));
            }
            return null;
        },

        _categoryAxisDayStart(date) {
            return new Date(date.getFullYear(), date.getMonth(), date.getDate());
        },

        _categoryAxisDayKey(date) {
            return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
        },

        _isCategoryAxisLabel(element, rowLabels) {
            const label = String(element && element.textContent || "").trim();
            if (!label) {
                return false;
            }
            return rowLabels.has(label)
                || /^\d{4}-\d{2}-\d{2}$/.test(label)
                || /^\d{2}-\d{2}\s+\d{2}:\d{2}$/.test(label)
                || /^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}$/.test(label);
        },

        _showsBoundaryBadge(chartId) {
            return String(chartId || "").includes("MoistureChart");
        },

        _chartOverlayPx(value) {
            const numberValue = Number(value);
            return `${Number.isFinite(numberValue) ? Number(numberValue.toFixed(2)) : 0}px`;
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
            const thresholdPct = this._moistureThresholdPct(chartId);
            const baseGeometry = {
                left,
                right,
                top,
                bottom,
                nowX: null,
                thresholdPct,
                thresholdY: this._thresholdOverlayY(chartDom, chartId, { left, right, top, bottom }, thresholdPct)
            };
            const now = new Date();
            const axisGeometry = this._axisOverlayGeometry(chartDom, rows, { left, right, top, bottom }, now);
            if (axisGeometry) {
                if (axisGeometry.visible) {
                    baseGeometry.nowX = axisGeometry.geometry.nowX;
                }
                return this._hasOverlayContent(baseGeometry) ? baseGeometry : null;
            }

            baseGeometry.nowX = this._fallbackNowOverlayX(rows, { left, right }, now);
            return this._hasOverlayContent(baseGeometry) ? baseGeometry : null;
        },

        _hasOverlayContent(geometry) {
            return Number.isFinite(geometry && geometry.nowX)
                || Number.isFinite(geometry && geometry.thresholdY);
        },

        _fallbackNowOverlayX(rows, plotBounds, now) {
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
            return plotBounds.left + (plotBounds.right - plotBounds.left) * ratio;
        },

        _thresholdOverlayY(chartDom, chartId, plotBounds, thresholdPct) {
            if (!this._isMoistureChart(chartId) || !Number.isFinite(thresholdPct)) {
                return null;
            }

            const domain = this._moistureAxisDomain(chartId, thresholdPct);
            if (domain && domain.max > domain.min) {
                const clampedThreshold = Math.max(domain.min, Math.min(domain.max, thresholdPct));
                const ratio = (clampedThreshold - domain.min) / (domain.max - domain.min);
                return plotBounds.bottom - ratio * (plotBounds.bottom - plotBounds.top);
            }

            const points = this._valueAxisLabelPoints(chartDom, plotBounds);
            if (points.length >= 2) {
                const byValue = points.sort((a, b) => a.value - b.value);
                const min = byValue[0];
                const max = byValue[byValue.length - 1];
                if (max.value > min.value && thresholdPct >= min.value && thresholdPct <= max.value) {
                    const ratio = (thresholdPct - min.value) / (max.value - min.value);
                    return min.y + ratio * (max.y - min.y);
                }
            }

            return plotBounds.bottom - ((thresholdPct / 100) * (plotBounds.bottom - plotBounds.top));
        },

        _moistureAxisDomain(chartId, thresholdPct) {
            if (!this._isMoistureChart(chartId)) {
                return null;
            }

            const model = this.getView().getModel();
            const rows = model ? model.getProperty(this._chartDataPath(chartId)) : [];
            const keys = this._moistureAxisValueKeys(chartId);
            const values = [];
            (Array.isArray(rows) ? rows : []).forEach((row) => {
                keys.forEach((key) => {
                    const value = Number(row && row[key]);
                    if (Number.isFinite(value)) {
                        values.push(value);
                    }
                });
            });
            if (Number.isFinite(thresholdPct)) {
                values.push(Number(thresholdPct));
            }
            if (!values.length) {
                return { min: 0, max: 50 };
            }

            const minValue = Math.min(...values);
            const maxValue = Math.max(...values);
            const min = Math.min(0, Math.floor(minValue / 10) * 10);
            const max = Math.max(50, Math.ceil(maxValue / 10) * 10);
            return {
                min,
                max: max > min ? max : min + 10
            };
        },

        _moistureAxisValueKeys(chartId) {
            if (chartId === "samplingMoistureChart") {
                return ["baseline_moisture", "sparse_moisture"];
            }
            if (chartId === "anfisMoistureChart") {
                return ["baseline_moisture", "anfis_moisture"];
            }
            if (chartId === "fuzzyMoistureChart") {
                return ["baseline_moisture", "fuzzy_moisture"];
            }
            return ["baseline_moisture"];
        },

        _moistureThresholdPct(chartId) {
            if (!this._isMoistureChart(chartId)) {
                return null;
            }

            const model = this.getView().getModel();
            const pots = model ? model.getProperty(this._chartPotsPath(chartId)) : [];
            const targets = (Array.isArray(pots) ? pots : [])
                .map((pot) => Number(pot && pot.moisture_target_pct))
                .filter(Number.isFinite);
            if (!targets.length) {
                return MOISTURE_THRESHOLD_PCT;
            }

            const total = targets.reduce((sum, value) => sum + value, 0);
            return Number((total / targets.length).toFixed(2));
        },

        _chartPotsPath(chartId) {
            if (chartId && chartId.startsWith("anfis")) {
                return "/anfisPots";
            }
            if (chartId && chartId.startsWith("fuzzy")) {
                return "/fuzzyPots";
            }
            return "/samplingPots";
        },

        _formatThresholdPct(value) {
            const numberValue = Number(value);
            if (!Number.isFinite(numberValue)) {
                return "N/A";
            }
            return Number.isInteger(numberValue) ? String(numberValue) : numberValue.toFixed(1);
        },

        _valueAxisLabelPoints(chartDom, plotBounds) {
            const chartRect = chartDom.getBoundingClientRect();
            const valuesByLabel = new Map();
            chartDom.querySelectorAll("svg text").forEach((node) => {
                const text = (node.textContent || "").trim().replace(",", ".");
                if (!/^-?\d+(\.\d+)?%?$/.test(text) || !node.getBoundingClientRect) {
                    return;
                }
                const rect = node.getBoundingClientRect();
                if (!rect.width || !rect.height) {
                    return;
                }
                const centerX = rect.left - chartRect.left + rect.width / 2;
                const centerY = rect.top - chartRect.top + rect.height / 2;
                if (
                    centerX > plotBounds.left - 2
                    || centerY < plotBounds.top - 8
                    || centerY > plotBounds.bottom + 8
                ) {
                    return;
                }
                const value = Number(text.replace("%", ""));
                if (!Number.isFinite(value)) {
                    return;
                }
                const existing = valuesByLabel.get(value) || [];
                existing.push(centerY);
                valuesByLabel.set(value, existing);
            });

            return Array.from(valuesByLabel.entries()).map(([value, yValues]) => ({
                value,
                y: yValues.reduce((sum, item) => sum + item, 0) / yValues.length
            }));
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
            rows.forEach((row, rowIndex) => {
                const label = this._chartRowLabel(row);
                const timestamp = entryTimestamp(row);
                if (label && timestamp) {
                    labelRows.set(label, { row, rowIndex });
                }
            });
            if (!labelRows.size) {
                return [];
            }

            const chartRect = chartDom.getBoundingClientRect();
            const pointsByTime = new Map();
            chartDom.querySelectorAll("svg text").forEach((node) => {
                const label = (node.textContent || "").trim();
                const labelEntry = labelRows.get(label);
                if (!labelEntry || !node.getBoundingClientRect) {
                    return;
                }
                const { row, rowIndex } = labelEntry;
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
                const existing = pointsByTime.get(time) || { date: timestamp, rowIndex, xValues: [] };
                existing.xValues.push(centerX);
                pointsByTime.set(time, existing);
            });

            return Array.from(pointsByTime.values())
                .map((point) => ({
                    date: point.date,
                    rowIndex: point.rowIndex,
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

        _syncCustomChartLegend(chartDom, chartId) {
            const parent = chartDom && chartDom.parentElement;
            if (!parent) {
                return;
            }

            parent.querySelectorAll(`.dtChartLegend[data-chart-id="${chartId}"]`).forEach((node) => node.remove());

            const measures = visibleChartMeasures(chartId, this._chartVisibility());
            if (!measures.length) {
                return;
            }

            const measureLegendHtml = measures.map((measure) => {
                const color = chartMeasureColor(chartId, measure) || "#5d7187";
                const shape = this._chartLegendShape(chartId, measure);
                return this._chartLegendItemHtml(this._chartLegendLabel(measure, chartId), shape, color);
            }).join("");
            chartDom.insertAdjacentHTML(
                "afterend",
                `<div class="dtChartLegend" data-chart-id="${chartId}">${measureLegendHtml}</div>`
            );
        },

        _chartLegendItemHtml(label, shape, color) {
            return (
                "<span class=\"dtChartLegendItem\">" +
                `<span class="dtChartLegendMarker dtChartLegendMarker-${shape}" style="--dt-chart-series-color:${color};"></span>` +
                `<span>${this._escapeHtml(label)}</span>` +
                "</span>"
            );
        },

        _chartLegendShape(chartId, measure) {
            const measures = CHART_MEASURES[chartId] || [];
            const index = measures.indexOf(measure);
            const axisShapes = visibleChartDataShapesByAxis(chartId, {});
            const allShapes = [
                ...axisShapes.primaryAxis,
                ...axisShapes.secondaryAxis
            ];
            return allShapes[index] === "bar" ? "bar" : "line";
        },

        _escapeHtml(value) {
            return String(value || "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;");
        }
    };
});
