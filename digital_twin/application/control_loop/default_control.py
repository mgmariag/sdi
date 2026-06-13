from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.shared.types import ExperimentSnapshot


class DefaultStrategy:
    """Runs the default DT threshold-based irrigation control logic."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.persist = persist
        self.snapshot = snapshot

    def run(self) -> dict[str, Any]:
        from digital_twin.simulation.engine import run_default_dt_irrigation_control

        return run_default_dt_irrigation_control(
            start_date=self.start_date,
            end_date=self.end_date,
            persist=self.persist,
            snapshot=self.snapshot,
        )


def run_default_dt_control(
    start_date: date,
    end_date: date,
    persist: bool = False,
    snapshot: ExperimentSnapshot | None = None,
) -> dict[str, Any]:
    return DefaultStrategy(start_date, end_date, persist, snapshot).run()
