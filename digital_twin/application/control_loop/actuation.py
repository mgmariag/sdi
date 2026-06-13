from __future__ import annotations

from typing import Any

from digital_twin.application.control_loop.ports import ActuationPort


class ActuationStage:
    """Materializes and consumes due irrigation actuator work."""

    def __init__(self, actuation_service: ActuationPort) -> None:
        self.actuation_service = actuation_service

    def run_due(self, actuator_node: str = "irrigation-actuator", limit: int = 100) -> dict[str, Any]:
        return self.actuation_service.run_due_prescription_windows(actuator_node=actuator_node, limit=limit)

    def summary(self) -> dict[str, Any]:
        return self.actuation_service.summary()
