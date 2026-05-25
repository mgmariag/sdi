from __future__ import annotations

from typing import Any


class ExperimentRepository:
    """Placeholder boundary for persisted experiment decisions, events, and alerts."""

    def describe(self) -> dict[str, Any]:
        return {
            "persistence": "irrigation_decisions, irrigation_events, alerts",
            "status": "delegated-to-legacy-experiment-module",
        }

