sap.ui.define([
    "disertatie/model/chart/chartConfig",
    "disertatie/model/chart/chartData",
    "sap/viz/ui5/controls/Popover",
    "sap/ui/core/HTML"
], (ChartConfig, ChartData, Popover, HTML) => {
    "use strict";

    const {
        ANFIS_SCORE_MEASURE,
        CHART_MEASURES,
        FUZZY_SCORE_MEASURE
    } = ChartConfig;
    const { entryTimestamp } = ChartData;

    return {
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
            let lines = [];
            if (row && measureName) {
                lines = this._chartDetailLines(row, chartId, measureName);
            } else if (row) {
                lines = this._chartPointSummaryLines(row, chartId);
            }

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
                    ["Watered pots before", this._formatPopoverPercent(row.anfis_irrigated_pre_moisture)],
                    ["Watered pots after", this._formatPopoverPercent(row.anfis_irrigated_post_moisture)],
                    ["Watered-pot gain", this._formatPopoverPercent(row.anfis_irrigated_moisture_gain)],
                    ["ANFIS valves", this._formatPopoverValves(row, "anfis")],
                    ["ANFIS water", `${this._formatPopoverNumber(row.anfis_water_usage_l)} L`]
                ],
                "Fuzzy Moisture": [
                    ["Fuzzy moisture", this._formatPopoverPercent(row.fuzzy_moisture)],
                    ["Watered pots before", this._formatPopoverPercent(row.fuzzy_irrigated_pre_moisture)],
                    ["Watered pots after", this._formatPopoverPercent(row.fuzzy_irrigated_post_moisture)],
                    ["Watered-pot gain", this._formatPopoverPercent(row.fuzzy_irrigated_moisture_gain)],
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
                    ["Valve activation signal", this._formatPopoverPercent(row.trigger_probability_percent)],
                    ["Decision threshold", this._formatPopoverPercent(row.anfis_decision_threshold_percent)],
                    ["Average category", row.predicted_category || "N/A"],
                    ["Activation category", row.trigger_predicted_category || "N/A"],
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

        _formatPopoverNumber(value) {
            if (value === null || value === undefined || value === "") {
                return "N/A";
            }
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
        }
    };
});
