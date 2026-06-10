sap.ui.define([
    "disertatie/model/chartBuilder",
    "sap/viz/ui5/format/ChartFormatter",
    "sap/viz/ui5/api/env/Format",
    "sap/viz/ui5/controls/Popover",
    "sap/viz/ui5/controls/common/feeds/FeedItem",
    "sap/ui/core/HTML"
], (ChartBuilder, ChartFormatter, Format, Popover, FeedItem, HTML) => {
    "use strict";

    const {
        CHART_DATA_PATHS,
        CHART_FORMATS,
        CHART_MEASURES,
        CHART_SOURCE_DATA_PATHS,
        EXPERIMENT_CHART_IDS,
        INITIAL_VISIBLE_CHART_DAYS,
        MOISTURE_THRESHOLD_COLOR,
        MOISTURE_THRESHOLD_PCT,
        entryTimestamp,
        visibleChartDataShapesByAxis,
        visibleChartMeasures,
        visibleChartMeasuresByAxis,
        visibleChartPalette,
        withChartVisibility,
        chartMeasureColor
    } = ChartBuilder;

    const ANFIS_SCORE_MEASURE = "ANFIS zone signal (%)";
    const FUZZY_SCORE_MEASURE = "Fuzzy irrigation score (%)";
    const LEGEND_LABELS = {
        "Baseline Moisture": "Baseline",
        "Sparse Moisture": "Sparse sensing",
        "ANFIS Moisture": "ANFIS estimate",
        "Fuzzy Moisture": "Fuzzy estimate",
        [ANFIS_SCORE_MEASURE]: "ANFIS signal",
        "Baseline Irrigation (L)": "Baseline",
        "Baseline Water Usage (L)": "Baseline water",
        "Sparse-Sensing Irrigation (L)": "Sparse sensing",
        "Sparse Water Usage (L)": "Sparse water",
        "ANFIS Water Usage (L)": "ANFIS",
        "Fuzzy Water Usage (L)": "Fuzzy",
        [FUZZY_SCORE_MEASURE]: "Fuzzy score",
        "Rain (mm)": "Rain (mm)",
        "Max Temperature (°C)": "Max Temperature (°C)",
        "Max Temp (°C)": "Max Temperature (°C)"
    };
    const CHART_LEGEND_LABELS = {
        samplingBaselineWeatherChart: {
            "Baseline Moisture": "Moisture",
            "Baseline Water Usage (L)": "Water Usage"
        }
    };

    return {
        onAfterRendering() {
            this._styleCharts();
        },

        _styleCharts() {
            EXPERIMENT_CHART_IDS.forEach((chartId) => {
                const chart = this.byId(chartId);
                if (chart) {
                    this._styleChart(chart, chartId);
                }
            });
        },

        _styleChart(chart, chartId) {
            const formatString = CHART_FORMATS[chartId] || {};
            const visibility = this._chartVisibility();
            const dataShapes = visibleChartDataShapesByAxis(chartId, visibility);

            this._applyChartFeedVisibility(chart, chartId, visibility);

            chart.setVizProperties({
                plotArea: {
                    colorPalette: visibleChartPalette(chartId, visibility),
                    dataShape: {
                        primaryAxis: dataShapes.primaryAxis,
                        secondaryAxis: dataShapes.secondaryAxis
                    },
                    dataPointStyleMode: "update",
                    dataPointStyle: {
                        rules: this._chartSeriesStyleRules(chartId)
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
                    visible: false,
                    position: "bottom",
                    label: {
                        style: { color: "#17324d" }
                    }
                },
                legendGroup: {
                    layout: {
                        position: "bottom"
                    }
                },
                title: {
                    visible: false
                },
                valueAxis: {
                    label: { style: { color: "#5d7187" } },
                    title: { visible: false }
                },
                valueAxis2: {
                    label: { style: { color: "#5d7187" } },
                    title: this._secondaryAxisTitle(chartId),
                    scale: this._secondaryAxisScale(chartId)
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

        _secondaryAxisTitle(chartId) {
            const titles = {
                samplingBaselineWeatherChart: "Water Usage (L)",
                anfisMoistureChart: "ANFIS Signal (%)",
                anfisContextChart: "Weather (mm / °C)",
                fuzzyMoistureChart: "Fuzzy Score (%)",
                fuzzyContextChart: "Weather (mm / °C)"
            };
            return titles[chartId] ? this._axisTitle(titles[chartId]) : { visible: false };
        },

        _axisTitle(text) {
            return {
                visible: true,
                text,
                style: { color: "#5d7187" }
            };
        },

        _secondaryAxisScale(chartId) {
            if (chartId === "anfisMoistureChart" || chartId === "fuzzyMoistureChart") {
                return { fixedRange: true, minValue: 0, maxValue: 100 };
            }
            if (chartId === "samplingBaselineWeatherChart") {
                return { fixedRange: true, minValue: 0, maxValue: 250 };
            }
            return { fixedRange: true, minValue: 0, maxValue: 60 };
        },

        _applyChartFeedVisibility(chart, chartId, visibility) {
            if (!chart || !chart.getFeeds || !chart.addFeed) {
                return;
            }
            const feeds = chart && chart.getFeeds && chart.getFeeds();
            if (Array.isArray(feeds) && chart.removeFeed) {
                feeds.filter((feed) => {
                    const uid = feed && feed.getUid && feed.getUid();
                    return uid === "valueAxis" || uid === "valueAxis2";
                }).forEach((feed) => {
                    chart.removeFeed(feed);
                    feed.destroy();
                });
            }

            const axes = visibleChartMeasuresByAxis(chartId, visibility);
            if (axes.primaryAxis.length) {
                chart.addFeed(new FeedItem({
                    uid: "valueAxis",
                    type: "Measure",
                    values: axes.primaryAxis
                }));
            }
            if (axes.secondaryAxis.length) {
                chart.addFeed(new FeedItem({
                    uid: "valueAxis2",
                    type: "Measure",
                    values: axes.secondaryAxis
                }));
            }
        },

        _chartVisibility() {
            const model = this.getView().getModel();
            return model ? model.getProperty("/chartVisibility") : {};
        },

        _applyVisibleChartData(chartId) {
            const model = this.getView().getModel();
            if (!model) {
                return [];
            }

            const storedRows = model.getProperty(this._chartSourceDataPath(chartId));
            const currentRows = model.getProperty(this._chartDataPath(chartId)) || [];
            const sourceRows = Array.isArray(storedRows) && storedRows.length ? storedRows : currentRows;
            const visibleRows = withChartVisibility(sourceRows, this._chartVisibility());
            model.setProperty(this._chartDataPath(chartId), visibleRows);
            return visibleRows;
        },

        _applyExperimentChartData(experiment) {
            this._experimentChartIds(experiment).forEach((chartId) => this._applyVisibleChartData(chartId));
        },

        _refreshExperimentCharts(experiment) {
            this._experimentChartIds(experiment).forEach((chartId) => this._refreshChart(chartId));
        },

        _experimentChartIds(experiment) {
            const prefix = String(experiment || "");
            return EXPERIMENT_CHART_IDS.filter((chartId) => chartId.startsWith(prefix));
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

        _chartSeriesStyleRules(chartId) {
            const measures = CHART_MEASURES[chartId] || [];
            return measures.map((measure) => {
                const color = chartMeasureColor(chartId, measure);
                return {
                    callback: (context) => this._chartMeasureName(chartId, context) === measure,
                    properties: this._chartSeriesColorProperties(color),
                    displayName: this._chartLegendLabel(measure, chartId)
                };
            });
        },

        _chartSeriesColorProperties(color) {
            return color ? { color, lineColor: color } : {};
        },

        _chartLegendLabel(measure, chartId) {
            const chartLabels = CHART_LEGEND_LABELS[chartId] || {};
            if (chartLabels[measure]) {
                return chartLabels[measure];
            }
            return LEGEND_LABELS[measure] || measure;
        },

        _isMoistureChart(chartId) {
            return chartId === "samplingMoistureChart"
                || chartId === "samplingBaselineWeatherChart"
                || chartId === "anfisMoistureChart"
                || chartId === "fuzzyMoistureChart";
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
                    customDataControl: (data) => this._chartDetailPopover(data, chartId)
                });
                this.getView().addDependent(this._chartPopovers[chartId]);
            }

            this._chartPopovers[chartId].connect(vizUid);
        },

        _chartDetailPopover(data, chartId) {
            const measureName = this._extractPopoverMeasureName(data, chartId);
            const row = this._findPopoverChartRow(data, chartId, measureName);
            const lines = row
                ? (measureName ? this._chartDetailLines(row, chartId, measureName) : this._chartPointSummaryLines(row, chartId))
                : [];

            if (!lines.length) {
                return new HTML({ content: "" });
            }

            const content = lines.map(([label, value]) => (
                "<div style=\"margin:4px 18px 8px 18px;white-space:nowrap;\">" +
                `<span style="color:#5d7187;">${label}</span>` +
                `<span style="float:right;margin-left:24px;font-weight:600;color:#17324d;">${value}</span>` +
                "</div>"
            )).join("");
            return new HTML({ content });
        },

        _chartDetailLines(row, chartId, measureName) {
            if (["Max Temp (°C)", "Max Temperature (°C)", "Max Temp (C)", "Max Temperature (C)"].includes(measureName)) {
                return this._weatherTemperatureLines(row, chartId);
            }
            if (measureName === "Rain (L/m²)" || measureName === "Rain (mm)") {
                return [
                    ["Rain", `${this._formatPopoverNumber(row.rain_amount)} mm`],
                    ["Cloud cover", `${this._formatPopoverNumber(this._popoverCloudCover(row, chartId))}%`]
                ];
            }

            return this._experimentMetricLines(row, measureName);
        },

        _chartPointSummaryLines(row, chartId) {
            return (CHART_MEASURES[chartId] || [])
                .map((measureName) => this._chartMeasureSummaryLine(row, measureName))
                .filter(Boolean);
        },

        _chartMeasureSummaryLine(row, measureName) {
            const label = {
                "Baseline Moisture": "Baseline moisture",
                "Sparse Moisture": "Sparse moisture",
                "ANFIS Moisture": "ANFIS moisture",
                "Fuzzy Moisture": "Fuzzy moisture",
                [ANFIS_SCORE_MEASURE]: "ANFIS zone signal",
                "Baseline Irrigation (L)": "Baseline water",
                "Baseline Water Usage (L)": "Baseline water",
                "Sparse-Sensing Irrigation (L)": "Sparse water",
                "Sparse Water Usage (L)": "Sparse water",
                "ANFIS Water Usage (L)": "ANFIS water",
                "Fuzzy Water Usage (L)": "Fuzzy water",
                [FUZZY_SCORE_MEASURE]: "Fuzzy score",
                "Max Temperature (°C)": "Temperature",
                "Max Temp (°C)": "Temperature",
                "Max Temperature (C)": "Temperature",
                "Max Temp (C)": "Temperature",
                "Rain (mm)": "Rain",
                "Rain (L/m²)": "Rain"
            }[measureName];
            const value = this._chartMeasureSummaryValue(row, measureName);
            return label && value !== null ? [label, value] : null;
        },

        _chartMeasureSummaryValue(row, measureName) {
            const temperatureValue = `${this._formatPopoverNumber(row.temperature ?? row.max_temperature)} °C`;
            const values = {
                "Baseline Moisture": this._formatPopoverPercent(row.baseline_moisture),
                "Sparse Moisture": this._formatPopoverPercent(row.sparse_moisture),
                "ANFIS Moisture": this._formatPopoverPercent(row.anfis_moisture),
                "Fuzzy Moisture": this._formatPopoverPercent(row.fuzzy_moisture),
                [ANFIS_SCORE_MEASURE]: this._formatPopoverPercent(row.predicted_probability_percent),
                "Baseline Irrigation (L)": `${this._formatPopoverNumber(row.baseline_water_usage_l)} L`,
                "Baseline Water Usage (L)": `${this._formatPopoverNumber(row.baseline_water_usage_l)} L`,
                "Sparse-Sensing Irrigation (L)": `${this._formatPopoverNumber(row.sparse_water_usage_l)} L`,
                "Sparse Water Usage (L)": `${this._formatPopoverNumber(row.sparse_water_usage_l)} L`,
                "ANFIS Water Usage (L)": `${this._formatPopoverNumber(row.anfis_water_usage_l)} L`,
                "Fuzzy Water Usage (L)": `${this._formatPopoverNumber(row.fuzzy_water_usage_l)} L`,
                [FUZZY_SCORE_MEASURE]: this._formatPopoverPercent(row.fuzzy_prescription_score_pct),
                "Max Temperature (°C)": temperatureValue,
                "Max Temp (°C)": temperatureValue,
                "Max Temperature (C)": temperatureValue,
                "Max Temp (C)": temperatureValue,
                "Rain (mm)": `${this._formatPopoverNumber(row.rain_amount)} mm`,
                "Rain (L/m²)": `${this._formatPopoverNumber(row.rain_amount)} mm`
            };
            const value = values[measureName];
            return value && !value.includes("N/A") ? value : null;
        },

        _weatherTemperatureLines(row, chartId) {
            const lines = [];
            if (this._isHourlyChartRow(row)) {
                lines.push(["Temperature", `${this._formatPopoverNumber(row.temperature ?? row.max_temperature)} °C`]);
            } else {
                lines.push(["Max temperature", `${this._formatPopoverNumber(row.max_temperature)} °C`]);
                lines.push(["Min temperature", `${this._formatPopoverNumber(this._popoverMinTemperature(row, chartId))} °C`]);
            }
            lines.push(["Humidity", `${this._formatPopoverNumber(row.humidity)}%`]);
            lines.push(["Cloud cover", `${this._formatPopoverNumber(this._popoverCloudCover(row, chartId))}%`]);
            return lines;
        },

        _experimentMetricLines(row, measureName) {
            const details = {
                "Baseline Moisture": [
                    ["End-of-day moisture", this._formatPopoverPercent(row.baseline_moisture)],
                    ["Before irrigation", this._formatPopoverPercent(row.baseline_pre_irrigation_moisture)],
                    ["After irrigation", this._formatPopoverPercent(row.baseline_post_irrigation_moisture)]
                ],
                "Sparse Moisture": [
                    ["Sparse moisture", this._formatPopoverPercent(row.sparse_moisture)],
                    ["Sparse valves", this._formatPopoverValves(row, "sparse")],
                    ["Sparse water", `${this._formatPopoverNumber(row.sparse_water_usage_l)} L`]
                ],
                "ANFIS Moisture": [
                    ["ANFIS moisture", this._formatPopoverPercent(row.anfis_moisture)],
                    ["ANFIS valves", this._formatPopoverValves(row, "anfis")],
                    ["ANFIS water", `${this._formatPopoverNumber(row.anfis_water_usage_l)} L`]
                ],
                "Fuzzy Moisture": [
                    ["Fuzzy moisture", this._formatPopoverPercent(row.fuzzy_moisture)],
                    ["Fuzzy valves", this._formatPopoverValves(row, "fuzzy")],
                    ["Fuzzy water", `${this._formatPopoverNumber(row.fuzzy_water_usage_l)} L`]
                ],
                "Baseline Water Usage (L)": [
                    ["Baseline water", `${this._formatPopoverNumber(row.baseline_water_usage_l)} L`],
                    ["Baseline windows", this._formatPopoverInteger(row.baseline_irrigation_events)],
                    ["Baseline valves", this._formatPopoverValves(row, "baseline")]
                ],
                "Baseline Irrigation (L)": [
                    ["Baseline water", `${this._formatPopoverNumber(row.baseline_water_usage_l)} L`],
                    ["Baseline windows", this._formatPopoverInteger(row.baseline_irrigation_events)],
                    ["Baseline valves", this._formatPopoverValves(row, "baseline")]
                ],
                "Sparse Water Usage (L)": [
                    ["Sparse water", `${this._formatPopoverNumber(row.sparse_water_usage_l)} L`],
                    ["Sparse windows", this._formatPopoverInteger(row.sparse_irrigation_events)],
                    ["Sparse valves", this._formatPopoverValves(row, "sparse")]
                ],
                "Sparse-Sensing Irrigation (L)": [
                    ["Sparse water", `${this._formatPopoverNumber(row.sparse_water_usage_l)} L`],
                    ["Sparse windows", this._formatPopoverInteger(row.sparse_irrigation_events)],
                    ["Sparse valves", this._formatPopoverValves(row, "sparse")]
                ],
                "ANFIS Water Usage (L)": [
                    ["ANFIS water", `${this._formatPopoverNumber(row.anfis_water_usage_l)} L`],
                    ["ANFIS windows", this._formatPopoverInteger(row.anfis_irrigation_events)],
                    ["ANFIS valves", this._formatPopoverValves(row, "anfis")]
                ],
                "Fuzzy Water Usage (L)": [
                    ["Fuzzy water", `${this._formatPopoverNumber(row.fuzzy_water_usage_l)} L`],
                    ["Fuzzy windows", this._formatPopoverInteger(row.fuzzy_irrigation_events)],
                    ["Fuzzy valves", this._formatPopoverValves(row, "fuzzy")]
                ],
                [ANFIS_SCORE_MEASURE]: [
                    ["Zone signal", this._formatPopoverPercent(row.predicted_probability_percent)],
                    ["Max valve probability", this._formatPopoverPercent(row.trigger_probability_percent)],
                    ["Decision threshold", this._formatPopoverPercent(row.anfis_decision_threshold_percent)],
                    ["Average category", row.predicted_category || "N/A"],
                    ["Max valve category", row.trigger_predicted_category || "N/A"],
                    ["ANFIS valves", this._formatPopoverValves(row, "anfis")]
                ],
                [FUZZY_SCORE_MEASURE]: [
                    ["Irrigation score", this._formatPopoverPercent(row.fuzzy_prescription_score_pct)],
                    ["Prescription", `${this._formatPopoverNumber(row.fuzzy_water_usage_l)} L`],
                    ["Temperature", `${this._formatPopoverNumber(row.max_temperature)} °C`],
                    ["Precipitation", `${this._formatPopoverNumber(row.rain_amount)} mm`],
                    ["Fuzzy valves", this._formatPopoverValves(row, "fuzzy")]
                ]
            };
            return details[measureName] || [];
        },

        _extractPopoverMeasureName(data, chartId) {
            const measures = CHART_MEASURES[chartId] || [];
            const explicitMeasure = this._findPopoverMeasureByExplicitKey(data, measures)
                || this._findPopoverMeasureBySingleMeasureKey(data, measures);
            if (explicitMeasure) {
                return explicitMeasure;
            }

            const matches = this._flattenPopoverValues(data);
            const matchedMeasures = measures.filter((item) => matches.includes(item));
            if (matchedMeasures.length === 1) {
                return matchedMeasures[0];
            }

            if (matches.includes("Rain (L/m²)") || matches.includes("Rain (L/m2)") || matches.includes("Rain (mm)")) {
                return "Rain (mm)";
            }
            return "";
        },

        _findPopoverMeasureByExplicitKey(value, measures) {
            const measureKeys = new Set([
                "MeasureNamesDimension",
                "measureNamesDimension",
                "Measure Names",
                "measure",
                "Measure",
                "MeasureName",
                "measureName",
                "measureNames",
                "series",
                "seriesName"
            ]);
            let found = "";
            const visit = (item) => {
                if (found || item === null || item === undefined || typeof item !== "object") {
                    return;
                }
                Object.keys(item).forEach((key) => {
                    if (found) {
                        return;
                    }
                    if (measureKeys.has(key)) {
                        const values = this._flattenPopoverValues(item[key]);
                        found = measures.find((measure) => values.includes(measure)) || "";
                    }
                });
                if (!found) {
                    Object.keys(item).forEach((key) => visit(item[key]));
                }
            };
            visit(value);
            return found;
        },

        _findPopoverMeasureBySingleMeasureKey(value, measures) {
            let found = "";
            const visit = (item) => {
                if (found || item === null || item === undefined || typeof item !== "object") {
                    return;
                }
                const presentMeasures = measures.filter((measure) => Object.prototype.hasOwnProperty.call(item, measure));
                if (presentMeasures.length === 1) {
                    found = presentMeasures[0];
                    return;
                }
                Object.keys(item).forEach((key) => visit(item[key]));
            };
            visit(value);
            return found;
        },

        _findPopoverChartRow(data, chartId, measureName) {
            const rows = this.getView().getModel().getProperty(this._chartDataPath(chartId)) || [];
            const values = this._flattenPopoverValues(data);
            const strings = values.filter((value) => typeof value === "string");

            return rows.find((row) => strings.includes(row.chart_label))
                || rows.find((row) => strings.includes(row.day_label))
                || rows.find((row) => strings.includes(row.timestamp))
                || rows.find((row) => strings.includes(row.date))
                || this._findPopoverRowByMeasureValue(data, measureName, rows)
                || this._findPopoverRowByIndex(data, rows);
        },

        _findPopoverRowByMeasureValue(data, measureName, rows) {
            const fields = this._measureFieldCandidates(measureName);
            if (!fields.length) {
                return null;
            }
            const selectedValues = this._flattenPopoverValues(data)
                .map((value) => this._popoverNumberFromAny(value))
                .filter(Number.isFinite);
            if (!selectedValues.length) {
                return null;
            }

            const matches = rows.filter((row) => fields.some((field) => {
                const rowValue = this._popoverNumber(row && row[field]);
                return Number.isFinite(rowValue) && selectedValues.some((value) => Math.abs(value - rowValue) < 0.01);
            }));
            return matches.length === 1 ? matches[0] : null;
        },

        _measureFieldCandidates(measureName) {
            return {
                "Baseline Moisture": ["baseline_moisture"],
                "Sparse Moisture": ["sparse_moisture"],
                "ANFIS Moisture": ["anfis_moisture"],
                "Fuzzy Moisture": ["fuzzy_moisture"],
                [ANFIS_SCORE_MEASURE]: ["predicted_probability_percent"],
                "Baseline Irrigation (L)": ["baseline_water_usage_l", "baseline_water_usage_chart"],
                "Baseline Water Usage (L)": ["baseline_water_usage_chart", "baseline_water_usage_l"],
                "Sparse-Sensing Irrigation (L)": ["sparse_water_usage_l", "sparse_water_usage_chart"],
                "Sparse Water Usage (L)": ["sparse_water_usage_chart", "sparse_water_usage_l"],
                "ANFIS Water Usage (L)": ["anfis_water_usage_l", "anfis_water_usage_chart"],
                "Fuzzy Water Usage (L)": ["fuzzy_water_usage_l", "fuzzy_water_usage_chart"],
                [FUZZY_SCORE_MEASURE]: ["fuzzy_prescription_score_pct"],
                "Max Temperature (°C)": ["max_temperature", "temperature"],
                "Max Temp (°C)": ["max_temperature", "temperature"],
                "Max Temperature (C)": ["max_temperature", "temperature"],
                "Max Temp (C)": ["max_temperature", "temperature"],
                "Rain (mm)": ["rain_amount"],
                "Rain (L/m²)": ["rain_amount"]
            }[measureName] || [];
        },

        _findPopoverRowByIndex(data, rows) {
            const indexes = [];
            const indexKeys = new Set(["dataIndex", "rowIndex", "pointIndex"]);
            const visit = (item) => {
                if (item === null || item === undefined) {
                    return;
                }
                if (typeof item === "string") {
                    const pathMatch = item.match(/\/(\d+)$/);
                    if (pathMatch) {
                        indexes.push(Number(pathMatch[1]));
                    }
                    return;
                }
                if (Array.isArray(item)) {
                    item.forEach(visit);
                    return;
                }
                if (typeof item === "object") {
                    Object.keys(item).forEach((key) => {
                        if (indexKeys.has(key) && Number.isInteger(item[key])) {
                            indexes.push(item[key]);
                        } else {
                            visit(item[key]);
                        }
                    });
                }
            };
            visit(data);
            const index = indexes.find((value) => Number.isInteger(value) && value >= 0 && value < rows.length);
            return Number.isInteger(index) ? rows[index] : null;
        },

        _popoverMinTemperature(row, chartId) {
            const explicitMin = this._popoverNumber(row && row.min_temperature);
            if (Number.isFinite(explicitMin)) {
                return explicitMin;
            }

            const rows = this._popoverRowsForDate(row, chartId);
            const dayTemperatures = rows.map((item) => this._popoverNumber(
                item.min_temperature ?? item.temperature ?? item.max_temperature
            )).filter(Number.isFinite);

            if (dayTemperatures.length) {
                return Math.min(...dayTemperatures);
            }

            return row ? row.temperature ?? row.max_temperature : null;
        },

        _popoverCloudCover(row, chartId) {
            if (this._isHourlyChartRow(row)) {
                return row ? row.cloud_cover_pct : null;
            }

            const rows = this._popoverRowsForDate(row, chartId);
            const cloudCoverValues = rows.map((item) => this._popoverNumber(item.cloud_cover_pct)).filter(Number.isFinite);

            if (cloudCoverValues.length) {
                const total = cloudCoverValues.reduce((sum, value) => sum + value, 0);
                return total / cloudCoverValues.length;
            }

            return row ? row.cloud_cover_pct : null;
        },

        _isHourlyChartRow(row) {
            return Boolean(row && row.hour);
        },

        _popoverRowsForDate(row, chartId) {
            const date = row && this._chartRowDateKey(row);
            if (!date) {
                return [];
            }

            const model = this.getView().getModel();
            const sourceRows = model.getProperty(this._chartSourceDataPath(chartId));
            const rows = Array.isArray(sourceRows) && sourceRows.length
                ? sourceRows
                : model.getProperty(this._chartDataPath(chartId)) || [];
            return rows.filter((item) => item && this._chartRowDateKey(item) === date);
        },

        _chartRowDateKey(row) {
            return row && (row.date || this._chartRowDate(row));
        },

        _chartRowDate(row) {
            const timestamp = entryTimestamp(row);
            return timestamp ? timestamp.toISOString().slice(0, 10) : "";
        },

        _popoverNumber(value) {
            const numberValue = Number(value);
            return Number.isFinite(numberValue) ? numberValue : null;
        },

        _popoverNumberFromAny(value) {
            if (typeof value === "number") {
                return Number.isFinite(value) ? value : null;
            }
            if (typeof value !== "string") {
                return null;
            }
            const normalized = value.replace(",", ".").replace(/[^0-9.+-]/g, "");
            if (!normalized) {
                return null;
            }
            const numberValue = Number(normalized);
            return Number.isFinite(numberValue) ? numberValue : null;
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
            if (CHART_DATA_PATHS[chartId]) {
                return CHART_DATA_PATHS[chartId];
            }
            return {
                samplingChart: "/samplingChartEntries",
                anfisChart: "/anfisChartEntries",
                fuzzyChart: "/fuzzyChartEntries"
            }[chartId] || "/samplingChartEntries";
        },

        _chartSourceDataPath(chartId) {
            if (CHART_SOURCE_DATA_PATHS[chartId]) {
                return CHART_SOURCE_DATA_PATHS[chartId];
            }
            return {
                samplingChart: "/samplingChartAllEntries",
                anfisChart: "/anfisChartAllEntries",
                fuzzyChart: "/fuzzyChartAllEntries"
            }[chartId] || "/samplingChartAllEntries";
        },

        _formatPopoverNumber(value) {
            const numberValue = Number(value);
            return Number.isFinite(numberValue) ? numberValue.toFixed(2) : "N/A";
        },

        _formatPopoverPercent(value) {
            return `${this._formatPopoverNumber(value)}%`;
        },

        _formatPopoverInteger(value) {
            const numberValue = Number(value);
            return Number.isFinite(numberValue) ? String(Math.round(numberValue)) : "N/A";
        },

        _formatPopoverValves(row, prefix) {
            const label = row && row[`${prefix}_activated_valves`];
            if (label) {
                return label;
            }
            const runs = Number(row && row[`${prefix}_valve_runs`]);
            return Number.isFinite(runs) && runs > 0 ? this._formatPopoverInteger(runs) : "none";
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

            this._thinCategoryAxisLabels(chartDom, chartId);
            this._syncCustomChartLegend(chartDom, chartId);
            const overlay = this._chartOverlayElement(chartDom);
            if (!overlay) {
                return;
            }
            const geometry = this._chartOverlayGeometry(chartDom, chartId);
            this._clearChartOverlay(overlay);
            if (!geometry) {
                return;
            }

            overlay.insertAdjacentHTML("beforeend", this._chartOverlayMarkup(geometry, chartId));
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
            const top = this._chartOverlayPx(geometry.top);
            const chartHeight = this._chartOverlayPx(geometry.bottom - geometry.top);
            const markup = [];
            const futureBandLabel = String(chartId || "").includes("MoistureChart")
                ? "Simulation"
                : "Forecast / Simulation";

            if (Number.isFinite(geometry.nowX)) {
                const nowX = this._chartOverlayPx(geometry.nowX);
                const futureWidth = this._chartOverlayPx(Math.max(0, geometry.right - geometry.nowX));
                markup.push(
                    `<div class="dtChartFutureBand" style="left:${nowX};top:${top};width:${futureWidth};height:${chartHeight};">` +
                    `<span class="dtChartFutureBandLabel">${futureBandLabel}</span>` +
                    `</div>`
                );
            }

            if (Number.isFinite(geometry.thresholdY) && Number.isFinite(geometry.thresholdPct)) {
                const thresholdY = this._chartOverlayPx(geometry.thresholdY);
                const thresholdLeft = this._chartOverlayPx(geometry.left);
                const thresholdWidth = this._chartOverlayPx(geometry.right - geometry.left);
                const thresholdPct = this._formatThresholdPct(geometry.thresholdPct);
                const thresholdTitle = `Comfort threshold ${thresholdPct}%`;
                markup.push(
                    `<div class="dtChartThresholdLine" title="${thresholdTitle}" aria-label="${thresholdTitle}" tabindex="0" style="left:${thresholdLeft};top:${thresholdY};width:${thresholdWidth};border-color:${MOISTURE_THRESHOLD_COLOR};">` +
                    `<span class="dtChartThresholdLabel">${thresholdTitle}</span>` +
                    `</div>`
                );
            }

            if (Number.isFinite(geometry.nowX)) {
                const nowX = this._chartOverlayPx(geometry.nowX);
                markup.push(
                    `<div class="dtChartNowLine" style="left:${nowX};top:${top};height:${chartHeight};"></div>`
                );
                if (this._showsBoundaryBadge(chartId)) {
                    const badgeTop = this._chartOverlayPx(Math.max(0, geometry.top - 23));
                    markup.push(
                        `<div class="dtChartNowBadge" style="left:${nowX};top:${badgeTop};">Forecast boundary</div>`
                    );
                }
            }

            return markup.join("");
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
                `<span class="dtChartLegendItem">` +
                `<span class="dtChartLegendMarker dtChartLegendMarker-${shape}" style="--dt-chart-series-color:${color};"></span>` +
                `<span>${this._escapeHtml(label)}</span>` +
                `</span>`
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
            formatter.registerCustomFormatter("DT_LM2", (value) => `${formatNumber(value)} L/m²`);
            formatter.registerCustomFormatter("DT_NUMBER", (value) => formatNumber(value));
            Format.numericFormatter(formatter);
        }

    };
});
