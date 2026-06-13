from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from threading import RLock
from typing import Any

from digital_twin.core.time import now_local

PRESCRIPTION_EXPERIMENTS = ("baseline", "sampling", "anfis", "fuzzy_dt")
WEATHER_CALIBRATION_POLICY = "stored-weather-observed-or-forecast-no-soil-calibration"
SENSOR_CALIBRATION_POLICY = "stored-soil-marker-05:30-hot-day-17:30"


@dataclass(frozen=True)
class RuntimeIrrigationPrescription:
    experiment_type: str
    target_date: date
    computed_at: datetime
    source_start_date: date
    source_end_date: date
    planned_volume_ml: float
    valve_runs: int
    payload: dict[str, Any]

    def db_row(self, dispatched_at: datetime) -> dict[str, Any]:
        return {
            "experiment_type": self.experiment_type,
            "prescription_date": self.target_date.isoformat(),
            "computed_at": self.computed_at.isoformat(),
            "dispatched_at": dispatched_at.isoformat(),
            "planned_volume_ml": round(self.planned_volume_ml, 2),
            "valve_runs": self.valve_runs,
            "payload": self.payload,
        }


class RuntimePrescriptionStore:
    """Stores the latest in-memory irrigation prescription per experiment."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._items: dict[str, RuntimeIrrigationPrescription] = {}

    def upsert_from_result(
        self,
        experiment_type: str,
        start_date: date,
        end_date: date,
        result: dict[str, Any],
    ) -> RuntimeIrrigationPrescription:
        prescription = self._build_prescription(experiment_type, start_date, end_date, result)
        with self._lock:
            self._items[experiment_type] = prescription
        return prescription

    def latest(self, experiment_type: str | None = None) -> list[RuntimeIrrigationPrescription]:
        with self._lock:
            if experiment_type is not None:
                item = self._items.get(experiment_type)
                return [item] if item is not None else []
            return [
                self._items[key]
                for key in PRESCRIPTION_EXPERIMENTS
                if key in self._items
            ]

    def latest_for_date(self, target_date: date) -> list[RuntimeIrrigationPrescription]:
        return [
            item
            for item in self.latest()
            if item.target_date == target_date
        ]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    @staticmethod
    def _build_prescription(
        experiment_type: str,
        start_date: date,
        end_date: date,
        result: dict[str, Any],
    ) -> RuntimeIrrigationPrescription:
        events = list(result.get("sampleEvents") or [])
        summary = dict(result.get("summary") or {})
        planned_volume_ml = sum(float(event.get("planned_volume_ml") or 0.0) for event in events)
        if not events and summary.get("totalWaterUsage") is not None:
            planned_volume_ml = float(summary.get("totalWaterUsage") or 0.0) * 1000.0
        valve_runs = len(events) if events else int(summary.get("valveRuns") or 0)
        target_date = end_date
        payload = {
            "experiment_type": experiment_type,
            "target_date": target_date.isoformat(),
            "source_start_date": start_date.isoformat(),
            "source_end_date": end_date.isoformat(),
            "sensor_calibration_policy": SENSOR_CALIBRATION_POLICY,
            "weather_calibration_policy": WEATHER_CALIBRATION_POLICY,
            "summary": _prescription_summary(summary),
            "events": events,
        }
        return RuntimeIrrigationPrescription(
            experiment_type=experiment_type,
            target_date=target_date,
            computed_at=now_local(),
            source_start_date=start_date,
            source_end_date=end_date,
            planned_volume_ml=planned_volume_ml,
            valve_runs=valve_runs,
            payload=payload,
        )


def _prescription_summary(summary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "totalWaterUsage",
        "averageDailyWaterUsage",
        "valveRuns",
        "irrigationEvents",
        "irrigationDecisions",
        "decisionLevel",
        "sensorDataUsed",
        "sensorSource",
        "sensorRows",
        "source",
    )
    return {key: summary[key] for key in keys if key in summary}


runtime_prescription_store = RuntimePrescriptionStore()
