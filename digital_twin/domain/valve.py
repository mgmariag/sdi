from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ValveZone:
    valve_number: int
    zone: str

    def as_dict(self) -> dict[str, int | str]:
        return {"valve_number": self.valve_number, "zone": self.zone}


class ValveLayout:
    def __init__(self, zones: Iterable[ValveZone]) -> None:
        self.zones = tuple(zones)
        self._zone_order = {item.zone: item.valve_number for item in self.zones}

    @property
    def count(self) -> int:
        return len(self.zones)

    @property
    def zone_order(self) -> dict[str, int]:
        return dict(self._zone_order)

    def design(self) -> tuple[dict[str, int | str], ...]:
        return tuple(item.as_dict() for item in self.zones)

    def required_zones(self) -> set[str]:
        return {item.zone for item in self.zones}

    def configured_numbers(self) -> list[int]:
        return sorted(item.valve_number for item in self.zones)

    def zone_for_number(self, valve_number: int) -> str | None:
        for item in self.zones:
            if item.valve_number == valve_number:
                return item.zone
        return None

    def valve_number_for_zone(self, zone: str) -> int | None:
        return self._zone_order.get(zone)

    def fallback_valve_number_for_zone(self, zone: str) -> int:
        return self.valve_number_for_zone(zone) or self.count + 1

    def design_for_zones(self, zones: Iterable[str]) -> list[dict[str, int | str]]:
        zone_set = set(zones)
        design = [item.as_dict() for item in self.zones if item.zone in zone_set]
        unknown_zones = sorted(zone for zone in zone_set if zone not in self._zone_order)
        next_valve = self.count + 1
        design.extend(
            {"valve_number": next_valve + index, "zone": zone}
            for index, zone in enumerate(unknown_zones)
        )
        return design


DEFAULT_VALVE_LAYOUT = ValveLayout(
    (
        ValveZone(1, "west_wall"),
        ValveZone(2, "south_rail"),
        ValveZone(3, "east_corner"),
        ValveZone(4, "north_shelter"),
        ValveZone(5, "hanging_row"),
    )
)
