from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import date
from typing import Any, Callable

from digital_twin.application.experiments.cache import (
    ExperimentCacheKeys,
    SingleFlightCache,
)
from digital_twin.application.experiments.experiments import (
    AnfisIrrigationExperiment,
    BaselineExperiment,
    FuzzyDigitalTwinExperiment,
    SamplingIrrigationExperiment,
)
from digital_twin.infrastructure.config import get_settings

_precompute_executor: ProcessPoolExecutor | None = None
_precompute_executor_lock = threading.Lock()
_PRECOMPUTE_WORKER_COUNT = max(1, min(2, (os.cpu_count() or 2) // 2))


class ExperimentPrecomputeService:
    """Decides and schedules related experiment cache precomputations."""

    def __init__(
        self,
        cache: SingleFlightCache,
        compute_payload: Callable[[dict[str, Any]], dict[str, Any]],
        cache_keys: ExperimentCacheKeys | None = None,
        settings_provider: Callable[[], Any] = get_settings,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cache = cache
        self.cache_keys = cache_keys or ExperimentCacheKeys()
        self.compute_payload = compute_payload
        self.settings_provider = settings_provider
        self.logger = logger or logging.getLogger("digital_twin.application.experiments")

    @property
    def worker_count(self) -> int:
        return _PRECOMPUTE_WORKER_COUNT

    def warm_baseline(self, start: date, end: date) -> str:
        return start_precompute_task(
            "baseline",
            self.cache_keys.baseline(start, end, True),
            {"experiment": "baseline", "start": start, "end": end, "persist": True},
            cache=self.cache,
            compute_payload=self.compute_payload,
            logger=self.logger,
        )

    def schedule(
        self,
        source_experiment: str,
        start: date,
        end: date,
        sample_interval_days: int,
        sample_interval_hours: int | None,
        seed: int | None,
    ) -> dict[str, list[str]]:
        status: dict[str, list[str]] = {
            "started": [],
            "cached": [],
            "inflight": [],
            "disabled": [],
            "failed": [],
        }
        if not self.settings_provider().experiment_precompute_related:
            status["disabled"].append("all")
            return status

        tasks = self._related_tasks(
            source_experiment,
            start,
            end,
            sample_interval_days,
            sample_interval_hours,
            seed,
            status,
        )
        for label, cache_key, task in tasks:
            task_status = start_precompute_task(
                label,
                cache_key,
                task,
                cache=self.cache,
                compute_payload=self.compute_payload,
                logger=self.logger,
            )
            status[task_status].append(label)
        return status

    def _related_tasks(
        self,
        source_experiment: str,
        start: date,
        end: date,
        sample_interval_days: int,
        sample_interval_hours: int | None,
        seed: int | None,
        status: dict[str, list[str]],
    ) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        tasks: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        if source_experiment != "baseline":
            tasks.append(
                (
                    "baseline",
                    self.cache_keys.baseline(start, end),
                    {"experiment": "baseline", "start": start, "end": end, "persist": True},
                )
            )
        if source_experiment != "sampling":
            tasks.append(
                (
                    "sampling",
                    self.cache_keys.sampling(start, end, sample_interval_days, sample_interval_hours),
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
            if self.settings_provider().experiment_precompute_anfis:
                tasks.append(
                    (
                        "anfis",
                        self.cache_keys.anfis(start, end, seed),
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
                    self.cache_keys.fuzzy_dt(start, end),
                    {"experiment": "fuzzy_dt", "start": start, "end": end, "persist": True},
                )
            )
        return tasks


class ExperimentPrecomputePayloadBuilder:
    """Builds experiment payloads inside precompute worker processes."""

    def __call__(self, task: dict[str, Any]) -> dict[str, Any]:
        from digital_twin.application.experiments.service import ExperimentService

        service = ExperimentService()
        experiment = task["experiment"]
        if experiment == "baseline":
            return service.run_experiment_payload(
                BaselineExperiment(task["start"], task["end"]),
                persist=task.get("persist", True),
            )
        if experiment == "sampling":
            return service.run_experiment_payload(
                SamplingIrrigationExperiment(
                    task["start"],
                    task["end"],
                    task["sample_interval_days"],
                    task["sample_interval_hours"],
                ),
                persist=task.get("persist", True),
            )
        if experiment == "anfis":
            return service.run_experiment_payload(
                AnfisIrrigationExperiment(task["start"], task["end"], task["seed"]),
                persist=task.get("persist", True),
            )
        if experiment == "fuzzy_dt":
            return service.run_experiment_payload(
                FuzzyDigitalTwinExperiment(task["start"], task["end"]),
                persist=task.get("persist", True),
            )
        raise ValueError(f"Unknown precompute experiment: {experiment}")


def worker_count() -> int:
    return _PRECOMPUTE_WORKER_COUNT


def start_precompute_task(
    label: str,
    cache_key: tuple[Any, ...],
    task: dict[str, Any],
    *,
    cache: SingleFlightCache,
    compute_payload: Callable[[dict[str, Any]], dict[str, Any]],
    logger,
) -> str:
    event, should_compute = cache.reserve(cache_key)
    if event is None:
        return "cached"
    if not should_compute:
        return "inflight"

    logger.info("Precomputing %s experiment cache for %s", label, cache_key)
    try:
        future = _get_precompute_executor().submit(compute_payload, task)
    except Exception as exc:
        cache.release_failed(cache_key, event)
        logger.warning("Precomputing %s experiment cache could not start: %s", label, exc)
        return "failed"
    future.add_done_callback(lambda completed: _finish_precompute_task(label, cache_key, event, completed, cache, logger))
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
    cache: SingleFlightCache,
    logger,
) -> None:
    try:
        result = future.result()
        cache.store(cache_key, result, event)
        logger.info("Precomputed %s experiment cache for %s", label, cache_key)
    except Exception as exc:
        cache.release_failed(cache_key, event)
        logger.warning("Precomputing %s experiment cache failed: %s", label, exc)
