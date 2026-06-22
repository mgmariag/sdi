from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.domain.pot import Pot
from digital_twin.domain.valve import DEFAULT_VALVE_LAYOUT


def pots_by_valve_zone(pots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    zones: dict[str, list[dict[str, Any]]] = {}
    for pot in pots:
        zones.setdefault(pot["balcony_zone"], []).append(pot)
    return zones


def is_valve_managed_pot(pot: dict[str, Any], day: date) -> bool:
    return Pot.from_mapping(pot).is_outdoor(day)


def valve_managed_zone_pots(zone_pots: dict[str, list[dict[str, Any]]], zone: str, day: date) -> list[dict[str, Any]]:
    return [pot for pot in zone_pots.get(zone, []) if is_valve_managed_pot(pot, day)]


def valve_number_for_zone(zone: str) -> int:
    return DEFAULT_VALVE_LAYOUT.fallback_valve_number_for_zone(zone)

