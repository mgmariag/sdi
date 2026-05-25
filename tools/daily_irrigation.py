"""Compatibility facade for irrigation experiments.

The concrete experiment classes live in ``tools.irrigation``. This module
keeps existing backend imports stable.
"""

from datetime import date
from typing import Any

from tools.irrigation.anfis_experiment import AnfisIrrigationExperiment
from tools.irrigation.baseline_experiment import BaselineIrrigationExperiment
from tools.irrigation.models import ANFIS_DECISION_THRESHOLD, ExperimentSnapshot, PotState
from tools.irrigation.sampling_experiment import SamplingIrrigationExperiment
from tools.irrigation.simulation_engine import load_experiment_snapshot


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


__all__ = [
    "ANFIS_DECISION_THRESHOLD",
    "ExperimentSnapshot",
    "PotState",
    "load_experiment_snapshot",
    "run_daily_anfis_experiment",
    "run_daily_irrigation_experiment",
    "run_daily_sampling_experiment",
]
