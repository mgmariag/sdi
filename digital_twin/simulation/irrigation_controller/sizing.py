from __future__ import annotations

from typing import Any


def _size_flow_rate_multiplier(pot: dict[str, Any]) -> float:
    return 1.0


def _pot_surface_area_m2(pot: dict[str, Any]) -> float:
    if pot["size_class"] == "small":
        return {
            "window_box": 0.06,
            "hanging": 0.04,
            "tabletop": 0.025,
        }.get(pot.get("small_subtype"), 0.04)
    return {
        "medium": 0.09,
        "large": 0.18,
        "huge": 0.32,
    }[pot["size_class"]]
