from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

from digital_twin.application.clock import ApplicationClock
from digital_twin.application.control_loop.prescription import (
    runtime_prescription_store,
)
from digital_twin.application.exceptions import InvalidDateRange
from digital_twin.application.experiments.cache import (
    ExperimentCacheKeys,
    ExperimentSnapshotCache,
    SingleFlightCache,
)
from digital_twin.application.experiments.experiments import (
    AnfisIrrigationExperiment,
    BaselineExperiment,
    ExperimentExecutionContext,
    ExperimentStrategy,
    FuzzyDigitalTwinExperiment,
    SamplingIrrigationExperiment,
)
from digital_twin.application.experiments.precompute import (
    ExperimentPrecomputePayloadBuilder,
    ExperimentPrecomputeService,
)
from digital_twin.application.experiments.run_history import ExperimentRunHistory
from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.connection import get_connection

logger = logging.getLogger("digital_twin.application.experiments")

DEFAULT_SCENARIO_SEED = 2026
DEFAULT_SAMPLING_INTERVAL_DAYS = 3


class ExperimentService:
    """Coordinates experiment execution, caching, precompute, and run history."""

    def __init__(
        self,
        *,
        experiment_cache: SingleFlightCache | None = None,
        snapshot_cache: ExperimentSnapshotCache | None = None,
        cache_keys: ExperimentCacheKeys | None = None,
        run_history: ExperimentRunHistory | None = None,
        precompute_service: ExperimentPrecomputeService | None = None,
        clock: ApplicationClock | None = None,
    ) -> None:
        self.cache_keys = cache_keys or ExperimentCacheKeys()
        self.experiment_cache = experiment_cache or SingleFlightCache()
        self.snapshot_cache = snapshot_cache or ExperimentSnapshotCache(cache_keys=self.cache_keys)
        self.run_history = run_history or ExperimentRunHistory(logger=logger)
        self.clock = clock or ApplicationClock()
        self.precompute_service = precompute_service or ExperimentPrecomputeService(
            cache=self.experiment_cache,
            cache_keys=self.cache_keys,
            compute_payload=ExperimentPrecomputePayloadBuilder(),
            logger=logger,
        )

    def get_default_range(self, end: date | None = None) -> tuple[date, date]:
        if end is None:
            settings = get_settings()
            with get_connection() as conn:
                end = conn.execute(
                    """
                    SELECT max(observed_date)
                    FROM weather_hourly
                    """,
                    {"timezone": settings.local_timezone},
                ).fetchone()[0]

        end = end or self.clock.today()
        return self.clock.add_months(end, -1), end

    def run_default_control(self, start: date | None, end: date | None, persist: bool = True) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return self.run_default_control_for_range(start=start, end=end, persist=persist)

    def run_default_control_for_range(
        self,
        start: date,
        end: date,
        persist: bool = True,
    ) -> dict[str, Any]:
        return self._run_cached_experiment(
            BaselineExperiment(start, end),
            cache_key=self.cache_keys.baseline(start, end, persist),
            persist=persist,
        )

    def run_sampling(
        self,
        start: date | None,
        end: date | None,
        sample_interval_days: int,
        sample_interval_hours: int | None,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return self.run_sampling_for_range(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
        )

    def run_sampling_for_range(
        self,
        start: date,
        end: date,
        sample_interval_days: int,
        sample_interval_hours: int | None,
    ) -> dict[str, Any]:
        experiment = SamplingIrrigationExperiment(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
        )
        return self._run_cached_experiment(
            experiment,
            cache_key=self.cache_keys.sampling(start, end, sample_interval_days, sample_interval_hours, True),
            persist=True,
            history_parameters={
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
                "effective_sample_interval_hours": sample_interval_hours or sample_interval_days * 24,
            },
            precompute_parameters={
                "sample_interval_days": sample_interval_days,
                "sample_interval_hours": sample_interval_hours,
            },
        )

    def run_anfis(self, start: date | None, end: date | None, seed: int | None) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return self.run_anfis_for_range(start=start, end=end, seed=seed)

    def run_anfis_for_range(self, start: date, end: date, seed: int | None) -> dict[str, Any]:
        resolved_model_key = AnfisIrrigationExperiment.resolve_model_key(start)
        return self._run_cached_experiment(
            AnfisIrrigationExperiment(start, end, seed),
            cache_key=self.cache_keys.anfis(start, end, seed, True),
            persist=True,
            history_parameters={
                "seed": seed,
                "model_key": resolved_model_key,
                "model_selection_policy": "start_year_auto",
            },
            precompute_parameters={"seed": seed},
        )

    def run_fuzzy_dt(self, start: date | None, end: date | None) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return self.run_fuzzy_dt_for_range(start=start, end=end)

    def run_fuzzy_dt_for_range(self, start: date, end: date) -> dict[str, Any]:
        return self._run_cached_experiment(
            FuzzyDigitalTwinExperiment(start, end),
            cache_key=self.cache_keys.fuzzy_dt(start, end, True),
            persist=True,
            precompute_parameters={},
        )

    def precompute(
        self,
        start: date | None,
        end: date | None,
        sample_interval_days: int,
        sample_interval_hours: int | None,
        seed: int | None,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return self.precompute_for_range(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
            seed=seed,
        )

    def precompute_for_range(
        self,
        start: date,
        end: date,
        sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
        sample_interval_hours: int | None = None,
        seed: int | None = DEFAULT_SCENARIO_SEED,
    ) -> dict[str, Any]:
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "worker_processes": self.precompute_service.worker_count,
            "precompute": self.schedule_related_precompute(
                "none",
                start,
                end,
                sample_interval_days=sample_interval_days,
                sample_interval_hours=sample_interval_hours,
                seed=seed,
            ),
        }

    def get_cached_snapshot(self, start: date, end: date):
        return self.snapshot_cache.get(start, end)

    def warm_default_baseline_cache(self) -> dict[str, Any]:
        start, end = self.get_default_range()
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": self.precompute_service.warm_baseline(start, end),
        }

    def schedule_related_precompute(
        self,
        source_experiment: str,
        start: date,
        end: date,
        sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
        sample_interval_hours: int | None = None,
        seed: int | None = DEFAULT_SCENARIO_SEED,
    ) -> dict[str, list[str]]:
        return self.precompute_service.schedule(
            source_experiment=source_experiment,
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
            seed=seed,
        )

    def prepare_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        target_date = target or (self.clock.today() + timedelta(days=1))
        prepared = {
            "baseline": self.run_experiment_payload(BaselineExperiment(target_date, target_date), persist=True),
            "sampling": self.run_experiment_payload(
                SamplingIrrigationExperiment(
                    target_date,
                    target_date,
                    DEFAULT_SAMPLING_INTERVAL_DAYS,
                    None,
                ),
                persist=True,
            ),
            "anfis": self.run_experiment_payload(
                AnfisIrrigationExperiment(target_date, target_date, DEFAULT_SCENARIO_SEED),
                persist=True,
            ),
            "fuzzy_dt": self.run_experiment_payload(FuzzyDigitalTwinExperiment(target_date, target_date), persist=True),
        }
        return {
            "targetDate": target_date.isoformat(),
            "preparedExperiments": list(prepared),
            "runtimePrescriptionCount": len(runtime_prescription_store.latest_for_date(target_date)),
        }

    def dispatch_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        from digital_twin.application.control_loop.runtime import RuntimeControlLoop

        return RuntimeControlLoop(experiment_service=self).dispatch_next_day_prescriptions(target)

    def list_runs(self, experiment_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.run_history.list_runs(experiment_type=experiment_type, limit=limit)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self.run_history.get_run(run_id)

    def _experiment_context(self) -> ExperimentExecutionContext:
        return ExperimentExecutionContext(
            snapshot_provider=self.get_cached_snapshot,
            baseline_provider=self._shared_baseline_result,
            runtime_store=runtime_prescription_store,
        )

    def _shared_baseline_result(self, start: date, end: date) -> dict[str, Any]:
        return self._cached_experiment_result(
            self.cache_keys.baseline(start, end, True),
            lambda: self.run_experiment_payload(BaselineExperiment(start, end), persist=True),
        )

    def _run_cached_experiment(
        self,
        experiment: ExperimentStrategy,
        *,
        cache_key: tuple[Any, ...],
        persist: bool,
        history_parameters: dict[str, Any] | None = None,
        precompute_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self._cached_experiment_result(
            cache_key,
            lambda: self.run_experiment_payload(experiment, persist=persist),
        )
        if persist:
            self.run_history.store(
                experiment.experiment_type,
                experiment.start,
                experiment.end,
                history_parameters or {},
                result,
            )
        if precompute_parameters is not None:
            self.schedule_related_precompute(
                experiment.experiment_type,
                experiment.start,
                experiment.end,
                **precompute_parameters,
            )
        return result

    def run_experiment_payload(self, experiment: ExperimentStrategy, persist: bool = True) -> dict[str, Any]:
        return experiment.run(self._experiment_context(), persist=persist)

    def _cached_experiment_result(self, cache_key: tuple[Any, ...], compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        result, cache_hit = self.experiment_cache.get_or_compute(cache_key, compute)
        result.setdefault("summary", {})["cacheHit"] = cache_hit
        return result

    def _resolve_range(self, start: date | None, end: date | None) -> tuple[date, date]:
        default_start, default_end = self.get_default_range(end)
        resolved_end = end or default_end
        return start or default_start, resolved_end

    @staticmethod
    def _validate_range(start: date, end: date) -> None:
        if end < start:
            raise InvalidDateRange("end date must not be before start date")

__all__ = [
    "DEFAULT_SCENARIO_SEED",
    "ExperimentService",
]
