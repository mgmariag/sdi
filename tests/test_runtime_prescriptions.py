from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal

from digital_twin.application.actuators.service import (
    IrrigationActuationService,
)
from digital_twin.application.control_loop.prescription import (
    RuntimePrescriptionStore,
)
from digital_twin.infrastructure.database.repositories.experiment_repository import (
    _experiment_run_row,
    _prescription_event_row,
)


class RuntimePrescriptionStoreTests(unittest.TestCase):
    def test_upsert_keeps_latest_prescription_per_experiment(self) -> None:
        store = RuntimePrescriptionStore()

        store.upsert_from_result(
            "baseline",
            date(2026, 5, 21),
            date(2026, 5, 22),
            _result(100.0),
        )
        prescription = store.upsert_from_result(
            "baseline",
            date(2026, 5, 21),
            date(2026, 5, 22),
            _result(250.0),
        )

        latest = store.latest("baseline")
        self.assertEqual(len(latest), 1)
        self.assertIs(latest[0], prescription)
        self.assertEqual(latest[0].planned_volume_ml, 250.0)
        self.assertEqual(latest[0].target_date, date(2026, 5, 22))
        self.assertEqual(latest[0].valve_runs, 1)
        self.assertEqual(latest[0].payload["weather_calibration_policy"], "stored-weather-observed-or-forecast-no-soil-calibration")

    def test_prescription_event_row_uses_valve_representative_pot(self) -> None:
        row = _prescription_event_row(
            {"id": 7, "experiment_type": "baseline", "prescription_date": date(2026, 5, 22)},
            {
                "scheduled_start_at": "2026-05-22T05:00:00+03:00",
                "duration_min": 10,
                "planned_volume_ml": 500,
                "flow_rate_ml_min": 50,
                "affected_pot_ids": [12, 13],
                "valve_zone": "south_rail",
                "valve_number": 2,
                "per_pot_distribution": [{"pot_id": 12, "delivered_volume_ml": 260}],
            },
        )

        self.assertIsNotNone(row)
        self.assertEqual(row["sensor_id"], 12)
        self.assertEqual(row["pot_id"], 12)
        self.assertEqual(row["planned_volume_ml"], 500.0)
        self.assertEqual(row["valve_zone"], "south_rail")
        self.assertEqual(row["valve_number"], 2)
        self.assertEqual(row["payload"].obj["per_pot_distribution"][0]["delivered_volume_ml"], 260)
        self.assertEqual(row["scheduled_end_at"], datetime.fromisoformat("2026-05-22T05:10:00+03:00"))

    def test_experiment_run_row_keeps_json_safe_snapshot(self) -> None:
        row = _experiment_run_row(
            "fuzzy_dt",
            date(2026, 5, 21),
            date(2026, 5, 22),
            {"threshold": Decimal("36.82"), "created": datetime(2026, 5, 21, 6, 0)},
            {
                "summary": {"water_savings_percent": Decimal("10.12")},
                "entries": [{"date": date(2026, 5, 22), "fuzzy_moisture": Decimal("37.4")}],
            },
        )

        self.assertEqual(row["experiment_type"], "fuzzy_dt")
        self.assertEqual(row["parameters"].obj["threshold"], 36.82)
        self.assertEqual(row["parameters"].obj["created"], "2026-05-21T06:00:00")
        self.assertEqual(row["summary"].obj["water_savings_percent"], 10.12)
        self.assertEqual(row["payload"].obj["entries"][0]["date"], "2026-05-22")

    def test_actuation_service_materializes_then_consumes_due_windows(self) -> None:
        service = IrrigationActuationService(repository=_FakeActuationRepository())

        result = service.run_due_prescription_windows(limit=5)

        self.assertEqual(result["materializedCount"], 1)
        self.assertEqual(result["completedCount"], 1)
        self.assertEqual(result["failedCount"], 0)


def _result(volume_ml: float) -> dict:
    return {
        "summary": {"totalWaterUsage": volume_ml / 1000.0, "valveRuns": 1},
        "sampleEvents": [{"planned_volume_ml": volume_ml, "valve_zone": "south_rail"}],
    }


class _FakeActuationRepository:
    def materialize_due_prescription_events(self, limit: int) -> list[dict]:
        return [{"actuationId": 1}]

    def due(self, limit: int) -> list[dict]:
        return [{"id": 1}]

    def mark_completed(self, actuation_id: int, actuator_node: str) -> dict:
        return {"id": actuation_id, "status": "completed", "actuator_node": actuator_node}

    def mark_failed(self, actuation_id: int, actuator_node: str, error: str) -> dict:
        return {"id": actuation_id, "status": "failed", "last_error": error}


if __name__ == "__main__":
    unittest.main()
