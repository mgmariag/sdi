from __future__ import annotations

import unittest
from datetime import date, datetime

from digital_twin.application.sensors.state_rows import (
    LOCAL_TZ,
    SensorStateProjector,
)
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil


class SensorReadingBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projector = SensorStateProjector()

    def test_generated_sensor_moisture_never_reaches_zero(self) -> None:
        pot = _pot()
        state = {"moisture": 0.0}
        local_day = date(2025, 12, 18)

        self.projector.apply_hourly_environment(state, pot, _weather(), local_day)
        row = self.projector.sensor_row(
            pot,
            state,
            _weather(),
            datetime(2025, 12, 18, 14, 0, tzinfo=LOCAL_TZ),
            "simulated_sensor",
        )

        self.assertGreaterEqual(state["moisture"], soil.minimum_realistic_moisture(pot, local_day))
        self.assertGreaterEqual(row["soil_moisture_pct"], soil.minimum_realistic_moisture(pot, local_day))

    def test_virtual_irrigation_uses_pot_delivery_physics(self) -> None:
        pot = {
            **_pot(),
            "moisture_min_pct": 20.0,
            "moisture_target_pct": 35.0,
            "winter_moisture_target_pct": 15.0,
            "volume_l": 2.2,
            "retention_factor": 0.6,
            "drip_flow_ml_min": 8.0,
            "size_class": "small",
            "cycle_soak_enabled": True,
        }
        state = {"moisture": 10.0}

        self.projector.apply_virtual_irrigation_if_due(
            state,
            pot,
            _weather(),
            datetime(2026, 7, 21, 7, 0, tzinfo=LOCAL_TZ),
        )

        self.assertAlmostEqual(state["moisture"], 14.36, places=2)


def _pot() -> dict:
    return {
        "id": 176,
        "plant_type_code": "vegetables",
        "winter_moisture_target_pct": 15.0,
        "moisture_min_pct": 20.0,
        "moisture_target_pct": 35.0,
        "evaporation_factor": 1.1,
        "rain_exposure": "partially_exposed",
        "sun_exposure": "reflected_heat",
        "wind_exposure": "moderate",
        "container_material": "plastic",
        "size_class": "small",
    }


def _weather() -> dict:
    return {
        "temperature_c": 3.0,
        "relative_humidity_pct": 70.0,
        "wind_speed_kmh": 5.0,
        "precipitation_mm": 0.0,
        "evapotranspiration_mm": 0.4,
    }


if __name__ == "__main__":
    unittest.main()
