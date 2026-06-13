from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class ExperimentPrescriptionPort(Protocol):
    def prepare_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        ...


class ActuationPort(Protocol):
    def store_prescriptions(self, target_date: date | None = None) -> dict[str, Any]:
        ...

    def run_due_prescription_windows(self, actuator_node: str = "irrigation-actuator", limit: int = 100) -> dict[str, Any]:
        ...

    def summary(self) -> dict[str, Any]:
        ...
