from __future__ import annotations

import unittest
from datetime import date

from digital_twin.services.sensor_readings import _indoor_hourly_moisture_loss as sensor_indoor_loss
from digital_twin.simulation.engine import _indoor_hourly_moisture_loss as simulation_indoor_loss


class IndoorMoistureLossTests(unittest.TestCase):
    def test_dormant_indoor_loss_is_lower_than_active_indoor_loss(self) -> None:
        pot = {"plant_type_code": "herbs"}

        self.assertLess(
            simulation_indoor_loss(pot, date(2026, 1, 15)),
            simulation_indoor_loss(pot, date(2026, 4, 15)),
        )
        self.assertEqual(
            simulation_indoor_loss(pot, date(2026, 1, 15)),
            sensor_indoor_loss(pot, date(2026, 1, 15)),
        )


if __name__ == "__main__":
    unittest.main()
