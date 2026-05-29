"""Experiment classes and convenience runners."""

from datetime import date
from typing import Any

from digital_twin.experiments.anfis import AnfisIrrigationExperiment
from digital_twin.experiments.baseline import BaselineIrrigationExperiment
from digital_twin.experiments.fuzzy_dt import FuzzyDigitalTwinExperiment
from digital_twin.experiments.sampling import SamplingIrrigationExperiment
from digital_twin.simulation.dto import ANFIS_DECISION_THRESHOLD, ExperimentSnapshot, PotState


def load_experiment_snapshot(*args: Any, **kwargs: Any) -> ExperimentSnapshot:
    from digital_twin.simulation.engine import load_experiment_snapshot as _load_experiment_snapshot

    return _load_experiment_snapshot(*args, **kwargs)


def run_daily_irrigation_experiment(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    return BaselineIrrigationExperiment(start_date, end_date, persist, snapshot).run()


def run_daily_sampling_experiment(
    start_date: date,
    end_date: date,
    sample_interval_days: int = 3,
    sample_interval_hours: int | None = None,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    return SamplingIrrigationExperiment(
        start_date,
        end_date,
        sample_interval_days,
        sample_interval_hours,
        persist,
        snapshot,
    ).run()


def run_daily_anfis_experiment(
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
) -> dict[str, Any]:
    return AnfisIrrigationExperiment(
        start_date,
        end_date,
        train_samples,
        test_samples,
        seed,
        generations,
        population,
        parallel_workers,
        parallel_backend,
        persist,
        snapshot,
    ).run()


def run_daily_fuzzy_dt_experiment(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    return FuzzyDigitalTwinExperiment(start_date, end_date, persist, snapshot).run()


__all__ = [
    "ANFIS_DECISION_THRESHOLD",
    "ExperimentSnapshot",
    "PotState",
    "load_experiment_snapshot",
    "run_daily_anfis_experiment",
    "run_daily_fuzzy_dt_experiment",
    "run_daily_irrigation_experiment",
    "run_daily_sampling_experiment",
]

