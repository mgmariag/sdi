from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from digital_twin.application.control_loop.ports import (
    ActuationPort,
    ExperimentPrescriptionPort,
)
from digital_twin.core.time import today_local


class PrescriptionStage:
    """Prepares and dispatches next-day irrigation prescriptions."""

    def __init__(
        self,
        experiment_service: ExperimentPrescriptionPort,
        actuation_service: ActuationPort,
    ) -> None:
        self.experiment_service = experiment_service
        self.actuation_service = actuation_service

    def prepare_next_day(self, target: date | None = None) -> dict[str, Any]:
        target_date = target or self.default_target_date()
        return self.experiment_service.prepare_tomorrow_prescriptions(target_date)

    def dispatch_next_day(self, target: date | None = None) -> dict[str, Any]:
        target_date = target or self.default_target_date()
        prepared = self.prepare_next_day(target_date)
        dispatch = self.actuation_service.store_prescriptions(target_date=target_date)
        return {
            **prepared,
            "dispatch": dispatch,
        }

    @staticmethod
    def default_target_date() -> date:
        return today_local() + timedelta(days=1)
