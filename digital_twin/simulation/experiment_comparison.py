from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.metrics import add_chart_summary
from digital_twin.simulation.result_helpers import (
    average_moisture_from_state_payload,
    comparison_pot_info_entries,
)
from digital_twin.simulation.shared.types import ExperimentSnapshot
from digital_twin.simulation.state.lookback import combined_line_metadata
from digital_twin.simulation.valves.rollups import comparison_window_fields


class ExperimentComparison:
    """Builds baseline-vs-controller experiment payloads."""

    SENSOR_SUMMARY_KEYS = (
        "sensorDataUsed",
        "sensorSource",
        "sensorRows",
        "sensorLocationCount",
        "sensorAssociatedPotCount",
        "latestStateRows",
        "sensorMappedDays",
        "sensorDateMappings",
        "sensorFirstDate",
        "sensorLastDate",
        "latestKnownSoilStateAt",
        "futureStateEstimated",
        "futureEstimatedDays",
        "futureEstimatedDateRange",
        "sensorError",
    )

    def __init__(
        self,
        start_date: date,
        end_date: date,
        snapshot: ExperimentSnapshot,
        baseline: dict[str, Any],
        comparison: dict[str, Any],
        prefix: str,
        water_usage_field: str,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.snapshot = snapshot
        self.baseline = baseline
        self.comparison = comparison
        self.prefix = prefix
        self.water_usage_field = water_usage_field
        self.baseline_summary = baseline["summary"]
        self.comparison_summary = comparison["summary"]
        self._baseline_by_date = {entry["date"]: entry for entry in baseline["entries"]}
        self.shared_initial_moisture = average_moisture_from_state_payload(
            baseline.get("stateAtExperimentStart")
        )

    def daily_entries(self, row_builder) -> list[dict[str, Any]]:
        return self._align_first_row_to_shared_initial_moisture(
            self._aligned_rows(
                self.comparison["entries"],
                self._baseline_by_date,
                row_builder,
            )
        )

    def chart_entries(self, row_builder) -> list[dict[str, Any]]:
        baseline_by_timestamp = {
            entry["timestamp"]: entry
            for entry in self.baseline.get("chartEntries", self.baseline["entries"])
        }
        return self._align_first_row_to_shared_initial_moisture(
            self._aligned_rows(
                self.comparison.get("chartEntries", self.comparison["entries"]),
                baseline_by_timestamp,
                row_builder,
                fallback_lookup=self._baseline_by_date,
                lookup_key="timestamp",
            )
        )

    def row(
        self,
        baseline_entry: dict[str, Any],
        comparison_entry: dict[str, Any],
        extra: dict[str, Any] | None = None,
        include_moisture_alias: bool = False,
    ) -> dict[str, Any]:
        baseline_active = self.is_active(baseline_entry)
        comparison_active = self.is_active(comparison_entry)
        row = {
            "date": comparison_entry["date"],
            "timestamp": comparison_entry["timestamp"],
            "day_label": comparison_entry["day_label"],
            "chart_label": comparison_entry.get("chart_label", comparison_entry["day_label"]),
            "baseline_moisture": baseline_entry["average_moisture"],
            f"{self.prefix}_moisture": comparison_entry["average_moisture"],
            "temperature": comparison_entry["temperature"],
            "max_temperature": comparison_entry["max_temperature"],
            "min_temperature": comparison_entry.get("min_temperature", comparison_entry["temperature"]),
            "humidity": comparison_entry["humidity"],
            "cloud_cover_pct": comparison_entry["cloud_cover_pct"],
            "rain_prediction": comparison_entry["rain_prediction"],
            "rain_amount": comparison_entry["rain_amount"],
            "baseline_irrigation_active": baseline_active,
            f"{self.prefix}_irrigation_active": comparison_active,
            "baseline_irrigation_events": baseline_entry["irrigation_events"],
            f"{self.prefix}_irrigation_events": comparison_entry["irrigation_events"],
            "baseline_valve_runs": baseline_entry.get("valve_runs", baseline_entry["irrigation_events"]),
            f"{self.prefix}_valve_runs": comparison_entry.get("valve_runs", comparison_entry["irrigation_events"]),
            "baseline_water_usage_l": baseline_entry["water_usage_l"],
            f"{self.prefix}_water_usage_l": comparison_entry["water_usage_l"],
            "baseline_water_usage_ml": baseline_entry["water_usage_ml"],
            f"{self.prefix}_water_usage_ml": comparison_entry["water_usage_ml"],
            **comparison_window_fields("baseline", baseline_entry),
            **comparison_window_fields(self.prefix, comparison_entry),
            "alerts": comparison_entry["alerts"],
            **combined_line_metadata(baseline_entry, comparison_entry),
        }
        if include_moisture_alias:
            row["moisture"] = baseline_entry["average_moisture"]
        if extra:
            row.update(extra)
        return row

    def base_summary(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        total_entries = len(entries)
        return {
            "totalEntries": total_entries,
            "daysAnalyzed": total_entries,
            "potsAnalyzed": self.comparison_summary["potsAnalyzed"],
        }

    def irrigation_day_counts(self, entries: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "baseline_irrigation_days": self.active_days(entries, "baseline"),
            f"{self.prefix}_irrigation_days": self.active_days(entries, self.prefix),
        }

    def total_usage_counts(self, entries: list[dict[str, Any]]) -> dict[str, int | float]:
        return {
            "baseline_total_water_usage_l": self.water_total(entries, "baseline"),
            f"{self.prefix}_total_water_usage_l": self.water_total(entries, self.prefix),
            "baseline_irrigation_event_count": self.event_total(entries, "baseline"),
            f"{self.prefix}_irrigation_event_count": self.event_total(entries, self.prefix),
            "baseline_valve_run_count": self.valve_run_total(entries, "baseline"),
            f"{self.prefix}_valve_run_count": self.valve_run_total(entries, self.prefix),
        }

    def decision_counts(self) -> dict[str, int]:
        return {
            "baseline_irrigation_decisions": self.baseline_summary["irrigationDecisions"],
            f"{self.prefix}_irrigation_decisions": self.comparison_summary["irrigationDecisions"],
        }

    def sensor_summary(self, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
        allowed = keys or self.SENSOR_SUMMARY_KEYS
        return {key: self.baseline_summary[key] for key in allowed if key in self.baseline_summary}

    def shared_initial_summary(self) -> dict[str, Any]:
        if self.shared_initial_moisture is None:
            return {
                "firstPointAlignmentPolicy": "unavailable",
            }
        return {
            "firstPointAlignmentPolicy": "shared_experiment_start_state",
            "sharedInitialMoisture": self.shared_initial_moisture,
        }

    def payload(
        self,
        entries: list[dict[str, Any]],
        chart_entries: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        summary.update(self.shared_initial_summary())
        add_chart_summary(summary, chart_entries, self.start_date, self.end_date)
        return {
            "entries": entries,
            "chartEntries": chart_entries,
            "summary": summary,
            "pots": comparison_pot_info_entries(
                self.snapshot.pots,
                self.baseline,
                self.comparison,
                self.water_usage_field,
            ),
            "sampleDecisions": self.comparison.get("sampleDecisions", [])[:200],
            "sampleEvents": self.comparison.get("sampleEvents", [])[:200],
            "sampleAlerts": self.comparison.get("sampleAlerts", [])[:200],
        }

    @staticmethod
    def is_active(entry: dict[str, Any]) -> bool:
        return int(entry.get("irrigation_events") or 0) > 0

    @staticmethod
    def active_days(entries: list[dict[str, Any]], prefix: str) -> int:
        return sum(1 for entry in entries if entry.get(f"{prefix}_irrigation_active"))

    @staticmethod
    def water_total(entries: list[dict[str, Any]], prefix: str) -> float:
        return round(sum(float(entry.get(f"{prefix}_water_usage_l") or 0.0) for entry in entries), 2)

    @staticmethod
    def event_total(entries: list[dict[str, Any]], prefix: str) -> int:
        return sum(int(entry.get(f"{prefix}_irrigation_events") or 0) for entry in entries)

    @staticmethod
    def valve_run_total(entries: list[dict[str, Any]], prefix: str) -> int:
        return sum(int(entry.get(f"{prefix}_valve_runs") or 0) for entry in entries)

    def _align_first_row_to_shared_initial_moisture(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows or self.shared_initial_moisture is None:
            return rows
        first = rows[0]
        if first.get("date") != self.start_date.isoformat():
            return rows
        aligned = dict(first)
        aligned["baseline_moisture"] = self.shared_initial_moisture
        aligned[f"{self.prefix}_moisture"] = self.shared_initial_moisture
        aligned["shared_initial_moisture"] = self.shared_initial_moisture
        aligned["first_point_alignment_policy"] = "shared_experiment_start_state"
        if "moisture" in aligned:
            aligned["moisture"] = self.shared_initial_moisture
        return [aligned, *rows[1:]]

    @staticmethod
    def _aligned_rows(
        comparison_entries: list[dict[str, Any]],
        baseline_lookup: dict[Any, dict[str, Any]],
        row_builder,
        fallback_lookup: dict[Any, dict[str, Any]] | None = None,
        lookup_key: str = "date",
    ) -> list[dict[str, Any]]:
        rows = []
        for comparison_entry in comparison_entries:
            baseline_entry = baseline_lookup.get(comparison_entry[lookup_key])
            if baseline_entry is None and fallback_lookup is not None:
                baseline_entry = fallback_lookup.get(comparison_entry["date"])
            if baseline_entry is not None:
                rows.append(row_builder(baseline_entry, comparison_entry))
        return rows
