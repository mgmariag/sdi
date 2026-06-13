from __future__ import annotations

import calendar
import logging
import threading
import time
from datetime import date, timedelta
from typing import Any

from digital_twin.application.anfis_training.anfis_training_service import (
    DEFAULT_ANFIS_GENERATIONS,
    DEFAULT_ANFIS_POPULATION,
    AnfisModelService,
)
from digital_twin.application.control_loop.default_control import run_default_dt_control
from digital_twin.application.control_loop.runtime_prescriptions import (
    runtime_prescription_store,
)
from digital_twin.application.control_loop.snapshots import load_experiment_snapshot
from digital_twin.application.experiments.runners import (
    AnfisIrrigationExperiment,
    FuzzyDigitalTwinExperiment,
    SamplingIrrigationExperiment,
)
import digital_twin.application.experiments.cache_keys as cache_keys
import digital_twin.application.experiments.precompute as precompute
from digital_twin.core.cache import SingleFlightCache
from digital_twin.core.config import get_settings
from digital_twin.core.exceptions import ExperimentConfigurationError, InvalidDateRange
from digital_twin.core.time import today_local
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.repositories.anfis_model_repository import (
    DEFAULT_MODEL_KEY,
)
from digital_twin.infrastructure.database.repositories.experiment_repository import (
    ExperimentRunRepository,
)
from digital_twin.simulation.anfis.model import DEFAULT_INPUTS as ANFIS_INPUT_FEATURES
from digital_twin.simulation.anfis.modeling import ANFIS_TRAINING_DATASET_VERSION

logger = logging.getLogger("digital_twin.application.experiments")

DEFAULT_SCENARIO_SEED = 2026
DEFAULT_SAMPLING_INTERVAL_DAYS = 3
DEFAULT_SNAPSHOT_CACHE_TTL_SECONDS = 15 * 60
_experiment_cache = SingleFlightCache()
_snapshot_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
_snapshot_cache_lock = threading.Lock()


def get_default_experiment_range(end: date | None = None) -> tuple[date, date]:
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

    end = end or date.today()
    return _add_months(end, -1), end


def run_default_dt_control_strategy(
    start: date,
    end: date,
    persist: bool = True,
) -> dict[str, Any]:
    cache_key = cache_keys.baseline_cache_key(start, end, persist)
    result = _cached_experiment_result(
        cache_key,
        lambda: _baseline_payload(start, end, persist=persist),
    )
    if persist:
        _store_runtime_prescription("baseline", start, end, result)
        _store_experiment_run("baseline", start, end, {}, result)
    return result


def run_sampling_experiment(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
) -> dict[str, Any]:
    cache_key = cache_keys.sampling_cache_key(start, end, sample_interval_days, sample_interval_hours, True)
    result = _cached_experiment_result(
        cache_key,
        lambda: _sampling_payload(start, end, sample_interval_days, sample_interval_hours, persist=True),
    )
    _store_experiment_run(
        "sampling",
        start,
        end,
        {
            "sample_interval_days": sample_interval_days,
            "sample_interval_hours": sample_interval_hours,
            "effective_sample_interval_hours": sample_interval_hours or sample_interval_days * 24,
        },
        result,
    )
    schedule_related_precompute(
        "sampling",
        start,
        end,
        sample_interval_days=sample_interval_days,
        sample_interval_hours=sample_interval_hours,
    )
    return result


def run_anfis_experiment(
    start: date,
    end: date,
    seed: int | None,
) -> dict[str, Any]:
    cache_key = cache_keys.anfis_cache_key(
        start,
        end,
        seed,
        True,
    )
    result = _cached_experiment_result(
        cache_key,
        lambda: _anfis_payload(
            start,
            end,
            seed,
            persist=True,
        ),
    )
    resolved_model_key = cache_keys.resolve_anfis_model_key(start)
    _store_experiment_run(
        "anfis",
        start,
        end,
        {
            "seed": seed,
            "model_key": resolved_model_key,
            "model_selection_policy": "start_year_auto",
        },
        result,
    )
    schedule_related_precompute(
        "anfis",
        start,
        end,
        seed=seed,
    )
    return result


def run_fuzzy_dt_experiment(
    start: date,
    end: date,
) -> dict[str, Any]:
    cache_key = cache_keys.fuzzy_dt_cache_key(start, end, True)
    result = _cached_experiment_result(
        cache_key,
        lambda: _fuzzy_dt_payload(start, end, persist=True),
    )
    _store_experiment_run("fuzzy_dt", start, end, {}, result)
    schedule_related_precompute("fuzzy_dt", start, end)
    return result


def precompute_experiments(
    start: date | None = None,
    end: date | None = None,
    sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
    sample_interval_hours: int | None = None,
    seed: int | None = DEFAULT_SCENARIO_SEED,
) -> dict[str, Any]:
    if start is None or end is None:
        default_start, default_end = get_default_experiment_range(end)
        start = start or default_start
        end = end or default_end
    precompute_status = schedule_related_precompute(
        "none",
        start,
        end,
        sample_interval_days=sample_interval_days,
        sample_interval_hours=sample_interval_hours,
        seed=seed,
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "worker_processes": _PRECOMPUTE_WORKER_COUNT,
        "precompute": precompute_status,
    }


def get_cached_snapshot(start: date, end: date):
    ttl_seconds = get_settings().experiment_snapshot_cache_ttl_seconds
    cache_key = cache_keys.snapshot_cache_key(start, end)
    now = time.time()
    with _snapshot_cache_lock:
        entry = _snapshot_cache.get(cache_key)
        cache_hit = bool(entry and now - entry["loaded_at_seconds"] <= ttl_seconds)
        if not cache_hit:
            _snapshot_cache[cache_key] = {
                "snapshot": load_experiment_snapshot(start_date=start, end_date=end),
                "loaded_at_seconds": now,
            }
        return _snapshot_cache[cache_key]["snapshot"], cache_hit


def warm_default_baseline_cache() -> dict[str, Any]:
    start, end = get_default_experiment_range()
    cache_key = cache_keys.baseline_cache_key(start, end, True)
    status = precompute.start_precompute_task(
        "baseline",
        cache_key,
        {"experiment": "baseline", "start": start, "end": end, "persist": True},
        cache=_experiment_cache,
        compute_payload=_compute_precompute_payload,
        logger=logger,
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": status,
    }


def _shared_baseline_result(start: date, end: date) -> dict[str, Any]:
    return _cached_experiment_result(
        cache_keys.baseline_cache_key(start, end, True),
        lambda: _baseline_payload(start, end, persist=True),
    )


def _cached_experiment_result(cache_key: tuple[Any, ...], compute) -> dict[str, Any]:
    result, cache_hit = _experiment_cache.get_or_compute(cache_key, compute)
    result.setdefault("summary", {})["cacheHit"] = cache_hit
    return result


def schedule_related_precompute(
    source_experiment: str,
    start: date,
    end: date,
    sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
    sample_interval_hours: int | None = None,
    seed: int | None = DEFAULT_SCENARIO_SEED,
) -> dict[str, list[str]]:
    status: dict[str, list[str]] = {
        "started": [],
        "cached": [],
        "inflight": [],
        "disabled": [],
        "failed": [],
    }
    if not _precompute_enabled():
        status["disabled"].append("all")
        return status

    tasks: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    if source_experiment != "baseline":
        tasks.append(
            (
                "baseline",
                cache_keys.baseline_cache_key(start, end),
                {"experiment": "baseline", "start": start, "end": end, "persist": True},
            )
        )
    if source_experiment != "sampling":
        tasks.append(
            (
                "sampling",
                cache_keys.sampling_cache_key(start, end, sample_interval_days, sample_interval_hours),
                {
                    "experiment": "sampling",
                    "start": start,
                    "end": end,
                    "sample_interval_days": sample_interval_days,
                    "sample_interval_hours": sample_interval_hours,
                    "persist": True,
                },
            )
        )
    if source_experiment != "anfis":
        if _precompute_anfis_enabled():
            tasks.append(
                (
                    "anfis",
                    cache_keys.anfis_cache_key(
                        start,
                        end,
                        seed,
                    ),
                    {
                        "experiment": "anfis",
                        "start": start,
                        "end": end,
                        "seed": seed,
                        "persist": True,
                    },
                )
            )
        else:
            status["disabled"].append("anfis")
    if source_experiment != "fuzzy_dt":
        tasks.append(
            (
                "fuzzy_dt",
                cache_keys.fuzzy_dt_cache_key(start, end),
                {"experiment": "fuzzy_dt", "start": start, "end": end, "persist": True},
            )
        )

    for label, cache_key, task in tasks:
        task_status = precompute.start_precompute_task(
            label,
            cache_key,
            task,
            cache=_experiment_cache,
            compute_payload=_compute_precompute_payload,
            logger=logger,
        )
        status[task_status].append(label)

    return status


def _baseline_payload(start: date, end: date, persist: bool = True) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    result = run_default_dt_control(
        start_date=start,
        end_date=end,
        persist=False,
        snapshot=snapshot,
    )
    result = _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
    if persist:
        _store_runtime_prescription("baseline", start, end, result)
    return result


def _sampling_payload(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
    persist: bool = True,
) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    baseline = _shared_baseline_result(start, end)
    result = SamplingIrrigationExperiment(
        start_date=start,
        end_date=end,
        sample_interval_days=sample_interval_days,
        sample_interval_hours=sample_interval_hours,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
    ).run()
    result = _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
    if persist:
        _store_runtime_prescription("sampling", start, end, result)
    return result


def _anfis_payload(
    start: date,
    end: date,
    seed: int | None,
    persist: bool = True,
) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    baseline = _shared_baseline_result(start, end)
    resolved_model_key = cache_keys.resolve_anfis_model_key(start)
    persisted_model = AnfisModelService().load_latest_model(resolved_model_key)
    if not persisted_model:
        raise ExperimentConfigurationError(
            f"No persisted ANFIS model is available for key '{resolved_model_key}'. Run "
            "`docker compose --profile anfis-training up --build anfis-trainer` first, "
            "or add a trained model for the requested date range."
        )
    if not _persisted_anfis_model_matches(
        persisted_model["metadata"],
        seed,
        strict_training_config=resolved_model_key == DEFAULT_MODEL_KEY,
    ):
        raise ExperimentConfigurationError(
            "The persisted ANFIS model was trained with different ANFIS seed, input-feature, "
            "sample-policy, or default training settings. "
            "Run the ANFIS trainer with matching parameters or use the default endpoint parameters."
        )
    result = AnfisIrrigationExperiment(
        start_date=start,
        end_date=end,
        seed=seed,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
        trained_model=persisted_model["model"],
        training_metadata=persisted_model["metadata"],
    ).run()
    result = _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
    result.setdefault("summary", {})["anfisModelKey"] = resolved_model_key
    result.setdefault("summary", {})["anfisModelSelectionPolicy"] = "start_year_auto"
    result.setdefault("summary", {})["anfisModelSelectionYear"] = start.year
    if persist:
        _store_runtime_prescription("anfis", start, end, result)
    return result


def _persisted_anfis_model_matches(
    metadata: dict[str, Any],
    seed: int | None,
    strict_training_config: bool = True,
) -> bool:
    base_match = (
        metadata.get("training_sample_policy") == "all_available_sensor_readings"
        and int(metadata.get("training_dataset_version") or 0) == ANFIS_TRAINING_DATASET_VERSION
        and metadata.get("seed") == seed
        and list(metadata.get("anfis_input_features") or []) == list(ANFIS_INPUT_FEATURES)
    )
    if not base_match:
        return False
    if not strict_training_config:
        return True
    return (
        int(metadata.get("generations") or 0) == DEFAULT_ANFIS_GENERATIONS
        and int(metadata.get("population") or 0) == DEFAULT_ANFIS_POPULATION
    )



def _fuzzy_dt_payload(start: date, end: date, persist: bool = True) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    baseline = _shared_baseline_result(start, end)
    result = FuzzyDigitalTwinExperiment(
        start_date=start,
        end_date=end,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
    ).run()
    result = _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
    if persist:
        _store_runtime_prescription("fuzzy_dt", start, end, result)
    return result


def _store_runtime_prescription(
    experiment_type: str,
    start: date,
    end: date,
    result: dict[str, Any],
) -> None:
    runtime_prescription_store.upsert_from_result(experiment_type, start, end, result)


def _store_experiment_run(
    experiment_type: str,
    start: date,
    end: date,
    parameters: dict[str, Any],
    result: dict[str, Any],
) -> None:
    try:
        row = ExperimentRunRepository().create(
            experiment_type=experiment_type,
            start_date=start,
            end_date=end,
            parameters=parameters,
            result=result,
        )
    except Exception as exc:
        logger.warning("Failed to persist %s experiment run for %s..%s: %s", experiment_type, start, end, exc)
        return
    result.setdefault("summary", {})["experimentRunId"] = row["id"]
    result["summary"]["experimentRunSavedAt"] = row["created_at"].isoformat()
    result["summary"]["experimentRunStartedAt"] = row["started_at"].isoformat()
    result["summary"]["experimentRunCompletedAt"] = row["completed_at"].isoformat()


def list_experiment_runs(experiment_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    return [_format_experiment_run(row) for row in ExperimentRunRepository().latest(experiment_type=experiment_type, limit=limit)]


def get_experiment_run(run_id: int) -> dict[str, Any] | None:
    row = ExperimentRunRepository().get(run_id)
    return _format_experiment_run(row, include_payload=True) if row else None


def _format_experiment_run(row: dict[str, Any], include_payload: bool = False) -> dict[str, Any]:
    output = {
        "id": row["id"],
        "experimentType": row["experiment_type"],
        "startDate": row["start_date"].isoformat(),
        "endDate": row["end_date"].isoformat(),
        "computedAt": row["computed_at"].isoformat(),
        "startedAt": row["started_at"].isoformat(),
        "completedAt": row["completed_at"].isoformat(),
        "createdAt": row["created_at"].isoformat(),
        "parameters": row.get("parameters") or {},
        "summary": row.get("summary") or {},
    }
    if include_payload:
        output["payload"] = row.get("payload") or {}
    return output


def prepare_tomorrow_prescriptions(target: date | None = None) -> dict[str, Any]:
    target_date = target or (today_local() + timedelta(days=1))
    prepared = {
        "baseline": _baseline_payload(target_date, target_date, persist=True),
        "sampling": _sampling_payload(
            target_date,
            target_date,
            DEFAULT_SAMPLING_INTERVAL_DAYS,
            None,
            persist=True,
        ),
        "anfis": _anfis_payload(
            target_date,
            target_date,
            DEFAULT_SCENARIO_SEED,
            persist=True,
        ),
        "fuzzy_dt": _fuzzy_dt_payload(target_date, target_date, persist=True),
    }
    return {
        "targetDate": target_date.isoformat(),
        "preparedExperiments": list(prepared),
        "runtimePrescriptionCount": len(runtime_prescription_store.latest_for_date(target_date)),
    }


def dispatch_tomorrow_prescriptions(target: date | None = None) -> dict[str, Any]:
    from digital_twin.application.control_loop.runtime import RuntimeControlLoop

    return RuntimeControlLoop().dispatch_next_day_prescriptions(target)


def _annotate_snapshot_cache(result: dict[str, Any], snapshot, cache_hit: bool) -> dict[str, Any]:
    result["summary"]["dbSnapshotCacheHit"] = cache_hit
    result["summary"]["dbSnapshotLoadedAt"] = snapshot.loaded_at.isoformat()
    result["summary"]["dbSnapshotWeatherRows"] = len(snapshot.selected_weather_rows)
    result["summary"]["dbSnapshotSensorRows"] = snapshot.sensor_context.get("row_count", 0)
    result["summary"]["dbSnapshotEstimatedWeatherRows"] = snapshot.estimated_selected_weather_rows
    result["summary"]["dbSnapshotEstimatedWeatherRowsTotal"] = snapshot.estimated_weather_rows
    result["summary"]["dbSnapshotEstimatedLookaheadWeatherRows"] = snapshot.estimated_lookahead_weather_rows
    result["summary"]["dbSnapshotInitialStateRows"] = len(snapshot.initial_pot_states)
    return result



def _compute_precompute_payload(task: dict[str, Any]) -> dict[str, Any]:
    experiment = task["experiment"]
    if experiment == "baseline":
        return _baseline_payload(task["start"], task["end"], persist=task.get("persist", True))
    if experiment == "sampling":
        return _sampling_payload(
            task["start"],
            task["end"],
            task["sample_interval_days"],
            task["sample_interval_hours"],
            persist=task.get("persist", True),
        )
    if experiment == "anfis":
        return _anfis_payload(
            task["start"],
            task["end"],
            task["seed"],
            persist=task.get("persist", True),
        )
    if experiment == "fuzzy_dt":
        return _fuzzy_dt_payload(task["start"], task["end"], persist=task.get("persist", True))
    raise ValueError(f"Unknown precompute experiment: {experiment}")



def _precompute_enabled() -> bool:
    return get_settings().experiment_precompute_related


def _precompute_anfis_enabled() -> bool:
    return get_settings().experiment_precompute_anfis


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

class ExperimentService:
    """Coordinates experiment execution and cache orchestration."""

    def run_default_control(self, start: date | None, end: date | None, persist: bool = True) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return run_default_dt_control_strategy(start=start, end=end, persist=persist)

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
        seed: int | None,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return run_anfis_experiment(
            start=start,
            end=end,
            seed=seed,
        )

    def run_fuzzy_dt(self, start: date | None, end: date | None) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return run_fuzzy_dt_experiment(start=start, end=end)

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
        return precompute_experiments(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
            seed=seed,
        )

    def prepare_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        return prepare_tomorrow_prescriptions(target)

    def dispatch_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        from digital_twin.application.control_loop.runtime import RuntimeControlLoop

        return RuntimeControlLoop(experiment_service=self).dispatch_next_day_prescriptions(target)

    def list_runs(self, experiment_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return list_experiment_runs(experiment_type=experiment_type, limit=limit)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return get_experiment_run(run_id)

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
    "DEFAULT_SCENARIO_SEED",
    "ExperimentConfigurationError",
    "ExperimentService",
    "dispatch_tomorrow_prescriptions",
    "get_experiment_run",
    "list_experiment_runs",
    "prepare_tomorrow_prescriptions",
]




