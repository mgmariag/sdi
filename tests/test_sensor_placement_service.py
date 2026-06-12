from __future__ import annotations

import unittest
from typing import Any

from digital_twin.domain.irrigation_methods import VALVE_ZONE_DESIGN
from digital_twin.services.sensor_placements import (
    MIN_SENSOR_COUNT,
    PLACEMENT_POLICY_VERSION,
    SensorPlacementService,
)


class SensorPlacementServiceTests(unittest.TestCase):
    def test_recommendation_clamps_to_valve_count_and_covers_zones(self) -> None:
        repository = _FakePlacementRepository(pots=_pots_by_valve_zone())
        service = SensorPlacementService(repository=repository)

        result = service.recommend(sensor_count=1)

        self.assertEqual(result["sensor_count"], MIN_SENSOR_COUNT)
        self.assertEqual({item["balcony_zone"] for item in result["items"]}, {item["zone"] for item in VALVE_ZONE_DESIGN})
        self.assertTrue(all(item["criteria"]["role"] == "valve_representative" for item in result["items"]))

    def test_ensure_replaces_existing_plan_that_misses_valve_zones(self) -> None:
        current = {
            "sensor_count": MIN_SENSOR_COUNT,
            "items": [{"balcony_zone": "west_wall"} for _ in range(MIN_SENSOR_COUNT)],
            "active_pot_count": 200,
        }
        repository = _FakePlacementRepository(current=current, pots=_pots_by_valve_zone())
        service = SensorPlacementService(repository=repository)

        result = service.ensure(sensor_count=MIN_SENSOR_COUNT)

        self.assertTrue(result["changed"])
        self.assertEqual({item["balcony_zone"] for item in result["items"]}, {item["zone"] for item in VALVE_ZONE_DESIGN})

    def test_ensure_keeps_valid_valve_zone_plan(self) -> None:
        current = {
            "sensor_count": MIN_SENSOR_COUNT,
            "items": [
                {
                    "balcony_zone": item["zone"],
                    "criteria": {"placement_policy_version": PLACEMENT_POLICY_VERSION},
                }
                for item in VALVE_ZONE_DESIGN
            ],
            "active_pot_count": 200,
        }
        repository = _FakePlacementRepository(current=current, pots=_pots_by_valve_zone())
        service = SensorPlacementService(repository=repository)

        result = service.ensure(sensor_count=1)

        self.assertFalse(result["changed"])
        self.assertIsNone(repository.replaced_count)

    def test_recommendation_prefers_fast_drying_zone_sentinel(self) -> None:
        pots = _pots_by_valve_zone()
        west_zone = VALVE_ZONE_DESIGN[0]["zone"]
        pots = [pot for pot in pots if pot["balcony_zone"] != west_zone]
        pots.extend(
            [
                _pot(
                    101,
                    west_zone,
                    plant_type_code="ornamentals",
                    plant_type_label="Ornamentals",
                    water_need_level="medium",
                    heat_sensitive=False,
                    sun_exposure="partial",
                    size_class="large",
                    wind_exposure="sheltered",
                    retention_factor=1.2,
                ),
                _pot(
                    102,
                    west_zone,
                    plant_type_code="ornamentals",
                    plant_type_label="Ornamentals",
                    water_need_level="medium",
                    heat_sensitive=False,
                    sun_exposure="partial",
                    size_class="large",
                    wind_exposure="sheltered",
                    retention_factor=1.2,
                ),
                _pot(
                    103,
                    west_zone,
                    plant_type_code="ornamentals",
                    plant_type_label="Ornamentals",
                    water_need_level="medium",
                    heat_sensitive=False,
                    sun_exposure="partial",
                    size_class="large",
                    wind_exposure="sheltered",
                    retention_factor=1.2,
                ),
                _pot(
                    104,
                    west_zone,
                    plant_type_code="vegetables",
                    plant_type_label="Vegetables",
                    water_need_level="high",
                    heat_sensitive=True,
                    sun_exposure="reflected_heat",
                    size_class="small",
                    wind_exposure="gusty",
                    retention_factor=0.75,
                ),
            ]
        )
        repository = _FakePlacementRepository(pots=pots)
        service = SensorPlacementService(repository=repository)

        result = service.recommend(sensor_count=MIN_SENSOR_COUNT)

        west_sensor = next(item for item in result["items"] if item["pot"]["balcony_zone"] == west_zone)
        self.assertEqual(west_sensor["pot"]["id"], 104)
        self.assertEqual(west_sensor["criteria"]["placement_policy_version"], PLACEMENT_POLICY_VERSION)


class _FakePlacementRepository:
    def __init__(self, current: dict[str, Any] | None = None, pots: list[dict[str, Any]] | None = None) -> None:
        self.current_result = current or {
            "sensor_count": 0,
            "items": [],
            "active_pot_count": len(pots or []),
        }
        self.pots = pots or []
        self.replaced_count: int | None = None

    def current(self) -> dict[str, Any]:
        return dict(self.current_result)

    def active_pots(self) -> list[dict[str, Any]]:
        return list(self.pots)

    def replace(self, requested_sensor_count: int, recommendations: list[dict[str, Any]]) -> dict[str, Any]:
        self.replaced_count = requested_sensor_count
        items = [
            {
                **item,
                "balcony_zone": item["pot"]["balcony_zone"],
            }
            for item in recommendations
        ]
        self.current_result = {
            "sensor_count": requested_sensor_count,
            "items": items,
            "active_pot_count": len(self.pots),
            "changed": True,
        }
        return dict(self.current_result)

    def selected_pot_ids(self, candidate_pot_ids: list[int] | None = None) -> list[int]:
        return []


def _pots_by_valve_zone() -> list[dict[str, Any]]:
    return [
        _pot(index, item["zone"])
        for index, item in enumerate(VALVE_ZONE_DESIGN, start=1)
    ]


def _pot(
    index: int,
    zone: str,
    plant_type_code: str = "basil",
    plant_type_label: str = "Basil",
    water_need_level: str = "medium",
    heat_sensitive: bool = False,
    sun_exposure: str = "full",
    size_class: str = "medium",
    wind_exposure: str = "moderate",
    retention_factor: float = 1.0,
) -> dict[str, Any]:
    return {
        "id": index,
        "pot_code": f"P{index}",
        "label": f"Pot {index}",
        "size_class": size_class,
        "small_subtype": None,
        "plant_type_code": plant_type_code,
        "plant_type_label": plant_type_label,
        "water_need_level": water_need_level,
        "heat_sensitive": heat_sensitive,
        "balcony_zone": zone,
        "rain_exposure": "partially_exposed",
        "sun_exposure": sun_exposure,
        "wind_exposure": wind_exposure,
        "container_material": "plastic",
        "evaporation_factor": 1.0,
        "retention_factor": retention_factor,
    }


if __name__ == "__main__":
    unittest.main()
