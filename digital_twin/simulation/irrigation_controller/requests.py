from __future__ import annotations

from typing import Any

from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_FUZZY_POLICY,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.delivery import apply_event_delivery
from digital_twin.simulation.shared.types import PotState


def _fuzzy_prescribed_request(pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_FUZZY_POLICY.irrigation_request(pot, weather, decision)


def _baseline_irrigation_request(pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_IRRIGATION_POLICY.irrigation_request(pot, weather, decision)


def apply_baseline_irrigation_event(state: PotState, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    event = _baseline_irrigation_request(pot, weather, decision)
    return apply_event_delivery(state, pot, event, event["requested_volume_ml"], event.get("duration_min"))
