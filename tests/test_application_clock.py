from __future__ import annotations

import unittest
from datetime import date

from digital_twin.application.clock import ApplicationClock


class ApplicationClockTests(unittest.TestCase):
    def test_add_months_clamps_to_last_day_of_target_month(self) -> None:
        self.assertEqual(ApplicationClock.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertEqual(ApplicationClock.add_months(date(2024, 1, 31), 1), date(2024, 2, 29))


if __name__ == "__main__":
    unittest.main()
