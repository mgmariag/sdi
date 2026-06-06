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
from digital_twin.experiments import (
    load_experiment_snapshot,
    run_daily_anfis_experiment,
    run_daily_fuzzy_dt_experiment,
    run_daily_sampling_experiment,
)
from digital_twin.services.irrigation_service import IrrigationActuationService


logger = logging.getLogger("digital_twin.experiments")

DEFAULT_SCENARIO_SEED = 2026
DEFAULT_SAMPLING_INTERVAL_DAYS = 3
DEFAULT_ANFIS_TRAIN_SAMPLES = 2000
DEFAULT_ANFIS_TEST_SAMPLES = 800
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
    train_samples: int,
    test_samples: int,
    seed: int | None,
) -> dict[str, Any]:
    # Experiment result caching is temporarily disabled.
    # cache_key = _anfis_cache_key(
    #     start,
    #     end,
    #     train_samples,
    #     test_samples,
    #     seed,
    #     True,
    # )
    # result = _experiment_cache.get_or_compute(
    #     cache_key,
    #     lambda: _anfis_payload(
    #         start,
    #         end,
    #         train_samples,
    #         test_samples,
    #         seed,
    #         persist=True,
    #     ),
    # )
    result = _anfis_payload(
        start,
        end,
        train_samples,
        test_samples,
        seed,
        persist=True,
    )
    result.setdefault("summary", {})["cacheHit"] = False
    # schedule_related_precompute(
    #     "anfis",
    #     start,
    #     end,
    #     train_samples=train_samples,
    #     test_samples=test_samples,
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
    # schedule_related_precompute("fuzzy_dt", start, end)
    return result


def precompute_experiments(
    start: date | None = None,
    end: date | None = None,
    sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
    sample_interval_hours: int | None = None,
    train_samples: int = DEFAULT_ANFIS_TRAIN_SAMPLES,
    test_samples: int = DEFAULT_ANFIS_TEST_SAMPLES,
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
        train_samples=train_samples,
        test_samples=test_samples,
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
    train_samples: int = DEFAULT_ANFIS_TRAIN_SAMPLES,
    test_samples: int = DEFAULT_ANFIS_TEST_SAMPLES,
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
                        train_samples,
                        test_samples,
                        seed,
                    ),
                    {
                        "experiment": "anfis",
                        "start": start,
                        "end": end,
                        "train_samples": train_samples,
                        "test_samples": test_samples,
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
    train_samples: int,
    test_samples: int,
    seed: int | None,
    persist: bool = True,
) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    baseline = _shared_baseline_result(start, end)
    result = run_daily_anfis_experiment(
        start_date=start,
        end_date=end,
        train_samples=train_samples,
        test_samples=test_samples,
        seed=seed,
        persist=False,
        snapshot=snapshot,
        baseline_result=baseline,
    )
    result = _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
    if persist:
        _store_runtime_prescription("anfis", start, end, result)
    return result


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
            DEFAULT_ANFIS_TRAIN_SAMPLES,
            DEFAULT_ANFIS_TEST_SAMPLES,
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
            task["train_samples"],
            task["test_samples"],
            task["seed"],
            persist=task.get("persist", True),
        )
    if experiment == "fuzzy_dt":
        return _fuzzy_dt_payload(task["start"], task["end"], persist=task.get("persist", True))
    raise ValueError(f"Unknown precompute experiment: {experiment}")


def _baseline_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("baseline-db-v16-seasonal-rain-dose-valve-physical-distribution", start, end, persist, _sensor_placement_cache_token())


def _sampling_cache_key(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    effective_sample_interval_hours = sample_interval_hours or sample_interval_days * 24
    return (
        "sampling-db-sensor-weather-v13-hourly-raw-weather-popover-valves-physical-distribution",
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
    train_samples: int,
    test_samples: int,
    seed: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    return (
        "anfis-db-size-flow-pots-v13-hourly-raw-weather-popover-valves-physical-distribution",
        start,
        end,
        train_samples,
        test_samples,
        seed,
        persist,
        _sensor_placement_cache_token(),
    )


def _fuzzy_dt_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("fuzzy-dt-db-v8-hourly-raw-weather-popover-valves-physical-distribution", start, end, persist, _sensor_placement_cache_token())


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
        train_samples: int,
        test_samples: int,
        seed: int | None,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return run_anfis_experiment(
            start=start,
            end=end,
            train_samples=train_samples,
            test_samples=test_samples,
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
        train_samples: int,
        test_samples: int,
        seed: int | None,
    ) -> dict[str, Any]:
        start, end = self._resolve_range(start, end)
        self._validate_range(start, end)
        return precompute_experiments(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
            train_samples=train_samples,
            test_samples=test_samples,
            seed=seed,
        )

    def prepare_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        return prepare_tomorrow_prescriptions(target)

    def dispatch_tomorrow_prescriptions(self, target: date | None = None) -> dict[str, Any]:
        return dispatch_tomorrow_prescriptions(target)

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
    "prepare_tomorrow_prescriptions",
]
