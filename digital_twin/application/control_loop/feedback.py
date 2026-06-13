from __future__ import annotations

from typing import Any, Protocol


class ActuationFeedbackPort(Protocol):
    def apply_completed_actuation(self, completed_row: dict[str, Any]) -> dict[str, Any]:
        ...


class FeedbackStage:
    """Applies actuator feedback to the sensed plant state."""

    def __init__(self, feedback_service: ActuationFeedbackPort) -> None:
        self.feedback_service = feedback_service

    def apply_completed_actuation(self, completed_row: dict[str, Any]) -> dict[str, Any]:
        return self.feedback_service.apply_completed_actuation(completed_row)
