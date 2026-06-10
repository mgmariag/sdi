from __future__ import annotations

import calendar
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import date, timedelta
import logging
import os
import threading
import time
from typing import Any

from digital_twin.core.cache import SingleFlightCache
from digital_twin.core.config import get_settings
from digital_twin.core.time import today_local
from digital_twin.core.exceptions import ExperimentConfigurationError, InvalidDateRange
from digital_twin.control import run_default_dt_control
from digital_twin.control.prescriptions import runtime_prescription_store
from digital_twin.db.connection import get_connection
from digital_twin.db.repositories.experiment_repository import ExperimentRunRepository
from digital_twin.experiments import (
    load_experiment_snapshot,
    run_daily_anfis_experiment,
    run_daily_fuzzy_dt_experiment,
    run_daily_sampling_experiment,
)
from digital_twin.services.anfis_model_service import AnfisModelService
from digital_twin.services.irrigation_service import IrrigationActuationService


logger = logging.getLogger("digital_twin.experiments")

DEFAULT_SCENARIO_SEED = 2026
DEFAULT_SAMPLING_INTERVAL_DAYS = 3
DEFAULT_SNAPSHOT_CACHE_TTL_SECONDS = 15 * 60

_experiment_cache = SingleFlightCache()
_snapshot_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
_snapshot_cache_lock = threading.Lock()
_precompute_executor: ProcessPoolExecutor | None = None
_precompute_executor_lock = threading.Lock()
_PRECOMPUTE_WORKER_COUNT = max(1, min(2, (os.cpu_count() or 2) // 2))


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
    cache_key = _baseline_cache_key(start, end, persist)
    result = _experiment_cache.get_or_compute(
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
    # Experiment result caching is temporarily disabled.
    # cache_key = _sampling_cache_key(start, end, sample_interval_days, sample_interval_hours, True)
    # result = _experiment_cache.get_or_compute(
    #     cache_key,
    #     lambda: _sampling_payload(start, end, sample_interval_days, sample_interval_hours, persist=True),
    # )
    result = _sampling_payload(start, end, sample_interval_days, sample_interval_hours, persist=True)
    result.setdefault("summary", {})["cacheHit"] = False
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
    # schedule_related_precompute(
    #     "sampling",
    #     start,
    #     end,
    #     sample_interval_days=sample_interval_days,
    #     sample_interval_hours=sample_interval_hours,
    # )
    return result


def run_anfis_experiment(
    start: date,
    end: date,
    seed: int | None,
) -> dict[str, Any]:
    # Experiment result caching is temporarily disabled.
    # cache_key = _anfis_cache_key(
    #     start,
    #     end,
    #     seed,
    #     True,
    # )
    # result = _experiment_cache.get_or_compute(
    #     cache_key,
    #     lambda: _anfis_payload(
    #         start,
    #         end,
    #         seed,
    #         persist=True,
    #     ),
    # )
    result = _anfis_payload(
        start,
        end,
        seed,
        persist=True,
    )
    result.setdefault("summary", {})["cacheHit"] = False
    _store_experiment_run("anfis", start, end, {"seed": seed}, result)
    # schedule_related_precompute(
    #     "anfis",
    #     start,
    #     end,
    #     seed=seed,
    # )
    return result


def run_fuzzy_dt_experiment(
    start: date,
    end: date,
) -> dict[str, Any]:
    # Experiment result caching is temporarily disabled.
    # cache_key = _fuzzy_dt_cache_key(start, end, True)
    # result = _experiment_cache.get_or_compute(
    #     cache_key,
    #     lambda: _fuzzy_dt_payload(start, end, persist=True),
    # )
    result = _fuzzy_dt_payload(start, end, persist=True)
    result.setdefault("summary", {})["cacheHit"] = False
    _store_experiment_run("fuzzy_dt", start, end, {}, result)
    # schedule_related_precompute("fuzzy_dt", start, end)
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
    cache_key = ("db-snapshot-v7-baseline-startup", start, end, _sensor_placement_cache_token())
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
    cache_key = _baseline_cache_key(start, end, True)
    status = _start_precompute_task(
        "baseline",
        cache_key,
        {"experiment": "baseline", "start": start, "end": end, "persist": True},
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": status,
    }


def _shared_baseline_result(start: date, end: date) -> dict[str, Any]:
    return _experiment_cache.get_or_compute(
        _baseline_cache_key(start, end, True),
        lambda: _baseline_payload(start, end, persist=True),
    )


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
    # Experiment result caching is temporarily disabled.
    status["disabled"].append("all")
    return status

    if not _precompute_enabled():
        status["disabled"].append("all")
        return status

    tasks: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    if source_experiment != "baseline":
        tasks.append(
            (
                "baseline",
                _baseline_cache_key(start, end),
                {"experiment": "baseline", "start": start, "end": end, "persist": True},
            )
        )
    if source_experiment != "sampling":
        tasks.append(
            (
                "sampling",
                _sampling_cache_key(start, end, sample_interval_days, sample_interval_hours),
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
                    _anfis_cache_key(
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
                _fuzzy_dt_cache_key(start, end),
                {"experiment": "fuzzy_dt", "start": start, "end": end, "persist": True},
            )
        )

    for label, cache_key, task in tasks:
        task_status = _start_precompute_task(label, cache_key, task)
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
    result = run_daily_sampling_experiment(
        start_date=start,
        end_date=end,
        sample_interval_days=sample_interval_days,
        sample_interval_hours=sample_interval_hours,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
    )
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
    persisted_model = AnfisModelService().load_latest_model()
    if not persisted_model:
        raise ExperimentConfigurationError(
            "No persisted ANFIS model is available. Run "
            "`docker compose --profile anfis-training up --build anfis-trainer` first."
        )
    if not _persisted_anfis_model_matches(
        persisted_model["metadata"],
        seed,
    ):
        raise ExperimentConfigurationError(
            "The persisted ANFIS model was trained with different ANFIS seed or sample-policy settings. "
            "Run the ANFIS trainer with matching parameters or use the default endpoint parameters."
        )
    result = run_daily_anfis_experiment(
        start_date=start,
        end_date=end,
        seed=seed,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
        trained_model=persisted_model["model"],
        training_metadata=persisted_model["metadata"],
    )
    result = _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
    if persist:
        _store_runtime_prescription("anfis", start, end, result)
    return result


def _persisted_anfis_model_matches(
    metadata: dict[str, Any],
    seed: int | None,
) -> bool:
    return metadata.get("training_sample_policy") == "all_available_sensor_readings" and metadata.get("seed") == seed


def _fuzzy_dt_payload(start: date, end: date, persist: bool = True) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    baseline = _shared_baseline_result(start, end)
    result = run_daily_fuzzy_dt_experiment(
        start_date=start,
        end_date=end,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
    )
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
    target_date = target or (today_local() + timedelta(days=1))
    prepared = prepare_tomorrow_prescriptions(target_date)
    dispatch = IrrigationActuationService().store_prescriptions(target_date=target_date)
    return {
        **prepared,
        "dispatch": dispatch,
    }


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


def _start_precompute_task(
    label: str,
    cache_key: tuple[Any, ...],
    task: dict[str, Any],
) -> str:
    event, should_compute = _experiment_cache.reserve(cache_key)
    if event is None:
        return "cached"
    if not should_compute:
        return "inflight"

    logger.info("Precomputing %s experiment cache for %s", label, cache_key)
    try:
        future = _get_precompute_executor().submit(_compute_precompute_payload, task)
    except Exception as exc:
        _experiment_cache.release_failed(cache_key, event)
        logger.warning("Precomputing %s experiment cache could not start: %s", label, exc)
        return "failed"
    future.add_done_callback(lambda completed: _finish_precompute_task(label, cache_key, event, completed))
    return "started"


def _get_precompute_executor() -> ProcessPoolExecutor:
    global _precompute_executor
    with _precompute_executor_lock:
        if _precompute_executor is None:
            _precompute_executor = ProcessPoolExecutor(max_workers=_PRECOMPUTE_WORKER_COUNT)
        return _precompute_executor


def _finish_precompute_task(
    label: str,
    cache_key: tuple[Any, ...],
    event: threading.Event,
    future: Future,
) -> None:
    try:
        result = future.result()
        _experiment_cache.store(cache_key, result, event)
        logger.info("Precomputed %s experiment cache for %s", label, cache_key)
    except Exception as exc:
        _experiment_cache.release_failed(cache_key, event)
        logger.warning("Precomputing %s experiment cache failed: %s", label, exc)


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


def _baseline_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("baseline-db-v17-seasonal-rain-dose-valve-details", start, end, persist, _sensor_placement_cache_token())


def _sampling_cache_key(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    effective_sample_interval_hours = sample_interval_hours or sample_interval_days * 24
    return (
        "sampling-db-sensor-weather-v14-hourly-raw-weather-popover-valve-details",
        start,
        end,
        sample_interval_days,
        effective_sample_interval_hours,
        persist,
        _sensor_placement_cache_token(),
    )


def _anfis_cache_key(
    start: date,
    end: date,
    seed: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    return (
        "anfis-db-size-flow-pots-v17-weighted-zone-calibrated-valve-details",
        start,
        end,
        seed,
        persist,
        _sensor_placement_cache_token(),
    )


def _fuzzy_dt_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("fuzzy-dt-db-v9-hourly-raw-weather-popover-valve-details", start, end, persist, _sensor_placement_cache_token())


def _sensor_placement_cache_token() -> tuple[Any, ...]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                count(*) AS location_count,
                coalesce(max(requested_sensor_count), 0) AS requested_sensor_count,
                coalesce(string_agg(pot_id::text, ',' ORDER BY rank), '') AS pot_ids
            FROM sensor_location_recommendations
            """
        ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0), row[2] or "")


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
        return dispatch_tomorrow_prescriptions(target)

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
