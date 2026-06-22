from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from digital_twin.domain.irrigation import IrrigationSlot, IrrigationStatus
from digital_twin.domain.pot import Pot, PotExposureRules
from digital_twin.infrastructure.database.schema.seeding import (
    DEFAULT_PLANT_CATALOG,
    DEFAULT_POT_EXPOSURE_RULES,
    DEFAULT_POT_SIZE_CATALOG,
)
from digital_twin.domain.sensor import SensorReadingResolution, SensorSource


class PlantCatalogTests(unittest.TestCase):
    def test_default_plant_catalog_exposes_reference_data(self) -> None:
        self.assertEqual(("vegetables", "herbs", "ornamentals", "succulents"), DEFAULT_PLANT_CATALOG.codes())
        self.assertEqual(DEFAULT_PLANT_CATALOG.label_for("vegetables"), "Vegetables")
        self.assertEqual(len(DEFAULT_PLANT_CATALOG.reference_rows()), 4)

    def test_default_plant_catalog_owns_plant_specific_rules(self) -> None:
        self.assertEqual(
            DEFAULT_PLANT_CATALOG.soil_profile("succulents"),
            "gritty_fast_draining_mix",
        )
        self.assertEqual(DEFAULT_PLANT_CATALOG.flow_adjustment("vegetables"), Decimal("0.12"))
        self.assertTrue(DEFAULT_PLANT_CATALOG.has_low_water_need("succulents"))


class PotCatalogTests(unittest.TestCase):
    def test_default_pot_size_catalog_exposes_reference_data(self) -> None:
        self.assertEqual(("huge", "large", "medium", "small"), DEFAULT_POT_SIZE_CATALOG.size_class_codes())
        self.assertEqual(("7cm", "15cm", "30cm"), DEFAULT_POT_SIZE_CATALOG.small_subtypes())
        self.assertEqual(len(DEFAULT_POT_SIZE_CATALOG.reference_rows()), 6)
        self.assertEqual(DEFAULT_POT_SIZE_CATALOG.profile_code("small", "7cm"), "small_7cm")

    def test_default_pot_rules_own_exposure_behavior(self) -> None:
        self.assertIsInstance(DEFAULT_POT_EXPOSURE_RULES, PotExposureRules)
        self.assertEqual(DEFAULT_POT_EXPOSURE_RULES.rain_exposure_for_zone("south_rail"), "fully_exposed")
        self.assertEqual(
            DEFAULT_POT_EXPOSURE_RULES.flow_adjustment("reflected_heat", "gusty"),
            Decimal("0.22"),
        )
        self.assertTrue(DEFAULT_POT_EXPOSURE_RULES.is_hot_gusty("reflected_heat", "gusty"))

    def test_pot_domain_object_owns_irrigation_traits(self) -> None:
        pot = Pot.from_mapping(
            {
                "id": 7,
                "pot_code": "POT-007",
                "plant_type_code": "vegetables",
                "water_need_level": "medium",
                "heat_sensitive": False,
                "allows_second_watering": False,
                "size_class": "small",
                "small_subtype": None,
                "balcony_zone": "hanging_row",
                "rain_exposure": "fully_exposed",
                "sun_exposure": "full",
                "moisture_min_pct": 20,
                "moisture_target_pct": 35,
                "moisture_max_pct": 70,
                "winter_moisture_target_pct": 15,
                "volume_l": 2.2,
                "retention_factor": 0.6,
                "drip_flow_ml_min": 8,
                "cycle_soak_enabled": True,
            }
        )

        self.assertEqual(pot.target_moisture_for_slot("morning"), 35.0)
        self.assertEqual(pot.target_moisture_for_slot("winter_check"), 15.0)
        self.assertEqual(pot.winter_trigger_threshold(), 10.0)
        self.assertEqual(pot.critical_low_threshold("morning"), 12.0)
        self.assertTrue(pot.is_high_need())
        self.assertTrue(pot.is_heat_priority())
        self.assertTrue(pot.allows_second_watering_in_heat())
        self.assertEqual(pot.surface_area_m2(), 0.04)
        self.assertEqual(pot.effective_flow_rate_ml_min(), 8.0)
        self.assertEqual(pot.runtime_min_for_volume(80.0), 10.0)
        self.assertEqual(pot.cycle_count_for_runtime(12.0), 2)
        self.assertEqual(pot.soak_pause_min_for_runtime(12.0), 10)
        self.assertEqual(pot.cycle_count_for_runtime(9.0), 1)
        self.assertAlmostEqual(pot.moisture_gain_for_volume(220.0), 6.0)
        self.assertEqual(pot.moisture_after_volume(98.0, 220.0), 100.0)
        self.assertAlmostEqual(pot.volume_for_moisture_deficit(20.0, 35.0, 100.0), 550.0)
        self.assertAlmostEqual(pot.volume_for_moisture_deficit(20.0, 35.0, 20.0), 160.0)
        self.assertTrue(pot.is_outdoor(date(2026, 7, 1)))
        self.assertEqual(pot.rain_exposure_factor(date(2026, 7, 1)), 1.0)


class DomainVocabularyTests(unittest.TestCase):
    def test_irrigation_vocabulary_separates_event_and_actuation_statuses(self) -> None:
        event_statuses = IrrigationStatus.event_values()
        actuation_statuses = IrrigationStatus.actuation_values()

        self.assertEqual(IrrigationSlot.values(), ("morning", "evening", "winter_check", "daily_prescription"))
        self.assertEqual(event_statuses, ("planned", "running", "completed", "skipped", "cancelled"))
        self.assertEqual(actuation_statuses, (*event_statuses, "failed"))
        self.assertNotIn("failed", event_statuses)
        self.assertIn("failed", actuation_statuses)

    def test_sensor_vocabulary_is_enum_backed(self) -> None:
        sensor_sources = SensorSource.values()
        reading_resolutions = SensorReadingResolution.values()

        self.assertEqual(sensor_sources, ("simulated_sensor", "actual_sensor", "actuator_feedback", "forecast_simulated_sensor"))
        self.assertEqual(reading_resolutions, ("raw_15min", "hourly", "daily"))
        self.assertEqual(SensorReadingResolution.query_values(), list(reading_resolutions))
        self.assertEqual(SensorSource.ACTUAL.value, "actual_sensor")
        self.assertEqual(SensorReadingResolution.RAW.value, "raw_15min")

    def test_default_sensor_source_expands_to_runtime_sources(self) -> None:
        expected = [
            SensorSource.DEFAULT.value,
            SensorSource.ACTUAL.value,
            SensorSource.ACTUATOR_FEEDBACK.value,
        ]
        self.assertEqual(SensorSource.query_values(None), expected)
        self.assertEqual(SensorSource.query_values(SensorSource.DEFAULT.value), expected)
        self.assertEqual(SensorSource.query_values(SensorSource.ACTUAL.value), [SensorSource.ACTUAL.value])
        self.assertEqual(SensorSource.query_values("custom_sensor"), ["custom_sensor"])


if __name__ == "__main__":
    unittest.main()
