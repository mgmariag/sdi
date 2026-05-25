from __future__ import annotations

import calendar
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import date
import logging
import threading
import time
from typing import Any

from backend.cache import SingleFlightCache
from database import get_connection
from digital_twin.core.config import get_settings
from tools.daily_irrigation import (
    load_experiment_snapshot,
    run_daily_anfis_experiment,
    run_daily_irrigation_experiment,
    run_daily_sampling_experiment,
)


logger = logging.getLogger("digital_twin.experiments")

DEFAULT_SCENARIO_SEED = 2026
DEFAULT_ANFIS_PARALLEL_WORKERS = 20
DEFAULT_ANFIS_PARALLEL_BACKEND = "process"
DEFAULT_SAMPLING_INTERVAL_DAYS = 3
DEFAULT_ANFIS_TRAIN_SAMPLES = 500
DEFAULT_ANFIS_TEST_SAMPLES = 200
DEFAULT_SNAPSHOT_CACHE_TTL_SECONDS = 15 * 60

_experiment_cache = SingleFlightCache()
_snapshot_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
_snapshot_cache_lock = threading.Lock()
_precompute_executor: ProcessPoolExecutor | None = None
_precompute_executor_lock = threading.Lock()
_PRECOMPUTE_WORKER_COUNT = 3


def get_default_experiment_range(end: date | None = None) -> tuple[date, date]:
    if end is None:
        settings = get_settings()
        with get_connection() as conn:
            end = conn.execute(
                """
                SELECT max(observed_at AT TIME ZONE %(timezone)s)::date
                FROM weather_hourly
                """,
                {"timezone": settings.local_timezone},
            ).fetchone()[0]

    end = end or date.today()
    return _add_months(end, -1), end


def run_baseline_experiment(
    start: date,
    end: date,
    persist: bool = True,
) -> dict[str, Any]:
    cache_key = _baseline_cache_key(start, end, persist)
    schedule_related_precompute("baseline", start, end)
    result = _experiment_cache.get_or_compute(
        cache_key,
        lambda: _baseline_payload(start, end, persist=persist),
    )
    return result


def run_sampling_experiment(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
) -> dict[str, Any]:
    cache_key = _sampling_cache_key(start, end, sample_interval_days, sample_interval_hours, True)
    schedule_related_precompute(
        "sampling",
        start,
        end,
        sample_interval_days=sample_interval_days,
        sample_interval_hours=sample_interval_hours,
    )
    result = _experiment_cache.get_or_compute(
        cache_key,
        lambda: _sampling_payload(start, end, sample_interval_days, sample_interval_hours, persist=True),
    )
    return result


def run_anfis_experiment(
    start: date,
    end: date,
    train_samples: int,
    test_samples: int,
    seed: int | None,
    parallel_workers: int,
    parallel_backend: str,
) -> dict[str, Any]:
    cache_key = _anfis_cache_key(
        start,
        end,
        train_samples,
        test_samples,
        seed,
        parallel_workers,
        parallel_backend,
        True,
    )
    schedule_related_precompute(
        "anfis",
        start,
        end,
        train_samples=train_samples,
        test_samples=test_samples,
        seed=seed,
        parallel_workers=parallel_workers,
        parallel_backend=parallel_backend,
    )
    result = _experiment_cache.get_or_compute(
        cache_key,
        lambda: _anfis_payload(
            start,
            end,
            train_samples,
            test_samples,
            seed,
            parallel_workers,
            parallel_backend,
            persist=True,
        ),
    )
    return result


def precompute_experiments(
    start: date | None = None,
    end: date | None = None,
    sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
    sample_interval_hours: int | None = None,
    train_samples: int = DEFAULT_ANFIS_TRAIN_SAMPLES,
    test_samples: int = DEFAULT_ANFIS_TEST_SAMPLES,
    seed: int | None = DEFAULT_SCENARIO_SEED,
    parallel_workers: int = DEFAULT_ANFIS_PARALLEL_WORKERS,
    parallel_backend: str = DEFAULT_ANFIS_PARALLEL_BACKEND,
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
        parallel_workers=parallel_workers,
        parallel_backend=parallel_backend,
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "worker_processes": _PRECOMPUTE_WORKER_COUNT,
        "precompute": precompute_status,
    }


def get_cached_snapshot(start: date, end: date):
    ttl_seconds = get_settings().experiment_snapshot_cache_ttl_seconds
    cache_key = ("db-snapshot-v5-active-pots-sensor-placement", start, end, _sensor_placement_cache_token())
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


def schedule_related_precompute(
    source_experiment: str,
    start: date,
    end: date,
    sample_interval_days: int = DEFAULT_SAMPLING_INTERVAL_DAYS,
    sample_interval_hours: int | None = None,
    train_samples: int = DEFAULT_ANFIS_TRAIN_SAMPLES,
    test_samples: int = DEFAULT_ANFIS_TEST_SAMPLES,
    seed: int | None = DEFAULT_SCENARIO_SEED,
    parallel_workers: int = DEFAULT_ANFIS_PARALLEL_WORKERS,
    parallel_backend: str = DEFAULT_ANFIS_PARALLEL_BACKEND,
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
                        parallel_workers,
                        parallel_backend,
                    ),
                    {
                        "experiment": "anfis",
                        "start": start,
                        "end": end,
                        "train_samples": train_samples,
                        "test_samples": test_samples,
                        "seed": seed,
                        "parallel_workers": parallel_workers,
                        "parallel_backend": parallel_backend,
                        "persist": True,
                    },
                )
            )
        else:
            status["disabled"].append("anfis")

    for label, cache_key, task in tasks:
        task_status = _start_precompute_task(label, cache_key, task)
        status[task_status].append(label)

    return status


def _baseline_payload(start: date, end: date, persist: bool = True) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    result = run_daily_irrigation_experiment(
        start_date=start,
        end_date=end,
        persist=persist,
        snapshot=snapshot,
    )
    return _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)


def _sampling_payload(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
    persist: bool = True,
) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    result = run_daily_sampling_experiment(
        start_date=start,
        end_date=end,
        sample_interval_days=sample_interval_days,
        sample_interval_hours=sample_interval_hours,
        persist=persist,
        snapshot=snapshot,
    )
    return _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)


def _anfis_payload(
    start: date,
    end: date,
    train_samples: int,
    test_samples: int,
    seed: int | None,
    parallel_workers: int,
    parallel_backend: str,
    persist: bool = True,
) -> dict[str, Any]:
    snapshot, snapshot_cache_hit = get_cached_snapshot(start, end)
    result = run_daily_anfis_experiment(
        start_date=start,
        end_date=end,
        train_samples=train_samples,
        test_samples=test_samples,
        seed=seed,
        parallel_workers=parallel_workers,
        parallel_backend=parallel_backend,
        persist=persist,
        snapshot=snapshot,
    )
    return _annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)


def _annotate_snapshot_cache(result: dict[str, Any], snapshot, cache_hit: bool) -> dict[str, Any]:
    result["summary"]["dbSnapshotCacheHit"] = cache_hit
    result["summary"]["dbSnapshotLoadedAt"] = snapshot.loaded_at.isoformat()
    result["summary"]["dbSnapshotWeatherRows"] = len(snapshot.selected_weather_rows)
    result["summary"]["dbSnapshotSensorRows"] = snapshot.sensor_context.get("row_count", 0)
    result["summary"]["dbSnapshotEstimatedWeatherRows"] = snapshot.estimated_weather_rows
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
            task["parallel_workers"],
            task["parallel_backend"],
            persist=task.get("persist", True),
        )
    raise ValueError(f"Unknown precompute experiment: {experiment}")


def _baseline_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("baseline-db-v8-active-pots-sensor-placement", start, end, persist, _sensor_placement_cache_token())


def _sampling_cache_key(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    effective_sample_interval_hours = sample_interval_hours or sample_interval_days * 24
    return (
        "sampling-db-sensor-weather-v6-active-pots",
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
    parallel_workers: int,
    parallel_backend: str,
    persist: bool = True,
) -> tuple[Any, ...]:
    return (
        "anfis-db-size-flow-pots-v4-active-pots",
        start,
        end,
        train_samples,
        test_samples,
        seed,
        parallel_workers,
        parallel_backend,
        persist,
        _sensor_placement_cache_token(),
    )


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
