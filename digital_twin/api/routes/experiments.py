from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from digital_twin.api.errors import http_error
from digital_twin.services.experiment_service import (
    DEFAULT_SCENARIO_SEED,
    ExperimentService,
)
from digital_twin.services.irrigation_service import IrrigationActuationService


router = APIRouter(prefix="/api/experiment")
service = ExperimentService()
actuation_service = IrrigationActuationService()
MAX_SAMPLING_INTERVAL_HOURS = 14 * 24
MAX_SAMPLING_INTERVAL_DAYS = 14


@router.get("")
def run_dt_experiment(
    start: date | None = Query(None),
    end: date | None = Query(None),
    persist: bool = Query(True),
):
    try:
        return service.run_default_control(start=start, end=end, persist=persist)
    except Exception as exc:
        raise http_error(exc, 500, "Default strategy failed") from exc


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
    seed: int | None = Query(DEFAULT_SCENARIO_SEED),
):
    try:
        return service.run_anfis(
            start=start,
            end=end,
            seed=seed,
        )
    except Exception as exc:
        raise http_error(exc, 500, "ANFIS experiment failed") from exc


@router.get("/fuzzy")
def run_dt_fuzzy_dt_experiment(
    start: date | None = Query(None),
    end: date | None = Query(None),
):
    try:
        return service.run_fuzzy_dt(start=start, end=end)
    except Exception as exc:
        raise http_error(exc, 500, "Fuzzy DT experiment failed") from exc


@router.get("/runs")
def list_experiment_runs(
    experiment_type: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        return {"runs": service.list_runs(experiment_type=experiment_type, limit=limit)}
    except Exception as exc:
        raise http_error(exc, 500, "Experiment run listing failed") from exc


@router.get("/runs/{run_id}")
def get_experiment_run(run_id: int):
    try:
        run = service.get_run(run_id)
    except Exception as exc:
        raise http_error(exc, 500, "Experiment run lookup failed") from exc
    if run is None:
        raise HTTPException(status_code=404, detail=f"Experiment run {run_id} was not found")
    return run


@router.post("/precompute")
def precompute_dt_experiments(
    start: date | None = Query(None),
    end: date | None = Query(None),
    sample_interval_days: int = Query(3, ge=1, le=MAX_SAMPLING_INTERVAL_DAYS),
    sample_interval_hours: int | None = Query(None, ge=1, le=MAX_SAMPLING_INTERVAL_HOURS),
    seed: int | None = Query(DEFAULT_SCENARIO_SEED),
):
    try:
        return service.precompute(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
            seed=seed,
        )
    except Exception as exc:
        raise http_error(exc, 500, "Experiment precompute failed") from exc


@router.post("/prescriptions/prepare")
def prepare_tomorrow_prescriptions(target: date | None = Query(None)):
    try:
        return service.prepare_tomorrow_prescriptions(target=target)
    except Exception as exc:
        raise http_error(exc, 500, "Prescription preparation failed") from exc


@router.post("/prescriptions/dispatch")
def dispatch_tomorrow_prescriptions(target: date | None = Query(None)):
    try:
        return service.dispatch_tomorrow_prescriptions(target=target)
    except Exception as exc:
        raise http_error(exc, 500, "Prescription dispatch failed") from exc


@router.post("/actuations/run-due")
def run_due_actuations():
    try:
        return actuation_service.run_due_prescription_windows()
    except Exception as exc:
        raise http_error(exc, 500, "Actuator consumption failed") from exc


@router.get("/actuations/summary")
def actuation_summary():
    try:
        return actuation_service.summary()
    except Exception as exc:
        raise http_error(exc, 500, "Actuation summary failed") from exc
