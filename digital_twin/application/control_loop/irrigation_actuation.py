from __future__ import annotations

from datetime import date, datetime
from typing import Any

from digital_twin.application.control_loop.runtime_prescriptions import (
    RuntimeIrrigationPrescription,
    runtime_prescription_store,
)
from digital_twin.application.control_loop.actuation_feedback import ActuationFeedbackService
from digital_twin.core.time import now_local
from digital_twin.infrastructure.database.repositories.experiment_repository import (
    ActuationRepository,
)


class IrrigationActuationService:
    """Simulates an actuator node consuming planned irrigation commands."""

    def __init__(
        self,
        repository: ActuationRepository | None = None,
        feedback_service: ActuationFeedbackService | None = None,
    ) -> None:
        self.repository = repository or ActuationRepository()
        self.feedback_service = feedback_service or ActuationFeedbackService()

    def store_prescriptions(
        self,
        prescriptions: list[RuntimeIrrigationPrescription] | None = None,
        target_date: date | None = None,
        dispatched_at: datetime | None = None,
    ) -> dict[str, Any]:
        dispatched_at = dispatched_at or now_local()
        items = prescriptions if prescriptions is not None else runtime_prescription_store.latest()
        if target_date is not None:
            items = [item for item in items if item.target_date == target_date]
        rows = [item.db_row(dispatched_at) for item in items]
        stored = self.repository.store_prescriptions(rows)
        return {
            "dispatchedAt": dispatched_at.isoformat(),
            "targetDate": target_date.isoformat() if target_date else None,
            "storedCount": len(stored),
            "stored": stored,
        }

    def run_due(self, actuator_node: str = "irrigation-actuator", limit: int = 100) -> dict[str, Any]:
        completed = []
        failed = []
        feedback = []
        for actuation in self.repository.due(limit=limit):
            try:
                completed_row = self.repository.mark_completed(actuation["id"], actuator_node=actuator_node)
            except Exception as exc:
                failed.append(self.repository.mark_failed(actuation["id"], actuator_node, str(exc)))
                continue
            if completed_row:
                completed.append(completed_row)
                try:
                    feedback.append(self.feedback_service.apply_completed_actuation(completed_row))
                except Exception as exc:
                    feedback.append(
                        {
                            "actuationId": completed_row.get("id"),
                            "feedbackRows": 0,
                            "correctiveCount": 0,
                            "error": str(exc),
                        }
                    )
        return {
            "actuatorNode": actuator_node,
            "completedCount": len(completed),
            "failedCount": len(failed),
            "feedbackCount": sum(1 for item in feedback if item.get("feedbackRows")),
            "correctiveCount": sum(int(item.get("correctiveCount") or 0) for item in feedback),
            "completed": completed,
            "feedback": feedback,
            "failed": failed,
        }

    def run_due_prescription_windows(
        self,
        actuator_node: str = "irrigation-actuator",
        limit: int = 100,
    ) -> dict[str, Any]:
        materialized = self.repository.materialize_due_prescription_events(limit=limit)
        result = self.run_due(actuator_node=actuator_node, limit=limit)
        result["materializedCount"] = len(materialized)
        result["materialized"] = materialized
        return result

    def summary(self) -> dict[str, Any]:
        return self.repository.summary()

