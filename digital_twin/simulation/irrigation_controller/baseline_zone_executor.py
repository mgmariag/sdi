from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.irrigation_controller.request_builder import (
    IrrigationRequestBuilder,
)
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.valves.distribution import (
    baseline_zone_dose_factor,
    execute_valve_zone_distribution,
    trigger_pot_codes,
    trigger_pot_ids,
    trigger_sensor_ids,
    zone_execution_decision_map,
)


class BaselineValveZoneExecutor:
    """Executes baseline decisions as simulated valve-zone delivery events."""

    def __init__(self, request_builder: IrrigationRequestBuilder | None = None) -> None:
        self.request_builder = request_builder or IrrigationRequestBuilder()

    def execute(
        self,
        pot_states: dict[int, PotState],
        zone_pots: dict[str, list[dict[str, Any]]],
        zone: str,
        current_date: date,
        hour_weather: dict[str, Any],
        decision_by_pot_id: dict[int, dict[str, Any]],
        trigger_decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        trigger_ids = trigger_pot_ids(trigger_decisions)
        trigger_sensors = trigger_sensor_ids(trigger_decisions)
        trigger_codes = trigger_pot_codes(trigger_decisions)
        zone_dose_factor = baseline_zone_dose_factor(trigger_decisions)
        execution_decisions = zone_execution_decision_map(
            decision_by_pot_id,
            zone_pots,
            zone,
            current_date,
            trigger_decisions,
        )
        return execute_valve_zone_distribution(
            pot_states,
            zone_pots,
            zone,
            current_date,
            hour_weather,
            execution_decisions,
            lambda zone_pot, zone_decision: {
                **zone_decision,
                "should_irrigate": True,
                "dose_factor": zone_dose_factor,
            },
            self.request_builder.build,
            {
                "zone_triggered": True,
                "zone_trigger_sensor_ids": trigger_sensors,
                "zone_trigger_pot_ids": trigger_ids,
                "zone_trigger_pot_codes": trigger_codes,
                "runtime_request_sensor_ids": trigger_sensors,
                "zone_dose_factor": zone_dose_factor,
                "zone": zone,
                "zone_activation_policy": "sensor_pot_trigger",
                "zone_runtime_policy": "sensor_trigger_zone_budget_runtime",
            },
        )
