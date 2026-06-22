from __future__ import annotations

from typing import Any

from digital_twin.domain.valve import DEFAULT_VALVE_LAYOUT
from digital_twin.infrastructure.database.repositories.sensor_placements import (
    SensorPlacementRepository,
)

MIN_SENSOR_COUNT = DEFAULT_VALVE_LAYOUT.count
DEFAULT_SENSOR_COUNT = MIN_SENSOR_COUNT
PLACEMENT_POLICY_VERSION = 2
PLACEMENT_POLICY_NAME = "zone_fast_drying_representative"


class SensorPlacementService:
    """Scores active pots and persists the current sensor placement plan."""

    def __init__(
        self,
        repository: SensorPlacementRepository | None = None,
        scorer: "SensorPlacementScorer" | None = None,
    ) -> None:
        self.repository = repository or SensorPlacementRepository()
        self.scorer = scorer or SensorPlacementScorer()

    def current(self) -> dict[str, Any]:
        return self.repository.current()

    def ensure_default_if_missing(self) -> dict[str, Any]:
        current = self.repository.current()
        if _current_placement_is_valid(current):
            current["changed"] = False
            return current
        return self.recommend(int(current.get("sensor_count") or DEFAULT_SENSOR_COUNT))

    def recommend(self, sensor_count: int = DEFAULT_SENSOR_COUNT) -> dict[str, Any]:
        pots = self.repository.active_pots()
        if not pots:
            result = self.repository.replace(sensor_count, [])
            result["changed"] = True
            return result

        requested_count = _normalized_sensor_count(sensor_count, len(pots))
        scored = [self.scorer.score(pot) for pot in pots]
        selected = self.scorer.select(scored, requested_count)
        result = self.repository.replace(requested_count, selected)
        result["active_pot_count"] = len(pots)
        result["changed"] = True
        return result

    def ensure(self, sensor_count: int = DEFAULT_SENSOR_COUNT) -> dict[str, Any]:
        current = self.repository.current()
        requested_count = _normalized_sensor_count(sensor_count, int(current.get("active_pot_count") or 0))
        if int(current.get("sensor_count") or 0) < requested_count:
            requested_count = _normalized_sensor_count(requested_count, int(current.get("active_pot_count") or 0))
        if _current_placement_is_valid(current) and int(current["sensor_count"]) == requested_count:
            current["changed"] = False
            return current
        return self.recommend(requested_count)

    def selected_pot_ids(self, candidate_pot_ids: list[int] | None = None) -> list[int]:
        return self.repository.selected_pot_ids(candidate_pot_ids)


class SensorPlacementScorer:
    """Ranks pots for sparse sensor placement coverage."""

    sun_scores = {
        "reflected_heat": 35.0,
        "full": 28.0,
        "partial": 16.0,
        "shade": 6.0,
    }
    size_scores = {
        "small": 30.0,
        "medium": 22.0,
        "large": 14.0,
        "huge": 10.0,
    }
    wind_scores = {
        "gusty": 14.0,
        "moderate": 8.0,
        "sheltered": 3.0,
    }
    water_need_scores = {
        "high": 16.0,
        "medium": 9.0,
        "low": 3.0,
    }
    material_scores = {
        "terracotta": 8.0,
        "fabric": 7.0,
        "plastic": 5.0,
        "ceramic": 3.0,
    }
    rain_scores = {
        "fully_exposed": 8.0,
        "partially_exposed": 5.0,
        "covered": 2.0,
    }

    def __init__(self, valve_layout=DEFAULT_VALVE_LAYOUT) -> None:
        self.valve_layout = valve_layout

    def score(self, pot: dict[str, Any]) -> dict[str, Any]:
        components = {
            "sun": self.sun_scores.get(pot["sun_exposure"], 10.0),
            "size": self.size_scores.get(pot["size_class"], 12.0),
            "wind": self.wind_scores.get(pot["wind_exposure"], 5.0),
            "water_need": self.water_need_scores.get(pot["water_need_level"], 6.0),
            "material": self.material_scores.get(pot["container_material"], 4.0),
            "rain_exposure": self.rain_scores.get(str(pot.get("rain_exposure") or "partially_exposed"), 5.0),
            "heat_sensitive": 5.0 if pot.get("heat_sensitive") else 0.0,
            "evaporation": min(float(pot.get("evaporation_factor") or 1.0) * 6.0, 14.0),
            "low_retention": max(0.0, (1.25 - float(pot.get("retention_factor") or 1.0)) * 12.0),
        }
        return {
            "pot": pot,
            "base_score": round(sum(components.values()), 3),
            "components": components,
            "reasons": self.score_reasons(pot, components),
        }

    def select(self, scored: list[dict[str, Any]], sensor_count: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        remaining = list(scored)

        for design in self.valve_layout.design():
            if len(selected) >= sensor_count:
                break
            zone_candidates = [item for item in remaining if item["pot"]["balcony_zone"] == design["zone"]]
            if not zone_candidates:
                continue
            best = max(zone_candidates, key=lambda item: item["base_score"])
            selected.append(self.selection_item(best, selected, role="valve_representative"))
            remaining.remove(best)

        while remaining and len(selected) < sensor_count:
            best = max(remaining, key=lambda item: item["base_score"] - self.diversity_penalty(item["pot"], selected))
            selected.append(self.selection_item(best, selected, role="extra_coverage"))
            remaining.remove(best)
        return selected

    def selection_item(self, best: dict[str, Any], selected: list[dict[str, Any]], role: str) -> dict[str, Any]:
        pot = best["pot"]
        penalty = self.diversity_penalty(pot, selected)
        adjusted_score = round(best["base_score"] - penalty, 3)
        valve_number = self.valve_layout.valve_number_for_zone(pot["balcony_zone"])
        role_reason = f"V{valve_number} representative" if role == "valve_representative" and valve_number else "extra coverage"
        return {
            "rank": len(selected) + 1,
            "sensor_id": len(selected) + 1,
            "pot": pot,
            "pot_id": pot["id"],
            "score": adjusted_score,
            "reason": ", ".join([role_reason, *best["reasons"]]),
            "criteria": {
                "base_score": best["base_score"],
                "diversity_penalty": penalty,
                "components": best["components"],
                "role": role,
                "placement_policy": PLACEMENT_POLICY_NAME,
                "placement_policy_version": PLACEMENT_POLICY_VERSION,
                "valve_number": valve_number,
                "valve_zone": pot["balcony_zone"],
            },
        }

    def diversity_penalty(self, pot: dict[str, Any], selected: list[dict[str, Any]]) -> float:
        if not selected:
            return 0.0
        penalty = 0.0
        for item in selected:
            selected_pot = item["pot"] if "pot" in item else None
            if selected_pot is None:
                continue
            if pot["balcony_zone"] == selected_pot["balcony_zone"]:
                penalty += 12.0
            if pot["sun_exposure"] == selected_pot["sun_exposure"]:
                penalty += 8.0
            if pot["size_class"] == selected_pot["size_class"]:
                penalty += 6.0
            if pot["plant_type_code"] == selected_pot["plant_type_code"]:
                penalty += 4.0
        return penalty

    def score_reasons(self, pot: dict[str, Any], components: dict[str, float]) -> list[str]:
        reason_candidates = [
            (components["sun"], pot["sun_exposure"].replace("_", " ")),
            (components["size"], self.size_label(pot)),
            (components["wind"], f"{pot['wind_exposure']} wind"),
            (components["water_need"], f"{pot['water_need_level']} water need"),
            (components["material"], pot["container_material"]),
            (components["evaporation"], f"{pot['balcony_zone']} zone"),
        ]
        reasons = [label for _, label in sorted(reason_candidates, reverse=True)[:4]]
        if pot.get("heat_sensitive"):
            reasons.append("heat sensitive")
        return reasons

    def size_label(self, pot: dict[str, Any]) -> str:
        if pot["size_class"] == "small" and pot.get("small_subtype"):
            return f"small {pot['small_subtype']}"
        return pot["size_class"]


def _normalized_sensor_count(sensor_count: int, active_pot_count: int) -> int:
    requested = max(MIN_SENSOR_COUNT, int(sensor_count or DEFAULT_SENSOR_COUNT))
    return min(requested, active_pot_count) if active_pot_count > 0 else requested


def _current_placement_is_valid(current: dict[str, Any]) -> bool:
    items = current.get("items") or []
    if not items or int(current.get("sensor_count") or 0) < MIN_SENSOR_COUNT:
        return False

    selected_zones = {str(item.get("balcony_zone") or "") for item in items}
    required_zones = DEFAULT_VALVE_LAYOUT.required_zones()
    has_current_policy = all(
        int((item.get("criteria") or {}).get("placement_policy_version") or 0) == PLACEMENT_POLICY_VERSION
        for item in items
    )
    return required_zones.issubset(selected_zones) and has_current_policy


__all__ = [
    "DEFAULT_SENSOR_COUNT",
    "MIN_SENSOR_COUNT",
    "PLACEMENT_POLICY_NAME",
    "PLACEMENT_POLICY_VERSION",
    "SensorPlacementScorer",
    "SensorPlacementService",
]

