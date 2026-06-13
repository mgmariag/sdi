from __future__ import annotations

from typing import Any

from digital_twin.simulation.irrigation_controller.defaults import DEFAULT_FUZZY_POLICY
from digital_twin.simulation.shared.types import PotState


def make_fuzzy_dt_decision(
    state: PotState,
    pot: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    slot: str = "morning",
) -> dict[str, Any]:
    return DEFAULT_FUZZY_POLICY.make_decision(state, pot, weather, day_profile, slot)
