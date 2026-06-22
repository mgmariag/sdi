from __future__ import annotations

import unittest
from datetime import date, datetime, time

from digital_twin.application.sensors.reading_cadence import (
    LOCAL_TZ,
    SensorReadingCadence,
)


class SensorReadingCadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cadence = SensorReadingCadence(
            local_tz=LOCAL_TZ,
            daily_reading_times=(time(1, 0), time(5, 30)),
        )

    def test_next_scheduled_datetime_uses_next_slot_today(self) -> None:
        now = datetime(2026, 6, 14, 1, 30, tzinfo=LOCAL_TZ)

        next_run = self.cadence.next_scheduled_datetime(now)

        self.assertEqual(next_run, datetime(2026, 6, 14, 5, 30, tzinfo=LOCAL_TZ))

    def test_next_scheduled_datetime_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 6, 14, 6, 0, tzinfo=LOCAL_TZ)

        next_run = self.cadence.next_scheduled_datetime(now)

        self.assertEqual(next_run, datetime(2026, 6, 15, 1, 0, tzinfo=LOCAL_TZ))

    def test_align_to_interval_preserves_local_time_bucket(self) -> None:
        aligned = self.cadence.align_to_interval(
            datetime(2026, 6, 14, 5, 44, 30, tzinfo=LOCAL_TZ),
            15,
        )

        self.assertEqual(aligned, datetime(2026, 6, 14, 5, 30, tzinfo=LOCAL_TZ))

    def test_same_month_day_handles_leap_day(self) -> None:
        self.assertEqual(
            self.cadence.same_month_day(2025, date(2024, 2, 29)),
            date(2025, 2, 28),
        )

    def test_retention_summary_uses_cadence_slots(self) -> None:
        summary = self.cadence.retention_summary()

        self.assertEqual(summary["raw_hours"], 24)
        self.assertEqual(summary["hourly_days"], 7)
        self.assertEqual(summary["daily_days"], 366)
        self.assertEqual(summary["daily_reading_times"], ["01:00", "05:30"])
        self.assertGreaterEqual(summary["reading_interval_minutes"], 1)


if __name__ == "__main__":
    unittest.main()
