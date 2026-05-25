from __future__ import annotations

from datetime import date
from typing import Any

from tools.irrigation.models import ExperimentSnapshot


class SamplingIrrigationExperiment:
    """Compares full-data irrigation decisions with sparse sampling."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        sample_interval_days: int = 3,
        sample_interval_hours: int | None = None,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.sample_interval_days = sample_interval_days
        self.sample_interval_hours = sample_interval_hours
        self.persist = persist
        self.snapshot = snapshot

    def run(self) -> dict[str, Any]:
        from tools.irrigation.simulation_engine import run_daily_sampling_experiment

        return run_daily_sampling_experiment(
            start_date=self.start_date,
            end_date=self.end_date,
            sample_interval_days=self.sample_interval_days,
            sample_interval_hours=self.sample_interval_hours,
            persist=self.persist,
            snapshot=self.snapshot,
        )
