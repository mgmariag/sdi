from __future__ import annotations

VALVE_ZONE_DESIGN = (
    {"valve_number": 1, "zone": "west_wall"},
    {"valve_number": 2, "zone": "south_rail"},
    {"valve_number": 3, "zone": "east_corner"},
    {"valve_number": 4, "zone": "north_shelter"},
    {"valve_number": 5, "zone": "hanging_row"},
)
VALVE_COUNT = len(VALVE_ZONE_DESIGN)
VALVE_ZONE_ORDER = {item["zone"]: item["valve_number"] for item in VALVE_ZONE_DESIGN}

__all__ = ["VALVE_COUNT", "VALVE_ZONE_DESIGN", "VALVE_ZONE_ORDER"]
