from __future__ import annotations

import unittest
from datetime import date

from digital_twin.application.control_loop.runtime import RuntimeControlLoop


class RuntimeControlLoopTests(unittest.TestCase):
    def test_dispatch_prepares_then_stores_prescriptions_for_target_date(self) -> None:
        experiment_service = _FakeExperimentService()
        actuation_service = _FakeActuationService()
        control_loop = RuntimeControlLoop(
            experiment_service=experiment_service,
            actuation_service=actuation_service,
        )

        result = control_loop.dispatch_next_day_prescriptions(date(2026, 6, 14))

        self.assertEqual(experiment_service.prepared_target, date(2026, 6, 14))
        self.assertEqual(actuation_service.stored_target, date(2026, 6, 14))
        self.assertEqual(result["targetDate"], "2026-06-14")
        self.assertEqual(result["dispatch"]["storedCount"], 4)

    def test_run_due_actuation_materializes_and_consumes_due_windows(self) -> None:
        actuation_service = _FakeActuationService()
        control_loop = RuntimeControlLoop(
            experiment_service=_FakeExperimentService(),
            actuation_service=actuation_service,
        )

        result = control_loop.run_due_actuation(actuator_node="node-1", limit=5)

        self.assertEqual(actuation_service.run_due_args, ("node-1", 5))
        self.assertEqual(result["materializedCount"], 1)
        self.assertEqual(result["completedCount"], 1)


class _FakeExperimentService:
    def __init__(self) -> None:
        self.prepared_target: date | None = None

    def prepare_tomorrow_prescriptions(self, target: date | None = None) -> dict:
        self.prepared_target = target
        return {
            "targetDate": target.isoformat() if target else None,
            "preparedExperiments": ["baseline", "sampling", "anfis", "fuzzy_dt"],
            "runtimePrescriptionCount": 4,
        }


class _FakeActuationService:
    def __init__(self) -> None:
        self.stored_target: date | None = None
        self.run_due_args: tuple[str, int] | None = None

    def store_prescriptions(self, target_date: date | None = None) -> dict:
        self.stored_target = target_date
        return {"targetDate": target_date.isoformat() if target_date else None, "storedCount": 4}

    def run_due_prescription_windows(self, actuator_node: str, limit: int) -> dict:
        self.run_due_args = (actuator_node, limit)
        return {"materializedCount": 1, "completedCount": 1, "failedCount": 0}

    def summary(self) -> dict:
        return {"planned": 0}


if __name__ == "__main__":
    unittest.main()
