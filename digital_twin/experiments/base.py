from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

from digital_twin.simulation.dto import ExperimentSnapshot


class EngineBackedExperiment:
    """Shared runner for experiments implemented by the simulation engine."""

    engine_runner_name: ClassVar[str]

    def __init__(
        self,
        start_date: date,
        end_date: date,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
        baseline_result: dict[str, Any] | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.persist = persist
        self.snapshot = snapshot
        self.baseline_result = baseline_result

    def engine_parameters(self) -> dict[str, Any]:
        return {}

    def run(self) -> dict[str, Any]:
        from digital_twin.simulation import engine

        runner = getattr(engine, self.engine_runner_name)
        return runner(
            start_date=self.start_date,
            end_date=self.end_date,
            persist=self.persist,
            snapshot=self.snapshot,
            baseline_result=self.baseline_result,
            **self.engine_parameters(),
        )
