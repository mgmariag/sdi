from __future__ import annotations

import unittest
from datetime import date

from digital_twin.simulation.engine import SimulationEngine


class StateLookbackTests(unittest.TestCase):
    def test_active_range_starts_at_current_season_start(self) -> None:
        start = date(2026, 4, 22)
        end = date(2026, 5, 23)
        engine = _AnchoredSimulationEngine(date(2025, 6, 4))

        self.assertEqual(engine.state_simulation_start(start, end), date(2026, 4, 1))

    def test_active_range_uses_first_available_state_when_season_data_starts_later(self) -> None:
        start = date(2026, 7, 20)
        end = date(2026, 7, 23)
        engine = _AnchoredSimulationEngine(date(2026, 6, 4))

        self.assertEqual(engine.state_simulation_start(start, end), date(2026, 6, 4))

    def test_active_range_starting_before_season_start_keeps_requested_start(self) -> None:
        start = date(2026, 3, 20)
        end = date(2026, 5, 23)
        engine = _AnchoredSimulationEngine(date(2025, 6, 4))

        self.assertEqual(engine.state_simulation_start(start, end), start)

    def test_dormant_range_does_not_warm_up(self) -> None:
        start = date(2026, 2, 1)
        end = date(2026, 2, 28)
        engine = _AnchoredSimulationEngine(date(2025, 6, 4))

        self.assertEqual(engine.state_simulation_start(start, end), start)


class _AnchoredSimulationEngine(SimulationEngine):
    def __init__(self, anchor_date: date) -> None:
        super().__init__()
        self.anchor_date = anchor_date

    def historical_state_anchor_date(self, end_date: date) -> date | None:
        return self.anchor_date


if __name__ == "__main__":
    unittest.main()
