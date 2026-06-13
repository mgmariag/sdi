from __future__ import annotations

import unittest
from datetime import date

from digital_twin.simulation.soil_model import indoor_hourly_moisture_loss


class IndoorMoistureLossTests(unittest.TestCase):
    def test_dormant_indoor_loss_is_lower_than_active_indoor_loss(self) -> None:
        pot = {"plant_type_code": "herbs"}

        self.assertLess(
            indoor_hourly_moisture_loss(pot, date(2026, 1, 15)),
            indoor_hourly_moisture_loss(pot, date(2026, 4, 15)),
        )


if __name__ == "__main__":
    unittest.main()
