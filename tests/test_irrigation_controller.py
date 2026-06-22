from __future__ import annotations

import unittest
from datetime import datetime

from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_FUZZY_POLICY,
    DEFAULT_IRRIGATION_REQUEST_BUILDER,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.irrigation_controller.delivery import apply_event_delivery
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import PotState


class BaselineIrrigationControllerTests(unittest.TestCase):
    def test_baseline_event_doses_from_actual_current_moisture(self) -> None:
        state = PotState(moisture=5.0)
        pot = _pot()
        weather = {"observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ)}
        decision = {
            "slot": "morning",
            "target_moisture_pct": 35.0,
            "current_moisture_pct": 5.0,
            "dose_factor": 1.0,
        }
        request = DEFAULT_IRRIGATION_REQUEST_BUILDER.build(pot, weather, decision)
        event = apply_event_delivery(
            state,
            pot,
            request,
            request["requested_volume_ml"],
            request.get("duration_min"),
        )

        self.assertGreater(event["planned_volume_ml"], 0.0)
        self.assertAlmostEqual(state.moisture, 35.0, places=2)

    def test_request_builder_exposes_neutral_irrigation_request(self) -> None:
        event = DEFAULT_IRRIGATION_REQUEST_BUILDER.build(
            _pot(),
            {"observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ)},
            {
                "slot": "morning",
                "target_moisture_pct": 35.0,
                "current_moisture_pct": 5.0,
                "dose_factor": 1.0,
            },
        )

        self.assertGreater(event["planned_volume_ml"], 0.0)
        self.assertEqual(event["slot"], "morning")

    def test_domain_policy_owns_safety_threshold(self) -> None:
        pot = {
            **_pot(),
            "plant_type_code": "herbs",
            "size_class": "small",
        }

        threshold = DEFAULT_IRRIGATION_POLICY.safety_threshold(
            pot,
            {**day_profile(max_temperature_c=32.0), "heatwave_day": True, "dry_windy_day": True},
            "morning",
        )

        self.assertEqual(threshold, 27.0)

    def test_domain_policy_detects_emergency_dryness(self) -> None:
        self.assertTrue(
            DEFAULT_IRRIGATION_POLICY.has_emergency_dryness(
                PotState(moisture=10.0),
                _pot(),
                datetime(2026, 7, 21).date(),
                datetime(2026, 7, 21, 12, 0, tzinfo=LOCAL_TZ),
            )
        )
        self.assertFalse(
            DEFAULT_IRRIGATION_POLICY.has_emergency_dryness(
                PotState(moisture=10.0),
                _pot(),
                datetime(2026, 7, 21).date(),
                datetime(2026, 7, 21, 8, 0, tzinfo=LOCAL_TZ),
            )
        )

    def test_fuzzy_prescription_signal_can_activate_above_fixed_threshold(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=38.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 8, 0, tzinfo=LOCAL_TZ), "temperature_c": 32.0},
            day_profile(max_temperature_c=32.0),
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_prescription_signal")
        self.assertGreater(decision["current_moisture_pct"], decision["fuzzy_trigger_threshold_pct"])
        self.assertGreater(decision["planned_volume_ml"], 0.0)

    def test_fuzzy_prescribes_when_moisture_is_below_trigger_threshold(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=15.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 8, 0, tzinfo=LOCAL_TZ), "temperature_c": 30.0},
            day_profile(max_temperature_c=30.0),
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertGreater(decision["planned_volume_ml"], 0.0)

    def test_fuzzy_waters_before_minimum_when_below_comfort_floor(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=24.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ), "temperature_c": 22.0},
            day_profile(max_temperature_c=22.0),
            "morning",
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_low_moisture_prescription")
        self.assertGreaterEqual(decision["fuzzy_comfort_floor_pct"], 29.0)

    def test_fuzzy_does_not_treat_rain_as_comfort_when_moisture_is_low(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=24.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ), "temperature_c": 22.0},
            {**day_profile(max_temperature_c=22.0), "precipitation_mm": 6.0},
            "morning",
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_low_moisture_prescription")

    def test_fuzzy_rain_reduces_prescription_without_hard_skip(self) -> None:
        weather = {"id": 1, "observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ), "temperature_c": 22.0}
        dry_decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=34.0),
            _pot(),
            weather,
            day_profile(max_temperature_c=22.0),
            "morning",
        )
        rainy_decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=34.0),
            _pot(),
            weather,
            {**day_profile(max_temperature_c=22.0), "precipitation_mm": 8.0},
            "morning",
        )

        self.assertFalse(rainy_decision["should_irrigate"])
        self.assertNotIn("rain_sufficient", rainy_decision["reason_code"])
        self.assertLess(rainy_decision["prescription_volume_ml"], dry_decision["prescription_volume_ml"])
        self.assertLess(rainy_decision["prescription_score_pct"], dry_decision["prescription_score_pct"])
        self.assertLess(rainy_decision["planned_volume_ml"], dry_decision["planned_volume_ml"])

    def test_fuzzy_cold_remains_physical_hard_stop(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=15.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 2, 21, 10, 0, tzinfo=LOCAL_TZ), "temperature_c": 2.0},
            day_profile(max_temperature_c=2.0),
            "winter_check",
        )

        self.assertFalse(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_cold_skip")
        self.assertEqual(decision["prescription_volume_ml"], 0.0)
        self.assertEqual(decision["planned_volume_ml"], 0.0)

    def test_fuzzy_policy_owns_slot_decision_and_request_conversion(self) -> None:
        pot = _pot()
        morning_weather = {"id": 1, "observed_local_at": datetime(2026, 7, 21, 6, 0, tzinfo=LOCAL_TZ), "temperature_c": 28.0}
        weather = {"id": 2, "observed_local_at": datetime(2026, 7, 21, 18, 0, tzinfo=LOCAL_TZ), "temperature_c": 36.0}
        profile = day_profile(max_temperature_c=36.0)
        morning_slot = DEFAULT_FUZZY_POLICY.decision_slot(datetime(2026, 7, 21).date(), morning_weather["observed_local_at"], profile)
        slot = DEFAULT_FUZZY_POLICY.decision_slot(datetime(2026, 7, 21).date(), weather["observed_local_at"], profile)

        decision = DEFAULT_FUZZY_POLICY.make_decision(PotState(moisture=15.0), pot, weather, profile, slot or "morning")
        event = DEFAULT_FUZZY_POLICY.irrigation_request(pot, weather, decision)

        self.assertEqual(morning_slot, "daily_prescription")
        self.assertEqual(slot, "evening")
        self.assertEqual(decision["slot"], "evening")
        self.assertEqual(event["slot"], "evening")
        self.assertGreater(event["planned_volume_ml"], 0.0)


def _pot() -> dict:
    return {
        "id": 1,
        "pot_code": "P1",
        "moisture_min_pct": 20.0,
        "moisture_target_pct": 35.0,
        "winter_moisture_target_pct": 15.0,
        "volume_l": 10.0,
        "retention_factor": 1.0,
        "drip_flow_ml_min": 100.0,
        "size_class": "large",
        "cycle_soak_enabled": False,
        "plant_type_code": "ornamental",
        "heat_sensitive": False,
        "allows_second_watering": False,
        "water_need_level": "medium",
        "sun_exposure": "partial",
        "small_subtype": None,
        "balcony_zone": "west_wall",
    }


def day_profile(max_temperature_c: float = 20.0) -> dict:
    return {
        "avg_temperature_c": max_temperature_c,
        "max_temperature_c": max_temperature_c,
        "precipitation_mm": 0.0,
        "heatwave_day": False,
        "dry_windy_day": False,
    }


if __name__ == "__main__":
    unittest.main()
