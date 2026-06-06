from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from digital_twin.simulation.engine import _state_simulation_start


class StateLookbackTests(unittest.TestCase):
    def test_range_after_anchor_starts_at_stable_historical_anchor(self) -> None:
        start = date(2026, 4, 22)
        end = date(2026, 5, 23)

        with patch("digital_twin.simulation.engine._historical_state_anchor_date", return_value=date(2025, 6, 4)):
            self.assertEqual(_state_simulation_start(start, end), date(2025, 6, 4))

    def test_range_before_anchor_starts_at_requested_start(self) -> None:
        start = date(2025, 5, 20)
        end = date(2025, 5, 23)

        with patch("digital_twin.simulation.engine._historical_state_anchor_date", return_value=date(2025, 6, 4)):
            self.assertEqual(_state_simulation_start(start, end), start)


if __name__ == "__main__":
    unittest.main()
