from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class PlantType:
    code: str
    label: str
    water_need_level: str
    moisture_min_pct: int
    moisture_target_pct: int
    moisture_max_pct: int
    winter_moisture_target_pct: int
    heat_sensitive: bool
    allows_second_watering: bool
    notes: str
    soil_profile: str
    flow_adjustment: Decimal = Decimal("0.0")
    sample_weight: float = 0.0

    def reference_row(self) -> tuple:
        return (
            self.code,
            self.label,
            self.water_need_level,
            self.moisture_min_pct,
            self.moisture_target_pct,
            self.moisture_max_pct,
            self.winter_moisture_target_pct,
            self.heat_sensitive,
            self.allows_second_watering,
            self.notes,
        )


class PlantCatalog:
    def __init__(self, plant_types: Iterable[PlantType]) -> None:
        self.plant_types = tuple(plant_types)
        self._by_code = {plant_type.code: plant_type for plant_type in self.plant_types}

    def get(self, code: str) -> PlantType:
        return self._by_code[code]

    def codes(self) -> tuple[str, ...]:
        return tuple(plant_type.code for plant_type in self.plant_types)

    def labels(self) -> dict[str, str]:
        return {plant_type.code: plant_type.label for plant_type in self.plant_types}

    def label_for(self, code: str) -> str:
        return self.get(code).label

    def reference_rows(self) -> list[tuple]:
        return [plant_type.reference_row() for plant_type in self.plant_types]

    def weighted_distribution(self) -> list[tuple[str, float]]:
        return [
            (plant_type.code, plant_type.sample_weight)
            for plant_type in self.plant_types
            if plant_type.sample_weight > 0
        ]

    def soil_profile(self, code: str) -> str:
        return self.get(code).soil_profile

    def flow_adjustment(self, code: str) -> Decimal:
        return self.get(code).flow_adjustment

    def has_low_water_need(self, code: str) -> bool:
        return self.get(code).water_need_level == "low"
