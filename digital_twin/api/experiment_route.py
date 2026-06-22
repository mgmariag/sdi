from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from digital_twin.api.errors import ApiErrorMapper
from digital_twin.application.experiments.service import (
    DEFAULT_SCENARIO_SEED,
    ExperimentService,
)

MAX_SAMPLING_INTERVAL_HOURS = 14 * 24
MAX_SAMPLING_INTERVAL_DAYS = 14


class ExperimentRoutes:
    def __init__(
        self,
        service: ExperimentService | None = None,
        error_mapper: ApiErrorMapper | None = None,
    ) -> None:
        self.router = APIRouter(prefix="/api/experiment")
        self.service = service or ExperimentService()
        self.error_mapper = error_mapper or ApiErrorMapper()
        self.router.add_api_route("", self.run_default_control, methods=["GET"])
        self.router.add_api_route("/sampling", self.run_sampling, methods=["GET"])
        self.router.add_api_route("/anfis", self.run_anfis, methods=["GET"])
        self.router.add_api_route("/fuzzy", self.run_fuzzy_dt, methods=["GET"])
        self.router.add_api_route("/runs", self.list_runs, methods=["GET"])
        self.router.add_api_route("/runs/{run_id}", self.get_run, methods=["GET"])
        self.router.add_api_route("/precompute", self.precompute, methods=["POST"])

    def run_default_control(
        self,
        start: date | None = Query(None),
        end: date | None = Query(None),
        persist: bool = Query(True),
    ):
        try:
            return self.service.run_default_control(start=start, end=end, persist=persist)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Default strategy failed") from exc

    def run_sampling(
        self,
        start: date | None = Query(None),
        end: date | None = Query(None),
        sample_interval_days: int = Query(3, ge=1, le=MAX_SAMPLING_INTERVAL_DAYS),
        sample_interval_hours: int | None = Query(None, ge=1, le=MAX_SAMPLING_INTERVAL_HOURS),
    ):
        try:
            return self.service.run_sampling(
                start=start,
                end=end,
                sample_interval_days=sample_interval_days,
                sample_interval_hours=sample_interval_hours,
            )
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sampling experiment failed") from exc

    def run_anfis(
        self,
        start: date | None = Query(None),
        end: date | None = Query(None),
        seed: int | None = Query(DEFAULT_SCENARIO_SEED),
    ):
        try:
            return self.service.run_anfis(
                start=start,
                end=end,
                seed=seed,
            )
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "ANFIS experiment failed") from exc

    def run_fuzzy_dt(
        self,
        start: date | None = Query(None),
        end: date | None = Query(None),
    ):
        try:
            return self.service.run_fuzzy_dt(start=start, end=end)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Fuzzy DT experiment failed") from exc

    def list_runs(
        self,
        experiment_type: str | None = Query(None),
        limit: int = Query(20, ge=1, le=100),
    ):
        try:
            return {"runs": self.service.list_runs(experiment_type=experiment_type, limit=limit)}
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Experiment run listing failed") from exc

    def get_run(self, run_id: int):
        try:
            run = self.service.get_run(run_id)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Experiment run lookup failed") from exc
        if run is None:
            raise HTTPException(status_code=404, detail=f"Experiment run {run_id} was not found")
        return run

    def precompute(
        self,
        start: date | None = Query(None),
        end: date | None = Query(None),
        sample_interval_days: int = Query(3, ge=1, le=MAX_SAMPLING_INTERVAL_DAYS),
        sample_interval_hours: int | None = Query(None, ge=1, le=MAX_SAMPLING_INTERVAL_HOURS),
        seed: int | None = Query(DEFAULT_SCENARIO_SEED),
    ):
        try:
            return self.service.precompute(
                start=start,
                end=end,
                sample_interval_days=sample_interval_days,
                sample_interval_hours=sample_interval_hours,
                seed=seed,
            )
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Experiment precompute failed") from exc

