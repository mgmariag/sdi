from __future__ import annotations

import unittest
from datetime import date

from digital_twin.application.sensors.availability import SensorAvailabilityService


class SensorAvailabilityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SensorAvailabilityService()

    def test_maps_experiment_date_to_latest_available_same_month_day(self) -> None:
        mapped_date = self.service.map_experiment_date_to_sensor_date(
            date(2026, 6, 14),
            date(2024, 5, 1),
            date(2025, 8, 31),
        )

        self.assertEqual(mapped_date, date(2025, 6, 14))

    def test_prefers_exact_available_sensor_date(self) -> None:
        available_dates = {date(2024, 6, 14), date(2025, 6, 14)}

        mapped_date = self.service.map_experiment_date_to_available_sensor_date(
            date(2025, 6, 14),
            available_dates,
        )

        self.assertEqual(mapped_date, date(2025, 6, 14))

    def test_falls_back_to_latest_same_month_day_available_date(self) -> None:
        available_dates = {date(2024, 6, 14), date(2025, 6, 14)}

        mapped_date = self.service.map_experiment_date_to_available_sensor_date(
            date(2026, 6, 14),
            available_dates,
        )

        self.assertEqual(mapped_date, date(2025, 6, 14))


if __name__ == "__main__":
    unittest.main()
