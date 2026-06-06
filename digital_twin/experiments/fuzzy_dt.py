from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.experiments.base import EngineBackedExperiment
from digital_twin.simulation.dto import ExperimentSnapshot


class FuzzyDigitalTwinExperiment(EngineBackedExperiment):
    """Runs the digital-twin fuzzy irrigation prescription controller."""

    engine_runner_name = "run_daily_fuzzy_dt_experiment"

    def __init__(
        self,
        start_date: date,
        end_date: date,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
        baseline_result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(start_date, end_date, persist, snapshot, baseline_result)

