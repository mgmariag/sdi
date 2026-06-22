from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.irrigation_controller.baseline_policy import (
    BaselineIrrigationPolicy,
)
from digital_twin.simulation.irrigation_controller.domain_policy import PotInput
from digital_twin.simulation.sensors.context import with_sensor_key
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.valves.distribution import apply_cold_month_indoor_skip


class BaselineIrrigationStep:
    """Builds a baseline decision with shared simulation enrichments."""

    def __init__(self, policy: BaselineIrrigationPolicy | None = None) -> None:
        self.policy = policy or BaselineIrrigationPolicy()

    def make_decision(
        self,
        state: PotState,
        pot: PotInput,
        weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
        sensor_context: dict[str, Any],
        current_date: date,
    ) -> dict[str, Any]:
        decision = self.policy.make_decision(state, pot, weather, day_profile, slot)
        decision = with_sensor_key(decision, pot, sensor_context)
        return apply_cold_month_indoor_skip(decision, pot, current_date)
