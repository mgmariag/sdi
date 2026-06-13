from __future__ import annotations

import unittest

from digital_twin.simulation.fuzzy.execution import FUZZY_ACTUATION_POLICY


class FuzzyActuationPolicyTests(unittest.TestCase):
    def test_fuzzy_actuation_summary_describes_daily_execution(self) -> None:
        summary = FUZZY_ACTUATION_POLICY.summary()

        self.assertEqual(
            summary["daily_execution_policy"],
            "one_daily_volume_prescription_plus_heatwave_evening_supplement",
        )
        self.assertEqual(summary["heatwave_supplement_policy"], "hot_evening_for_triggered_zone")

    def test_heatwave_supplement_depends_on_hot_evening_slot(self) -> None:
        self.assertTrue(
            FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 36.0, "heatwave_day": False},
            )
        )
        self.assertTrue(
            FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 28.0, "heatwave_day": True},
            )
        )
        self.assertFalse(
            FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 28.0, "heatwave_day": False},
            )
        )
        self.assertFalse(
            FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "daily_prescription",
                {"max_temperature_c": 36.0, "heatwave_day": False},
            )
        )

    def test_safety_need_uses_shorter_minimum_runtime(self) -> None:
        self.assertEqual(
            FUZZY_ACTUATION_POLICY.minimum_runtime(
                [{"current_moisture_pct": 28.0, "fuzzy_safety_floor_pct": 30.0}]
            ),
            0.5,
        )
        self.assertEqual(
            FUZZY_ACTUATION_POLICY.minimum_runtime(
                [{"current_moisture_pct": 34.0, "fuzzy_safety_floor_pct": 30.0}]
            ),
            2.0,
        )


if __name__ == "__main__":
    unittest.main()
