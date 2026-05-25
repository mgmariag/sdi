from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from digital_twin.api.errors import http_error
from digital_twin.services.experiment_service import (
    DEFAULT_ANFIS_PARALLEL_BACKEND,
    DEFAULT_ANFIS_PARALLEL_WORKERS,
    DEFAULT_SCENARIO_SEED,
    ExperimentService,
)


router = APIRouter(prefix="/api/experiment")
service = ExperimentService()
MAX_SAMPLING_INTERVAL_HOURS = 14 * 24
MAX_SAMPLING_INTERVAL_DAYS = 14


@router.get("")
def run_dt_experiment(
    start: date | None = Query(None),
    end: date | None = Query(None),
    persist: bool = Query(True),
):
    try:
        return service.run_baseline(start=start, end=end, persist=persist)
    except Exception as exc:
        raise http_error(exc, 500, "Baseline experiment failed") from exc


@router.get("/sampling")
def run_dt_sampling_experiment(
    start: date | None = Query(None),
    end: date | None = Query(None),
    sample_interval_days: int = Query(3, ge=1, le=MAX_SAMPLING_INTERVAL_DAYS),
    sample_interval_hours: int | None = Query(None, ge=1, le=MAX_SAMPLING_INTERVAL_HOURS),
):
    try:
        return service.run_sampling(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
        )
    except Exception as exc:
        raise http_error(exc, 500, "Sampling experiment failed") from exc


@router.get("/anfis")
def run_dt_anfis_experiment(
    start: date | None = Query(None),
    end: date | None = Query(None),
    train_samples: int = Query(500, ge=100, le=2000),
    test_samples: int = Query(200, ge=50, le=1000),
    seed: int | None = Query(DEFAULT_SCENARIO_SEED),
    parallel_workers: int = Query(DEFAULT_ANFIS_PARALLEL_WORKERS, ge=1, le=32),
    parallel_backend: str = Query(DEFAULT_ANFIS_PARALLEL_BACKEND),
):
    try:
        return service.run_anfis(
            start=start,
            end=end,
            train_samples=train_samples,
            test_samples=test_samples,
            seed=seed,
            parallel_workers=parallel_workers,
            parallel_backend=parallel_backend,
        )
    except Exception as exc:
        raise http_error(exc, 500, "ANFIS experiment failed") from exc


@router.post("/precompute")
def precompute_dt_experiments(
    start: date | None = Query(None),
    end: date | None = Query(None),
    sample_interval_days: int = Query(3, ge=1, le=MAX_SAMPLING_INTERVAL_DAYS),
    sample_interval_hours: int | None = Query(None, ge=1, le=MAX_SAMPLING_INTERVAL_HOURS),
    train_samples: int = Query(500, ge=100, le=2000),
    test_samples: int = Query(200, ge=50, le=1000),
    seed: int | None = Query(DEFAULT_SCENARIO_SEED),
    parallel_workers: int = Query(DEFAULT_ANFIS_PARALLEL_WORKERS, ge=1, le=32),
    parallel_backend: str = Query(DEFAULT_ANFIS_PARALLEL_BACKEND),
):
    try:
        return service.precompute(
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
    except Exception as exc:
        raise http_error(exc, 500, "Experiment precompute failed") from exc
