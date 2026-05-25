from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any, Callable


class SingleFlightCache:
    """Thread-safe cache that deduplicates concurrent work for the same key."""

    def __init__(self) -> None:
        self._values: dict[tuple[Any, ...], Any] = {}
        self._inflight: dict[tuple[Any, ...], threading.Event] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: tuple[Any, ...], compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        event, should_compute = self.reserve(key)
        if event is None:
            result = self.get(key)
            result["summary"]["cacheHit"] = True
            return result

        if not should_compute:
            event.wait()
            if self.contains(key):
                result = self.get(key)
                result["summary"]["cacheHit"] = True
                return result
            return self.get_or_compute(key, compute)

        try:
            computed = compute()
            self.store(key, computed, event)
        except Exception:
            self.release_failed(key, event)
            raise

        result = deepcopy(computed)
        result["summary"]["cacheHit"] = False
        return result

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

    def store(self, key: tuple[Any, ...], value: dict[str, Any], event: threading.Event) -> None:
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

    def get(self, key: tuple[Any, ...]) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._values[key])

