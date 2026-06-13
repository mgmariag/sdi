from __future__ import annotations

from typing import Any

from digital_twin.domain.valves import VALVE_ZONE_DESIGN
from digital_twin.infrastructure.database.repositories.overview._common import (
    NO_IRRIGATION_RECORDED_LABEL,
    number as _number,
)


def irrigation_activity(
    planned_window: dict[str, Any] | None,
    recent_window: dict[str, Any] | None,
) -> dict[str, Any]:
    if planned_window:
        return {**planned_window, "mode": "next_planned", "display_label": "Next planned irrigation"}
    if recent_window:
        return {**recent_window, "mode": "most_recent", "display_label": "Most recent irrigation"}
    return {
        "label": NO_IRRIGATION_RECORDED_LABEL,
        "start_at": None,
        "end_at": None,
        "source": "none",
        "mode": "none",
        "display_label": "Most recent irrigation",
        "item_count": 0,
        "planned_volume_l": 0.0,
        "valves": [],
    }


def activity_window(
    rows: list[dict[str, Any]],
    source: str,
    mode: str,
    display_label: str,
) -> dict[str, Any] | None:
    if not rows:
        return None
    start = min(item["start_at"] for item in rows if item.get("start_at"))
    end = max(item["end_at"] for item in rows if item.get("end_at"))
    experiments = sorted({str(item.get("experiment_type")) for item in rows if item.get("experiment_type")})
    valve_numbers = sorted({
        int(item["valve_number"])
        for item in rows
        if item.get("valve_number") is not None
    })
    valves = _activity_window_valves(rows)
    return {
        "label": f"{start:%Y-%m-%d %H:%M} - {end:%H:%M}",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "source": source,
        "mode": mode,
        "display_label": display_label,
        "item_count": len(rows),
        "planned_volume_l": round(
            sum(_number(item.get("planned_volume_ml"), 0.0) for item in rows) / 1000.0,
            2,
        ),
        "experiment_types": experiments,
        "activated_valves": _valve_label(valve_numbers),
        "valves": valves,
    }


def _activity_window_valves(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valves: dict[int, dict[str, Any]] = {}
    for item in rows:
        valve_number = item.get("valve_number")
        if valve_number is None:
            continue
        number = int(valve_number)
        valve_zone = item.get("valve_zone") or _valve_zone_for_number(number)
        current = valves.setdefault(
            number,
            {
                "valve_number": number,
                "valve_zone": valve_zone,
                "valve_name": _valve_zone_label(valve_zone),
                "planned_volume_l": 0.0,
            },
        )
        current["planned_volume_l"] += _number(item.get("planned_volume_ml"), 0.0) / 1000.0

    return [
        {
            **item,
            "planned_volume_l": round(item["planned_volume_l"], 2),
        }
        for item in sorted(valves.values(), key=lambda value: value["valve_number"])
    ]


def _valve_zone_for_number(valve_number: int) -> str:
    for item in VALVE_ZONE_DESIGN:
        if int(item["valve_number"]) == valve_number:
            return str(item["zone"])
    return ""


def _valve_zone_label(zone: Any) -> str:
    return str(zone).replace("_", " ") if zone else "Unmapped"


def _valve_label(valve_numbers: list[int]) -> str:
    if not valve_numbers:
        return "none"
    if valve_numbers == list(range(min(valve_numbers), max(valve_numbers) + 1)):
        return f"V{valve_numbers[0]}" if len(valve_numbers) == 1 else f"V{valve_numbers[0]}-V{valve_numbers[-1]}"
    return ", ".join(f"V{number}" for number in valve_numbers)
