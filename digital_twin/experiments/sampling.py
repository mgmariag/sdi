from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.experiments.base import EngineBackedExperiment
from digital_twin.simulation.dto import ExperimentSnapshot


class SamplingIrrigationExperiment(EngineBackedExperiment):
    """Compares full-data irrigation decisions with sparse sampling."""

    engine_runner_name = "run_daily_sampling_experiment"

    def __init__(
        self,
        start_date: date,
        end_date: date,
        sample_interval_days: int = 3,
        sample_interval_hours: int | None = None,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
        baseline_result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(start_date, end_date, persist, snapshot, baseline_result)
        self.sample_interval_days = sample_interval_days
        self.sample_interval_hours = sample_interval_hours

    def engine_parameters(self) -> dict[str, Any]:
        return {
            "sample_interval_days": self.sample_interval_days,
            "sample_interval_hours": self.sample_interval_hours,
        }

