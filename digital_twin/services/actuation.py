from __future__ import annotations

from typing import Any

from digital_twin.db.actuation_repository import ActuationRepository


class IrrigationActuationService:
    """Simulates an actuator node consuming planned irrigation commands."""

    def __init__(self, repository: ActuationRepository | None = None) -> None:
        self.repository = repository or ActuationRepository()

    def run_due(self, actuator_node: str = "irrigation-actuator", limit: int = 100) -> dict[str, Any]:
        completed = []
        failed = []
        for actuation in self.repository.due(limit=limit):
            try:
                completed_row = self.repository.mark_completed(actuation["id"], actuator_node=actuator_node)
                if completed_row:
                    completed.append(completed_row)
            except Exception as exc:
                failed.append(self.repository.mark_failed(actuation["id"], actuator_node, str(exc)))
        return {
            "actuatorNode": actuator_node,
            "completedCount": len(completed),
            "failedCount": len(failed),
            "completed": completed,
            "failed": failed,
        }

    def summary(self) -> dict[str, Any]:
        return self.repository.summary()
