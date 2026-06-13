from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class StrategyDecisionPort(Protocol):
    def run_default_control(self, start: date | None, end: date | None, persist: bool = True) -> dict[str, Any]:
        ...

    def run_sampling(
        self,
        start: date | None,
        end: date | None,
        sample_interval_days: int,
        sample_interval_hours: int | None,
    ) -> dict[str, Any]:
        ...

    def run_anfis(self, start: date | None, end: date | None, seed: int | None) -> dict[str, Any]:
        ...

    def run_fuzzy_dt(self, start: date | None, end: date | None) -> dict[str, Any]:
        ...


class DecisionStage:
    """Runs irrigation strategy decisions through the configured experiment service."""

    def __init__(self, experiment_service: StrategyDecisionPort) -> None:
        self.experiment_service = experiment_service

    def baseline(self, start: date | None, end: date | None, persist: bool = True) -> dict[str, Any]:
        return self.experiment_service.run_default_control(start=start, end=end, persist=persist)

    def sampling(
        self,
        start: date | None,
        end: date | None,
        sample_interval_days: int,
        sample_interval_hours: int | None,
    ) -> dict[str, Any]:
        return self.experiment_service.run_sampling(
            start=start,
            end=end,
            sample_interval_days=sample_interval_days,
            sample_interval_hours=sample_interval_hours,
        )

    def anfis(self, start: date | None, end: date | None, seed: int | None) -> dict[str, Any]:
        return self.experiment_service.run_anfis(start=start, end=end, seed=seed)

    def fuzzy_dt(self, start: date | None, end: date | None) -> dict[str, Any]:
        return self.experiment_service.run_fuzzy_dt(start=start, end=end)
