from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.application.control_loop.decision import DecisionStage
from digital_twin.application.control_loop.prescriptions import PrescriptionStage


class RuntimeControlLoop:
    """Coordinates runtime prescription dispatch and actuator feedback consumption."""

    def __init__(
        self,
        experiment_service: Any | None = None,
        actuation_service: Any | None = None,
    ) -> None:
        if experiment_service is None:
            from digital_twin.application.experiments.experiment_service import (
                ExperimentService,
            )

            experiment_service = ExperimentService()
        if actuation_service is None:
            from digital_twin.application.control_loop.irrigation_actuation import (
                IrrigationActuationService,
            )

            actuation_service = IrrigationActuationService()
        self.experiment_service = experiment_service
        self.actuation_service = actuation_service
        self.decision = DecisionStage(self.experiment_service)
        self.prescriptions = PrescriptionStage(self.experiment_service, self.actuation_service)

    def prepare_next_day_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        return self.prescriptions.prepare_next_day(target)

    def dispatch_next_day_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        return self.prescriptions.dispatch_next_day(target)

    def run_due_actuation(self, actuator_node: str = "irrigation-actuator", limit: int = 100) -> dict[str, Any]:
        return self.actuation_service.run_due_prescription_windows(actuator_node=actuator_node, limit=limit)

    def actuation_summary(self) -> dict[str, Any]:
        return self.actuation_service.summary()

