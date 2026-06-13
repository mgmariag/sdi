from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.soil_model import clamp, number


def apply_event_delivery(
    state: PotState,
    pot: dict[str, Any],
    event: dict[str, Any],
    delivered_volume_ml: float,
    duration_min: float | None = None,
) -> dict[str, Any]:
    delivered_volume_ml = max(0.0, float(delivered_volume_ml or 0.0))
    requested_volume_ml = max(0.0, number(event.get("requested_volume_ml"), event.get("planned_volume_ml", 0.0)))
    flow_rate = max(number(event.get("flow_rate_ml_min"), pot["drip_flow_ml_min"]), 1.0)
    runtime_min = max(0.0, number(duration_min, delivered_volume_ml / flow_rate if flow_rate > 0 else 0.0))
    cycle_count = 2 if pot["cycle_soak_enabled"] and runtime_min >= 10 else 1
    soak_pause_min = 10 if cycle_count == 2 else 0

    pre_delivery_moisture = float(state.moisture)
    _apply_planned_volume(state, pot, delivered_volume_ml)
    post_delivery_moisture = float(state.moisture)

    scheduled_start = datetime.fromisoformat(str(event["scheduled_start_at"]))
    scheduled_end = scheduled_start + timedelta(minutes=runtime_min + soak_pause_min)
    output = dict(event)
    output.update(
        {
            "scheduled_end_at": scheduled_end.isoformat(),
            "duration_min": round(runtime_min, 3),
            "valve_runtime_min": round(runtime_min, 3),
            "cycle_count": cycle_count,
            "soak_pause_min": soak_pause_min,
            "requested_volume_ml": round(requested_volume_ml, 2),
            "delivered_volume_ml": round(delivered_volume_ml, 2),
            "planned_volume_ml": round(delivered_volume_ml, 2),
            "delivery_error_ml": round(delivered_volume_ml - requested_volume_ml, 2),
            "delivery_ratio": round(delivered_volume_ml / requested_volume_ml, 4) if requested_volume_ml > 0 else None,
            "pre_delivery_moisture_pct": round(pre_delivery_moisture, 2),
            "post_delivery_moisture_pct": round(post_delivery_moisture, 2),
            "delivery_moisture_gain_pct": round(post_delivery_moisture - pre_delivery_moisture, 2),
            "physical_distribution_policy": "valve_runtime_x_pot_drip_flow",
        }
    )
    return output


def _apply_planned_volume(state: PotState, pot: dict[str, Any], planned_volume_ml: float) -> None:
    volume_l = pot["volume_l"]
    retention = max(pot["retention_factor"], 0.1)
    moisture_gain = planned_volume_ml * retention / max(volume_l * 10.0, 1.0)
    state.moisture = clamp(state.moisture + moisture_gain, 0.0, 100.0)
