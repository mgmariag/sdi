from __future__ import annotations

from enum import Enum


class IrrigationMethod(str, Enum):
    BASELINE = "baseline"
    SAMPLING = "sampling"
    ANFIS_GA = "anfis"
    FUZZY_DT = "fuzzy"


METHOD_LABELS: dict[IrrigationMethod, str] = {
    IrrigationMethod.BASELINE: "Baseline",
    IrrigationMethod.SAMPLING: "Sampling",
    IrrigationMethod.ANFIS_GA: "ANFIS-GA",
    IrrigationMethod.FUZZY_DT: "Fuzzy DT / FAO-PM",
}

VALVE_ZONE_DESIGN = (
    {"valve_number": 1, "zone": "west_wall"},
    {"valve_number": 2, "zone": "south_rail"},
    {"valve_number": 3, "zone": "east_corner"},
    {"valve_number": 4, "zone": "north_shelter"},
    {"valve_number": 5, "zone": "hanging_row"},
)
VALVE_ZONE_ORDER = {item["zone"]: item["valve_number"] for item in VALVE_ZONE_DESIGN}

__all__ = ["IrrigationMethod", "METHOD_LABELS", "VALVE_ZONE_DESIGN", "VALVE_ZONE_ORDER"]
