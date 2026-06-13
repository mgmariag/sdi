from __future__ import annotations

import unittest

from digital_twin.simulation.metrics import moisture_safe_savings_metrics


class MetricsTests(unittest.TestCase):
    def test_moisture_safe_savings_discounts_water_savings_by_comfort(self) -> None:
        metrics = moisture_safe_savings_metrics(
            [
                {"baseline_moisture": 40.0, "anfis_moisture": 38.0},
                {"baseline_moisture": 45.0, "anfis_moisture": 40.0},
                {"baseline_moisture": 45.0, "anfis_moisture": 39.0},
            ],
            "anfis",
            [_pot()],
            10.0,
        )

        self.assertEqual(metrics["comfort_threshold_pct"], 40.0)
        self.assertEqual(metrics["comfort_preserved_days"], 2)
        self.assertAlmostEqual(metrics["comfort_preserved_percent"], 66.67)
        self.assertAlmostEqual(metrics["moisture_safe_savings_percent"], 6.67)


def _pot() -> dict:
    return {
        "moisture_min_pct": 30.0,
        "moisture_target_pct": 40.0,
        "balcony_zone": "west_wall",
    }


if __name__ == "__main__":
    unittest.main()
