from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.dto import ExperimentSnapshot


class BaselineIrrigationExperiment:
    """Runs the full-data threshold irrigation simulation."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.persist = persist
        self.snapshot = snapshot

    def run(self) -> dict[str, Any]:
        from digital_twin.simulation.engine import run_daily_irrigation_experiment

        return run_daily_irrigation_experiment(
            start_date=self.start_date,
            end_date=self.end_date,
            persist=self.persist,
            snapshot=self.snapshot,
        )

