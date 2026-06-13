from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any, Callable

from digital_twin.core.cache import SingleFlightCache

_precompute_executor: ProcessPoolExecutor | None = None
_precompute_executor_lock = threading.Lock()
_PRECOMPUTE_WORKER_COUNT = max(1, min(2, (os.cpu_count() or 2) // 2))


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
