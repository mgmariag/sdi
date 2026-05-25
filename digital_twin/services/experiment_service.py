from __future__ import annotations

from datetime import date
from typing import Any

from backend.experiment_service import (
    DEFAULT_ANFIS_PARALLEL_BACKEND,
    DEFAULT_ANFIS_PARALLEL_WORKERS,
    DEFAULT_SCENARIO_SEED,
    get_default_experiment_range,
    precompute_experiments,
    run_anfis_experiment,
    run_baseline_experiment,
    run_sampling_experiment,
)
from digital_twin.core.exceptions import ExperimentConfigurationError, InvalidDateRange


class ExperimentService:
    """Coordinates experiment execution and cache orchestration."""

    def run_baseline(self, start: date | None, end: date | None, persist: bool = True) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return run_baseline_experiment(start=start, end=end, persist=persist)

    def run_sampling(
        self,
        start: date | None,
        end: date | None,
        sample_interval_days: int,
        sample_interval_hours: int | None,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return run_sampling_experiment(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
        )

    def run_anfis(
        self,
        start: date | None,
        end: date | None,
        train_samples: int,
        test_samples: int,
        seed: int | None,
        parallel_workers: int = DEFAULT_ANFIS_PARALLEL_WORKERS,
        parallel_backend: str = DEFAULT_ANFIS_PARALLEL_BACKEND,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        if parallel_backend not in {"process", "thread"}:
            parallel_backend = DEFAULT_ANFIS_PARALLEL_BACKEND
        return run_anfis_experiment(
            start=start,
            end=end,
            train_samples=train_samples,
            test_samples=test_samples,
            seed=seed,
            parallel_workers=parallel_workers,
            parallel_backend=parallel_backend,
        )

    def precompute(
        self,
        start: date | None,
        end: date | None,
        sample_interval_days: int,
        sample_interval_hours: int | None,
        train_samples: int,
        test_samples: int,
        seed: int | None,
        parallel_workers: int = DEFAULT_ANFIS_PARALLEL_WORKERS,
        parallel_backend: str = DEFAULT_ANFIS_PARALLEL_BACKEND,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        if parallel_backend not in {"process", "thread"}:
            parallel_backend = DEFAULT_ANFIS_PARALLEL_BACKEND
        return precompute_experiments(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
            train_samples=train_samples,
            test_samples=test_samples,
            seed=seed,
            parallel_workers=parallel_workers,
            parallel_backend=parallel_backend,
        )

    @staticmethod
    def _validate_range(start: date, end: date) -> None:
        if end < start:
            raise InvalidDateRange("end date must not be before start date")

    @staticmethod
    def _resolve_range(start: date | None, end: date | None) -> tuple[date, date]:
        default_start, default_end = get_default_experiment_range(end)
        resolved_end = end or default_end
        return start or default_start, resolved_end


__all__ = [
    "DEFAULT_ANFIS_PARALLEL_BACKEND",
    "DEFAULT_ANFIS_PARALLEL_WORKERS",
    "DEFAULT_SCENARIO_SEED",
    "ExperimentConfigurationError",
    "ExperimentService",
]
