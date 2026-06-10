from __future__ import annotations

import unittest
from datetime import datetime

from digital_twin.simulation.dto import LOCAL_TZ, PotState
from digital_twin.simulation.irrigation_controller import (
    DEFAULT_FUZZY_POLICY,
    DEFAULT_IRRIGATION_POLICY,
    _apply_baseline_irrigation_event,
    _make_fuzzy_dt_decision,
)


class BaselineIrrigationControllerTests(unittest.TestCase):
    def test_baseline_event_doses_from_actual_current_moisture(self) -> None:
        state = PotState(moisture=5.0)

        event = _apply_baseline_irrigation_event(
            state,
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
        self.assertAlmostEqual(state.moisture, 35.0, places=2)

    def test_domain_policy_exposes_neutral_irrigation_request(self) -> None:
        event = DEFAULT_IRRIGATION_POLICY.irrigation_request(
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

    def test_fuzzy_keeps_soft_prescription_above_trigger_threshold(self) -> None:
        decision = _make_fuzzy_dt_decision(
            PotState(moisture=38.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 8, 0, tzinfo=LOCAL_TZ), "temperature_c": 20.0},
            _day_profile(),
        )

        self.assertFalse(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_soft_zone_need")
        self.assertGreater(decision["planned_volume_ml"], 0.0)

    def test_fuzzy_prescribes_when_moisture_is_below_trigger_threshold(self) -> None:
        decision = _make_fuzzy_dt_decision(
            PotState(moisture=15.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 8, 0, tzinfo=LOCAL_TZ), "temperature_c": 30.0},
            _day_profile(max_temperature_c=30.0),
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertGreater(decision["planned_volume_ml"], 0.0)

    def test_fuzzy_waters_before_minimum_when_below_comfort_floor(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=24.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ), "temperature_c": 22.0},
            _day_profile(max_temperature_c=22.0),
            "morning",
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_comfort_preserving_prescription")
        self.assertGreaterEqual(decision["fuzzy_comfort_floor_pct"], 29.0)

    def test_fuzzy_does_not_treat_rain_as_comfort_when_moisture_is_low(self) -> None:
        decision = DEFAULT_FUZZY_POLICY.make_decision(
            PotState(moisture=24.0),
            _pot(),
            {"id": 1, "observed_local_at": datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ), "temperature_c": 22.0},
            {**_day_profile(max_temperature_c=22.0), "precipitation_mm": 6.0},
            "morning",
        )

        self.assertTrue(decision["should_irrigate"])
        self.assertEqual(decision["reason_code"], "fuzzy_comfort_preserving_prescription")

    def test_fuzzy_policy_owns_slot_decision_and_request_conversion(self) -> None:
        pot = _pot()
        weather = {"id": 1, "observed_local_at": datetime(2026, 7, 21, 18, 0, tzinfo=LOCAL_TZ), "temperature_c": 36.0}
        day_profile = _day_profile(max_temperature_c=36.0)
        slot = DEFAULT_FUZZY_POLICY.decision_slot(datetime(2026, 7, 21).date(), weather["observed_local_at"], day_profile)

        decision = DEFAULT_FUZZY_POLICY.make_decision(PotState(moisture=15.0), pot, weather, day_profile, slot or "morning")
        event = DEFAULT_FUZZY_POLICY.irrigation_request(pot, weather, decision)

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


def _day_profile(max_temperature_c: float = 20.0) -> dict:
    return {
        "avg_temperature_c": max_temperature_c,
        "max_temperature_c": max_temperature_c,
        "precipitation_mm": 0.0,
        "heatwave_day": False,
        "dry_windy_day": False,
    }


if __name__ == "__main__":
    unittest.main()
