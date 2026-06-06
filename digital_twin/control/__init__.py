"""Digital twin control strategies."""

from datetime import date
from typing import Any

from digital_twin.control.default_strategy import DefaultStrategy
from digital_twin.control.prescriptions import (
    RuntimeIrrigationPrescription,
    RuntimePrescriptionStore,
    runtime_prescription_store,
)
from digital_twin.simulation.dto import ExperimentSnapshot


def run_default_dt_control(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    return DefaultStrategy(start_date, end_date, persist, snapshot).run()


__all__ = [
    "DefaultStrategy",
    "RuntimeIrrigationPrescription",
    "RuntimePrescriptionStore",
    "run_default_dt_control",
    "runtime_prescription_store",
]
