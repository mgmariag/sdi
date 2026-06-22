from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from digital_twin.domain.pot import Pot
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at

PotInput = Mapping[str, Any] | Pot


def normalized_dose_factor(value: float) -> float:
    number_value = soil.clamp(soil.number(value, 1.0), 0.5, 1.0)
    if number_value <= 0.625:
        return 0.5
    if number_value <= 0.875:
        return 0.75
    return 1.0


class IrrigationRequestBuilder:
    """Converts strategy decisions into scheduled irrigation request events."""

    def full_dose_start_moisture(self, pot: PotInput, decision: dict[str, Any], target: float) -> float:
        domain_pot = Pot.from_mapping(pot)
        if decision.get("full_dose_start_moisture_pct") is not None:
            return soil.clamp(soil.number(decision.get("full_dose_start_moisture_pct"), target), 0.0, target)
        if decision.get("current_moisture_pct") is not None:
            return soil.clamp(soil.number(decision.get("current_moisture_pct"), target), 0.0, target)
        if decision.get("slot") == "winter_check":
            return max(7.0, min(target - 5.0, 12.0))
        return soil.number(domain_pot.moisture_min_pct, target)

    def build(self, pot: PotInput, weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        domain_pot = Pot.from_mapping(pot)
        target = soil.number(decision.get("target_moisture_pct"), domain_pot.moisture_target_pct)
        start_moisture = self.full_dose_start_moisture(domain_pot, decision, target)
        dose_factor = normalized_dose_factor(soil.number(decision.get("dose_factor"), 1.0))
        flow_rate = domain_pot.effective_flow_rate_ml_min()
        max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[domain_pot.size_class]
        full_dose_volume_ml = domain_pot.volume_for_moisture_deficit(start_moisture, target, max_minutes)
        planned_volume_ml = full_dose_volume_ml * dose_factor
        duration_min = domain_pot.runtime_min_for_volume(planned_volume_ml)
        cycle_count = domain_pot.cycle_count_for_runtime(duration_min)
        soak_pause_min = domain_pot.soak_pause_min_for_runtime(duration_min)

        scheduled_start = local_observed_at(weather)
        scheduled_end = scheduled_start + timedelta(minutes=duration_min + soak_pause_min)
        sensor_id = int(decision.get("sensor_id", domain_pot.id))
        return {
            "pot_id": domain_pot.id,
            "sensor_id": sensor_id,
            "request_sensor_id": sensor_id,
            "pot_code": domain_pot.pot_code,
            "date": scheduled_start.date().isoformat(),
            "slot": decision["slot"],
            "scheduled_start_at": scheduled_start.isoformat(),
            "scheduled_end_at": scheduled_end.isoformat(),
            "flow_rate_ml_min": round(flow_rate, 2),
            "planned_volume_ml": round(planned_volume_ml, 2),
            "requested_volume_ml": round(planned_volume_ml, 2),
            "duration_min": round(duration_min, 3),
            "cycle_count": cycle_count,
            "soak_pause_min": soak_pause_min,
            "full_dose_volume_ml": round(full_dose_volume_ml, 2),
            "dose_factor": round(dose_factor, 3),
            "dose_reduction_pct": round((1.0 - dose_factor) * 100.0, 1),
        }
