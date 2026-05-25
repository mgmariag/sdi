from __future__ import annotations

from datetime import date
from typing import Any

from tools.irrigation.models import ExperimentSnapshot


class AnfisIrrigationExperiment:
    """Runs the ANFIS-GA controller against the baseline simulation."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        train_samples: int = 2000,
        test_samples: int = 800,
        seed: int | None = 2026,
        generations: int = 35,
        population: int = 24,
        parallel_workers: int | None = None,
        parallel_backend: str = "process",
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.train_samples = train_samples
        self.test_samples = test_samples
        self.seed = seed
        self.generations = generations
        self.population = population
        self.parallel_workers = parallel_workers
        self.parallel_backend = parallel_backend
        self.persist = persist
        self.snapshot = snapshot

    def run(self) -> dict[str, Any]:
        from tools.irrigation.simulation_engine import run_daily_anfis_experiment

        return run_daily_anfis_experiment(
            start_date=self.start_date,
            end_date=self.end_date,
            train_samples=self.train_samples,
            test_samples=self.test_samples,
            seed=self.seed,
            generations=self.generations,
            population=self.population,
            parallel_workers=self.parallel_workers,
            parallel_backend=self.parallel_backend,
            persist=self.persist,
            snapshot=self.snapshot,
        )
