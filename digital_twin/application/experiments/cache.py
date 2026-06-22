"""Experiment cache primitives."""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import date
from typing import Any, Callable

from digital_twin.application.experiments.experiments import (
    AnfisIrrigationExperiment,
    BaselineExperiment,
    FuzzyDigitalTwinExperiment,
    SamplingIrrigationExperiment,
)
from digital_twin.application.experiments.snapshots import ExperimentSnapshotLoader
from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.simulation.shared.types import ExperimentSnapshot


class SingleFlightCache:
    """Thread-safe cache that deduplicates concurrent work for the same key."""

    def __init__(self) -> None:
        self._values: dict[tuple[Any, ...], Any] = {}
        self._inflight: dict[tuple[Any, ...], threading.Event] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> tuple[Any, bool]:
        event, should_compute = self.reserve(key)
        if event is None:
            return self.get(key), True

        if not should_compute:
            event.wait()
            if self.contains(key):
                return self.get(key), True
            return self.get_or_compute(key, compute)

        try:
            computed = compute()
            self.store(key, computed, event)
        except Exception:
            self.release_failed(key, event)
            raise

        return deepcopy(computed), False

    def reserve(self, key: tuple[Any, ...]) -> tuple[threading.Event | None, bool]:
        with self._lock:
            if key in self._values:
                return None, True
            event = self._inflight.get(key)
            if event is not None:
                return event, False
            event = threading.Event()
            self._inflight[key] = event
            return event, True

    def store(self, key: tuple[Any, ...], value: Any, event: threading.Event) -> None:
        with self._lock:
            self._values[key] = value
            self._inflight.pop(key, None)
            event.set()

    def release_failed(self, key: tuple[Any, ...], event: threading.Event) -> None:
        with self._lock:
            self._inflight.pop(key, None)
            event.set()

    def contains(self, key: tuple[Any, ...]) -> bool:
        with self._lock:
            return key in self._values

    def get(self, key: tuple[Any, ...]) -> Any:
        with self._lock:
            return deepcopy(self._values[key])


class ExperimentCacheKeys:
    """Builds cache keys for experiment payloads and snapshots."""

    def __init__(self, sensor_placement_token_provider: Callable[[], tuple[Any, ...]] | None = None) -> None:
        self.sensor_placement_token_provider = sensor_placement_token_provider or sensor_placement_cache_token

    def baseline(self, start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
        return (BaselineExperiment.cache_version, start, end, persist, self._sensor_placement_token())

    def sampling(
        self,
        start: date,
        end: date,
        sample_interval_days: int,
        sample_interval_hours: int | None,
        persist: bool = True,
    ) -> tuple[Any, ...]:
        effective_sample_interval_hours = sample_interval_hours or sample_interval_days * 24
        return (
            SamplingIrrigationExperiment.cache_version,
            start,
            end,
            sample_interval_days,
            effective_sample_interval_hours,
            persist,
            self._sensor_placement_token(),
        )

    def anfis(
        self,
        start: date,
        end: date,
        seed: int | None,
        persist: bool = True,
    ) -> tuple[Any, ...]:
        return (
            AnfisIrrigationExperiment.cache_version,
            start,
            end,
            seed,
            AnfisIrrigationExperiment.resolve_model_key(start),
            persist,
            self._sensor_placement_token(),
        )

    def fuzzy_dt(self, start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
        return (FuzzyDigitalTwinExperiment.cache_version, start, end, persist, self._sensor_placement_token())

    def snapshot(self, cache_version: str, start: date, end: date) -> tuple[Any, ...]:
        return (cache_version, start, end, self._sensor_placement_token())

    def _sensor_placement_token(self) -> tuple[Any, ...]:
        return self.sensor_placement_token_provider()


class ExperimentSnapshotCache:
    """Caches database-backed experiment snapshots for a short TTL."""

    def __init__(
        self,
        loader: ExperimentSnapshotLoader | None = None,
        cache_keys: ExperimentCacheKeys | None = None,
        settings_provider: Callable[[], Any] = get_settings,
        time_provider: Callable[[], float] = time.time,
    ) -> None:
        self.loader = loader or ExperimentSnapshotLoader()
        self.cache_keys = cache_keys or ExperimentCacheKeys()
        self.settings_provider = settings_provider
        self.time_provider = time_provider
        self._entries: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, start: date, end: date) -> tuple[ExperimentSnapshot, bool]:
        ttl_seconds = self.settings_provider().experiment_snapshot_cache_ttl_seconds
        cache_key = self._cache_key(start, end)
        now = self.time_provider()
        with self._lock:
            entry = self._entries.get(cache_key)
            cache_hit = bool(entry and now - entry["loaded_at_seconds"] <= ttl_seconds)
            if not cache_hit:
                self._entries[cache_key] = {
                    "snapshot": self.loader.load(start, end),
                    "loaded_at_seconds": now,
                }
            return self._entries[cache_key]["snapshot"], cache_hit

    def _cache_key(self, start: date, end: date) -> tuple[Any, ...]:
        return self.cache_keys.snapshot(self.loader.cache_version, start, end)


def sensor_placement_cache_token() -> tuple[Any, ...]:
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
