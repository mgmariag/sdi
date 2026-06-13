sap.ui.define([
    "disertatie/model/chart/chartConfig",
    "disertatie/model/chart/chartData"
], (ChartConfig, ChartData) => {
    "use strict";

    const { INITIAL_VISIBLE_CHART_DAYS } = ChartConfig;
    const { entryTimestamp } = ChartData;

    const CHART_WINDOW_SYNC_GROUPS = [
        ["samplingMoistureChart", "samplingContextChart"],
        ["anfisMoistureChart", "anfisContextChart"],
        ["fuzzyMoistureChart", "fuzzyContextChart"]
    ];

    function fullWindow() {
        return {
            start: "firstDataPoint",
            end: "lastDataPoint"
        };
    }

    function initialChartWindow(rows, rowLabel) {
        const normalizedRows = Array.isArray(rows) ? rows : [];
        if (normalizedRows.length === 0) {
            return fullWindow();
        }

        const endRow = normalizedRows[normalizedRows.length - 1];
        const endDate = entryTimestamp(endRow);
        if (!endDate) {
            return fullWindow();
        }

        const startDate = new Date(endDate.getTime() - INITIAL_VISIBLE_CHART_DAYS * 24 * 60 * 60 * 1000);
        const startRow = normalizedRows.find((row) => {
            const rowDate = entryTimestamp(row);
            return rowDate && rowDate >= startDate;
        }) || normalizedRows[0];

        if (startRow === normalizedRows[0] && endRow === normalizedRows[normalizedRows.length - 1]) {
            return fullWindow();
        }

        return {
            start: {
                categoryAxis: {
                    "Date/Time": rowLabel(startRow)
                }
            },
            end: {
                categoryAxis: {
                    "Date/Time": rowLabel(endRow)
                }
            }
        };
    }

    function chartWindowSignature(rows, rowLabel) {
        const normalizedRows = Array.isArray(rows) ? rows : [];
        if (normalizedRows.length === 0) {
            return "";
        }
        const endRow = normalizedRows[normalizedRows.length - 1];
        const endDate = entryTimestamp(endRow);
        const startDate = endDate ? new Date(endDate.getTime() - INITIAL_VISIBLE_CHART_DAYS * 24 * 60 * 60 * 1000) : null;
        const startIndex = startDate
            ? Math.max(0, normalizedRows.findIndex((row) => {
                const rowDate = entryTimestamp(row);
                return rowDate && rowDate >= startDate;
            }))
            : 0;
        return [
            normalizedRows.length,
            rowLabel(normalizedRows[startIndex]),
            rowLabel(normalizedRows[normalizedRows.length - 1])
        ].join("|");
    }

    return {
        _initialChartWindow(chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            return initialChartWindow(rows, (row) => this._chartRowLabel(row));
        },

        _chartWindowSignature(chartId) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            return chartWindowSignature(rows, (row) => this._chartRowLabel(row));
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
            this._setupChartWindowSyncState();
            const initialRange = this._chartWindowRangeFromConfig(chartId, this._initialChartWindow(chartId));
            if (initialRange) {
                this._syncedChartWindowSignatures[chartId] = initialRange.signature;
            }
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

        _setupChartWindowSyncState() {
            this._chartWindowSyncTimers = this._chartWindowSyncTimers || {};
            this._chartWindowSyncObservers = this._chartWindowSyncObservers || {};
            this._chartWindowSyncListenerCleanups = this._chartWindowSyncListenerCleanups || {};
            this._syncedChartWindowSignatures = this._syncedChartWindowSignatures || {};
            this._chartWindowUserInteractionUntil = this._chartWindowUserInteractionUntil || {};
            this._chartWindowProgrammaticUntil = this._chartWindowProgrammaticUntil || {};
        },

        _chartWindowSyncGroup(chartId) {
            return CHART_WINDOW_SYNC_GROUPS.find((group) => group.includes(chartId)) || [];
        },

        _chartWindowSyncPeers(chartId) {
            return this._chartWindowSyncGroup(chartId).filter((peerId) => peerId !== chartId && this.byId(peerId));
        },

        _trackChartWindowSync(chart, chartId) {
            if (!this._chartWindowSyncPeers(chartId).length) {
                return;
            }
            const chartDom = chart && chart.getDomRef && chart.getDomRef();
            if (!chartDom) {
                return;
            }

            this._setupChartWindowSyncState();
            this._replaceChartWindowSyncListeners(chartDom, chartId);
            this._observeChartWindowSyncSvg(chartDom, chartId);
        },

        _replaceChartWindowSyncListeners(chartDom, chartId) {
            if (this._chartWindowSyncListenerCleanups[chartId]) {
                this._chartWindowSyncListenerCleanups[chartId]();
            }

            const cleanupFns = [];
            const schedule = (options) => {
                if (options && options.userEvent) {
                    this._markChartWindowUserInteraction(chartId);
                }
                this._scheduleChartOverlay(chartId);
                this._scheduleChartWindowSyncFrom(chartId, 60, options);
            };
            const onWheel = (event) => {
                if (this._handleChartWheelScroll(event, chartId, chartDom)) {
                    schedule({ userEvent: true, force: true });
                }
            };
            chartDom.addEventListener("wheel", onWheel, { passive: false, capture: true });
            cleanupFns.push(() => chartDom.removeEventListener("wheel", onWheel, true));

            const onInteraction = () => schedule({ userEvent: true, force: true });
            const eventNames = ["mouseup", "pointerup", "touchend", "keyup", "dblclick"];
            eventNames.forEach((eventName) => {
                chartDom.addEventListener(eventName, onInteraction, { passive: true });
                cleanupFns.push(() => chartDom.removeEventListener(eventName, onInteraction));
            });

            this._chartHorizontalScrollNodes(chartDom).forEach((node, index) => {
                const onScroll = () => {
                    if (this._chartScrollSyncing || this._chartWindowSyncSuppressed(chartId)) {
                        this._scheduleChartOverlay(chartId);
                        return;
                    }
                    this._syncChartScrollPosition(chartId, node, index);
                    schedule({ userEvent: true, force: true });
                };
                node.addEventListener("scroll", onScroll, { passive: true });
                cleanupFns.push(() => node.removeEventListener("scroll", onScroll));
            });

            this._chartWindowSyncListenerCleanups[chartId] = () => cleanupFns.forEach((cleanup) => cleanup());
        },

        _observeChartWindowSyncSvg(chartDom, chartId) {
            const svg = chartDom.querySelector("svg");
            const existing = this._chartWindowSyncObservers[chartId];
            if (!svg || (existing && existing.svg === svg)) {
                return;
            }
            if (existing && existing.observer) {
                existing.observer.disconnect();
            }

            let scheduled = false;
            const observer = new MutationObserver(() => {
                if (scheduled) {
                    return;
                }
                scheduled = true;
                requestAnimationFrame(() => {
                    scheduled = false;
                    this._scheduleChartWindowSyncFrom(chartId, 60);
                });
            });
            observer.observe(svg, {
                attributes: true,
                childList: true,
                subtree: true
            });
            this._chartWindowSyncObservers[chartId] = { observer, svg };
        },

        _scheduleChartWindowSyncFrom(chartId, delay, options) {
            this._setupChartWindowSyncState();
            if (this._isApplyingSyncedChartWindow || this._chartWindowSyncMuted()) {
                return;
            }
            const force = options && options.force;
            if (!force && (this._chartWindowSyncSuppressed(chartId) || !this._hasRecentChartWindowUserInteraction(chartId))) {
                return;
            }

            clearTimeout(this._chartWindowSyncTimers[chartId]);
            this._chartWindowSyncTimers[chartId] = setTimeout(() => {
                this._syncChartWindowFrom(chartId);
            }, delay || 0);
        },

        _markChartWindowUserInteraction(chartId) {
            this._setupChartWindowSyncState();
            this._chartWindowUserInteractionUntil[chartId] = Date.now() + 900;
        },

        _hasRecentChartWindowUserInteraction(chartId) {
            return this._chartWindowUserInteractionUntil
                && this._chartWindowUserInteractionUntil[chartId]
                && Date.now() < this._chartWindowUserInteractionUntil[chartId];
        },

        _suppressProgrammaticChartWindowSync(chartId) {
            this._setupChartWindowSyncState();
            this._chartWindowProgrammaticUntil[chartId] = Date.now() + 900;
        },

        _chartWindowSyncSuppressed(chartId) {
            return this._chartWindowProgrammaticUntil
                && this._chartWindowProgrammaticUntil[chartId]
                && Date.now() < this._chartWindowProgrammaticUntil[chartId];
        },

        _chartWindowSyncMuted() {
            return this._chartWindowSyncMuteUntil && Date.now() < this._chartWindowSyncMuteUntil;
        },

        _syncChartWindowFrom(chartId) {
            if (this._isApplyingSyncedChartWindow || !this._chartWindowSyncPeers(chartId).length) {
                return;
            }

            const range = this._visibleChartWindowRange(chartId);
            if (!range || !range.signature || this._syncedChartWindowSignatures[chartId] === range.signature) {
                return;
            }

            this._syncedChartWindowSignatures[chartId] = range.signature;
            this._isApplyingSyncedChartWindow = true;
            this._chartWindowSyncMuteUntil = Date.now() + 180;
            try {
                this._chartWindowSyncPeers(chartId).forEach((peerId) => {
                    this._applySyncedChartWindow(peerId, range);
                });
            } finally {
                this._isApplyingSyncedChartWindow = false;
            }
        },

        _applySyncedChartWindow(chartId, sourceRange) {
            const chart = this.byId(chartId);
            const range = this._chartWindowRangeForChart(chartId, sourceRange);
            if (!chart || !range || !range.window) {
                return;
            }

            this._suppressProgrammaticChartWindowSync(chartId);
            chart.setVizProperties({
                plotArea: {
                    window: range.window
                }
            });
            this._syncedChartWindowSignatures[chartId] = range.signature;
            this._scheduleChartOverlay(chartId);
        },

        _visibleChartWindowRange(chartId) {
            const domRange = this._visibleChartWindowRangeFromDom(chartId);
            if (domRange) {
                return domRange;
            }

            const chart = this.byId(chartId);
            const properties = chart && chart.getVizProperties && chart.getVizProperties();
            const windowConfig = properties && properties.plotArea && properties.plotArea.window;
            return this._chartWindowRangeFromConfig(chartId, windowConfig);
        },

        _visibleChartWindowRangeFromDom(chartId) {
            const chart = this.byId(chartId);
            const chartDom = chart && chart.getDomRef && chart.getDomRef();
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            if (!chartDom || !Array.isArray(rows) || rows.length < 2) {
                return null;
            }

            const plotBounds = this._renderedPlotBounds(chartDom);
            if (!plotBounds) {
                return null;
            }

            const points = this._visibleAxisLabelPoints(chartDom, rows, plotBounds)
                .filter((point) => Number.isFinite(point.rowIndex) && Number.isFinite(point.x))
                .sort((a, b) => a.x - b.x);
            if (points.length < 2) {
                return null;
            }

            const first = points[0];
            const last = points[points.length - 1];
            if (last.x <= first.x || last.rowIndex <= first.rowIndex) {
                return null;
            }

            const indexPerPixel = (last.rowIndex - first.rowIndex) / (last.x - first.x);
            const startIndex = this._clampChartRowIndex(
                Math.round(first.rowIndex - ((first.x - plotBounds.left) * indexPerPixel)),
                rows
            );
            const endIndex = this._clampChartRowIndex(
                Math.round(last.rowIndex + ((plotBounds.right - last.x) * indexPerPixel)),
                rows
            );
            if (endIndex <= startIndex) {
                return null;
            }

            return this._chartWindowRangeFromIndexes(chartId, startIndex, endIndex);
        },

        _chartWindowRangeFromConfig(chartId, windowConfig) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            if (!windowConfig || !Array.isArray(rows) || rows.length < 2) {
                return null;
            }

            const startLabel = this._chartWindowBoundaryLabel(windowConfig.start, rows[0]);
            const endLabel = this._chartWindowBoundaryLabel(windowConfig.end, rows[rows.length - 1]);
            const startIndex = this._chartRowIndexByLabel(rows, startLabel);
            const endIndex = this._chartRowIndexByLabel(rows, endLabel);
            if (startIndex < 0 || endIndex <= startIndex) {
                return null;
            }

            return this._chartWindowRangeFromIndexes(chartId, startIndex, endIndex);
        },

        _chartWindowBoundaryLabel(boundary, fallbackRow) {
            if (boundary === "firstDataPoint" || boundary === "lastDataPoint") {
                return this._chartRowLabel(fallbackRow);
            }
            return boundary
                && boundary.categoryAxis
                && boundary.categoryAxis["Date/Time"];
        },

        _chartWindowRangeForChart(chartId, sourceRange) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            if (!Array.isArray(rows) || rows.length < 2) {
                return null;
            }

            let startIndex = this._clampChartRowIndex(sourceRange.startIndex, rows);
            let endIndex = this._clampChartRowIndex(sourceRange.endIndex, rows);
            const labelStartIndex = this._chartRowIndexByLabel(rows, sourceRange.startLabel);
            const labelEndIndex = this._chartRowIndexByLabel(rows, sourceRange.endLabel);
            if (labelStartIndex >= 0) {
                startIndex = labelStartIndex;
            }
            if (labelEndIndex >= 0) {
                endIndex = labelEndIndex;
            }
            if (endIndex <= startIndex) {
                return null;
            }

            return this._chartWindowRangeFromIndexes(chartId, startIndex, endIndex);
        },

        _chartWindowRangeFromIndexes(chartId, startIndex, endIndex) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            const startRow = rows[startIndex];
            const endRow = rows[endIndex];
            const startLabel = this._chartRowLabel(startRow);
            const endLabel = this._chartRowLabel(endRow);
            if (!startLabel || !endLabel) {
                return null;
            }

            const windowConfig = startIndex === 0 && endIndex === rows.length - 1
                ? {
                    start: "firstDataPoint",
                    end: "lastDataPoint"
                }
                : {
                    start: {
                        categoryAxis: {
                            "Date/Time": startLabel
                        }
                    },
                    end: {
                        categoryAxis: {
                            "Date/Time": endLabel
                        }
                    }
                };

            return {
                startIndex,
                endIndex,
                startLabel,
                endLabel,
                window: windowConfig,
                signature: [
                    this._chartWindowSyncGroup(chartId).join(","),
                    rows.length,
                    startIndex,
                    endIndex,
                    startLabel,
                    endLabel
                ].join("|")
            };
        },

        _chartRowIndexByLabel(rows, label) {
            return rows.findIndex((row) => this._chartRowLabel(row) === label);
        },

        _clampChartRowIndex(index, rows) {
            const numericIndex = Number(index);
            if (!Number.isFinite(numericIndex)) {
                return 0;
            }
            return Math.max(0, Math.min(rows.length - 1, Math.round(numericIndex)));
        },

        _chartHorizontalScrollNodes(chartDom) {
            const nodes = [chartDom].concat(Array.from(chartDom.querySelectorAll("*")));
            return nodes.filter((node) => {
                if (!node || node.nodeType !== 1 || node.scrollWidth <= node.clientWidth + 1) {
                    return false;
                }
                const style = getComputedStyle(node);
                return style.overflowX !== "hidden" && style.overflowX !== "clip";
            }).slice(0, 6);
        },

        _handleChartWheelScroll(event, chartId, chartDom) {
            const target = this._chartWheelScrollTarget(chartDom);
            const delta = this._normalizedChartWheelDelta(event, target ? target.node : chartDom);
            if (!delta) {
                return false;
            }

            this._consumeChartWheelEvent(event);
            if (!target) {
                return true;
            }

            const nextScrollLeft = Math.max(0, Math.min(target.max, target.node.scrollLeft + delta));
            if (Math.abs(nextScrollLeft - target.node.scrollLeft) >= 0.5) {
                target.node.scrollLeft = nextScrollLeft;
                this._syncChartScrollPosition(chartId, target.node, target.index);
            }
            return true;
        },

        _consumeChartWheelEvent(event) {
            if (event && event.preventDefault) {
                event.preventDefault();
            }
            if (event && event.stopPropagation) {
                event.stopPropagation();
            }
            if (event && event.stopImmediatePropagation) {
                event.stopImmediatePropagation();
            }
        },

        _chartWheelScrollTarget(chartDom) {
            const nodes = this._chartHorizontalScrollNodes(chartDom).map((node, index) => ({
                node,
                index,
                max: node.scrollWidth - node.clientWidth
            })).filter((target) => target.max > 0);
            if (!nodes.length) {
                return null;
            }

            return nodes.find((target) => {
                const style = getComputedStyle(target.node);
                return target.node.scrollLeft > 0 || style.overflowX === "auto" || style.overflowX === "scroll";
            }) || nodes[0];
        },

        _normalizedChartWheelDelta(event, node) {
            if (!event) {
                return 0;
            }

            const horizontalDelta = Number(event.deltaX) || 0;
            const verticalDelta = Number(event.deltaY) || 0;
            let delta = Math.abs(horizontalDelta) > Math.abs(verticalDelta) ? horizontalDelta : verticalDelta;
            if (!delta) {
                return 0;
            }
            if (event.deltaMode === 1) {
                delta *= 32;
            } else if (event.deltaMode === 2) {
                delta *= node.clientWidth || 1;
            }
            return delta;
        },

        _syncChartScrollPosition(chartId, sourceNode, sourceIndex) {
            if (this._chartScrollSyncing) {
                return;
            }
            const sourceMax = sourceNode.scrollWidth - sourceNode.clientWidth;
            if (sourceMax <= 0) {
                return;
            }

            const ratio = sourceNode.scrollLeft / sourceMax;
            this._chartScrollSyncing = true;
            this._chartWindowSyncPeers(chartId).forEach((peerId) => {
                const peerChart = this.byId(peerId);
                const peerDom = peerChart && peerChart.getDomRef && peerChart.getDomRef();
                if (!peerDom) {
                    return;
                }
                const peerNodes = this._chartHorizontalScrollNodes(peerDom);
                const peerNode = peerNodes[sourceIndex] || peerNodes[0];
                const peerMax = peerNode && (peerNode.scrollWidth - peerNode.clientWidth);
                if (peerMax > 0) {
                    this._suppressProgrammaticChartWindowSync(peerId);
                    peerNode.scrollLeft = ratio * peerMax;
                    this._scheduleChartOverlay(peerId);
                }
            });
            setTimeout(() => {
                this._chartScrollSyncing = false;
            }, 0);
        }
    };
});
