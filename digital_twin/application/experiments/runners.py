from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

from digital_twin.simulation.shared.types import ExperimentSnapshot


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


class AnfisIrrigationExperiment(EngineBackedExperiment):
    """Runs the ANFIS controller against the irrigation simulation."""

    engine_runner_name = "run_daily_anfis_experiment"

    def __init__(
        self,
        start_date: date,
        end_date: date,
        seed: int | None = 2026,
        generations: int = 35,
        population: int = 24,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
        baseline_result: dict[str, Any] | None = None,
        trained_model: Any | None = None,
        training_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(start_date, end_date, persist, snapshot, baseline_result)
        self.seed = seed
        self.generations = generations
        self.population = population
        self.trained_model = trained_model
        self.training_metadata = training_metadata

    def engine_parameters(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "generations": self.generations,
            "population": self.population,
            "trained_model": self.trained_model,
            "training_metadata": self.training_metadata,
        }


class FuzzyDigitalTwinExperiment(EngineBackedExperiment):
    """Runs the digital-twin fuzzy irrigation prescription controller."""

    engine_runner_name = "run_daily_fuzzy_dt_experiment"
