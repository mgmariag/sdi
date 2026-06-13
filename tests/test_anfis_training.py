from __future__ import annotations

import unittest
from datetime import date, datetime, time

from digital_twin.simulation.anfis.model import ANFIS, DEFAULT_INPUTS
from digital_twin.simulation.anfis.modeling import (
    AnfisModelController,
    AnfisProbabilityCalibrator,
    anfis_training_signals,
    anfis_training_target_probability,
    deserialize_trained_anfis_model,
    generate_database_anfis_dataset,
    serialize_trained_anfis_model,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ


class AnfisTrainingTests(unittest.TestCase):
    def test_default_anfis_uses_three_weather_aware_inputs(self) -> None:
        model = ANFIS()

        self.assertEqual(model.input_names, list(DEFAULT_INPUTS))
        self.assertEqual(model.input_names, ["moisture", "temperature", "rain"])
        self.assertEqual(len(model.rule_indices), 27)
        for input_index in range(len(DEFAULT_INPUTS)):
            self.assertEqual(
                {rule[input_index] for rule in model.rule_indices},
                {0, 1, 2},
            )

    def test_dry_hot_no_rain_and_recovery_reading_get_extra_weight(self) -> None:
        signals, weight = anfis_training_signals(
            _pot(),
            {"soil_moisture_pct": 39.0},
            {"max_temperature_c": 33.0, "precipitation_mm": 0.0},
            {"soil_moisture_pct": 35.0},
            time(7, 30),
        )

        self.assertIn("real_sensor_reading", signals)
        self.assertIn("dry_hot_no_rain", signals)
        self.assertIn("post_irrigation_recovery", signals)
        self.assertGreaterEqual(weight, 4.0)

    def test_rain_and_temperature_are_learned_through_anfis_target_probability(self) -> None:
        dry_warm_no_rain = anfis_training_target_probability(
            {
                "moisture": 34.0,
                "temperature": 29.0,
                "rain": 0.0,
            }
        )
        same_moisture_heavy_rain = anfis_training_target_probability(
            {
                "moisture": 34.0,
                "temperature": 29.0,
                "rain": 8.0,
            }
        )

        self.assertGreater(dry_warm_no_rain, 0.76)
        self.assertLess(same_moisture_heavy_rain, 0.76)

    def test_trained_anfis_controller_round_trips_through_payload(self) -> None:
        controller = AnfisModelController(
            global_model=ANFIS(),
            global_calibrator=AnfisProbabilityCalibrator([(0.2, 0.25), (0.8, 0.75)]),
            zone_models={"west_wall": ANFIS()},
            zone_calibrators={"west_wall": AnfisProbabilityCalibrator([(0.2, 0.3), (0.8, 0.7)])},
        )
        inputs = {
            "moisture": 34.0,
            "temperature": 31.0,
            "rain": 0.0,
        }

        restored = deserialize_trained_anfis_model(serialize_trained_anfis_model(controller))

        self.assertAlmostEqual(restored.predict(inputs, "west_wall"), controller.predict(inputs, "west_wall"))

    def test_database_anfis_dataset_uses_exact_sensor_reading_once(self) -> None:
        reading_date = date(2026, 6, 1)
        slot = time(5, 30)
        recorded_at = datetime.combine(reading_date, slot, tzinfo=LOCAL_TZ)
        sensor_reading = {
            "sensor_id": 1,
            "local_date": reading_date,
            "local_time": slot,
            "recorded_at": recorded_at,
            "soil_moisture_pct": 34.0,
            "air_temperature_c": 24.0,
        }
        weather = {
            "observed_local_at": recorded_at,
            "temperature_c": 24.0,
            "relative_humidity_pct": 58.0,
            "precipitation_mm": 0.0,
            "wind_speed_kmh": 8.0,
            "wind_gust_kmh": 12.0,
            "evapotranspiration_mm": 0.05,
        }

        dataset = generate_database_anfis_dataset(
            [weather],
            [{**_pot(), "id": 1}],
            0,
            2026,
            {
                "available": True,
                "lookup": {
                    (reading_date, slot, 1): sensor_reading,
                    (reading_date, 6, 1): sensor_reading,
                },
            },
            {reading_date: [weather]},
            {},
        )

        self.assertEqual(len(dataset), 1)


def _pot() -> dict:
    return {
        "moisture_min_pct": 30.0,
        "moisture_target_pct": 40.0,
        "balcony_zone": "west_wall",
    }


if __name__ == "__main__":
    unittest.main()
