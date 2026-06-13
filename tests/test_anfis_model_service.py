from __future__ import annotations

import unittest
from datetime import date

import digital_twin.application.experiments.experiment_service as experiments
from digital_twin.application.anfis_training.anfis_training_service import (
    DEFAULT_ANFIS_GENERATIONS,
    DEFAULT_ANFIS_POPULATION,
    AnfisModelService,
)
from digital_twin.simulation.anfis.model import DEFAULT_INPUTS
from digital_twin.simulation.anfis.modeling import ANFIS_TRAINING_DATASET_VERSION


class AnfisModelServiceTests(unittest.TestCase):
    def test_anfis_default_training_range_uses_one_year_history(self) -> None:
        start, end = AnfisModelService._resolve_training_range(None, date(2026, 6, 26))

        self.assertEqual(start, date(2025, 6, 26))
        self.assertEqual(end, date(2026, 6, 26))

    def test_anfis_staleness_detects_new_sensor_watermark(self) -> None:
        latest = {
            "sensor_source": "simulated_sensor",
            "sensor_reading_count": 10,
            "sensor_readings_max_recorded_at": "2026-06-01T06:00:00",
            "metrics": {
                "training_sample_policy": "all_available_sensor_readings",
                "training_dataset_version": ANFIS_TRAINING_DATASET_VERSION,
                "anfis_input_features": list(DEFAULT_INPUTS),
            },
            "seed": 2026,
            "generations": DEFAULT_ANFIS_GENERATIONS,
            "population": DEFAULT_ANFIS_POPULATION,
        }
        config = {
            "sensor_source": "simulated_sensor",
            "seed": 2026,
            "generations": DEFAULT_ANFIS_GENERATIONS,
            "population": DEFAULT_ANFIS_POPULATION,
            "training_dataset_version": ANFIS_TRAINING_DATASET_VERSION,
            "anfis_input_features": list(DEFAULT_INPUTS),
        }

        self.assertFalse(
            AnfisModelService._needs_training(
                latest,
                {
                    "sensor_reading_count": 10,
                    "sensor_readings_max_recorded_at": "2026-06-01T06:00:00",
                },
                config,
            )
        )
        stale_dataset = {
            **latest,
            "metrics": {
                **latest["metrics"],
                "training_dataset_version": ANFIS_TRAINING_DATASET_VERSION - 1,
            },
        }
        self.assertTrue(
            AnfisModelService._needs_training(
                stale_dataset,
                {
                    "sensor_reading_count": 10,
                    "sensor_readings_max_recorded_at": "2026-06-01T06:00:00",
                },
                config,
            )
        )
        stale_features = {
            **latest,
            "metrics": {
                **latest["metrics"],
                "anfis_input_features": [
                    "moisture",
                    "temperature",
                    "rain",
                    "drying_demand_index",
                    "container_retention_index",
                    "plant_water_need_index",
                    "moisture_trend",
                ],
            },
        }
        self.assertTrue(
            AnfisModelService._needs_training(
                stale_features,
                {
                    "sensor_reading_count": 10,
                    "sensor_readings_max_recorded_at": "2026-06-01T06:00:00",
                },
                config,
            )
        )
        self.assertTrue(
            AnfisModelService._needs_training(
                latest,
                {
                    "sensor_reading_count": 11,
                    "sensor_readings_max_recorded_at": "2026-06-01T06:15:00",
                },
                config,
            )
        )

    def test_anfis_model_key_is_selected_from_start_year(self) -> None:
        current_year = experiments.today_local().year

        self.assertEqual(
            experiments._resolve_anfis_model_key(
                date(current_year, 1, 1),
                date(current_year, 6, 1),
            ),
            "anfis-default",
        )
        self.assertEqual(
            experiments._resolve_anfis_model_key(
                date(2023, 5, 1),
                date(2023, 6, 1),
            ),
            "anfis-2023-simulated",
        )
        self.assertEqual(
            experiments._resolve_anfis_model_key(
                date(2025, 5, 1),
                date(2025, 6, 1),
            ),
            "anfis-default",
        )

    def test_non_default_anfis_model_can_use_its_own_ga_budget(self) -> None:
        metadata = {
            "training_sample_policy": "all_available_sensor_readings",
            "training_dataset_version": ANFIS_TRAINING_DATASET_VERSION,
            "seed": 2026,
            "generations": 20,
            "population": 16,
            "anfis_input_features": list(DEFAULT_INPUTS),
        }

        self.assertFalse(experiments._persisted_anfis_model_matches(metadata, 2026))
        self.assertTrue(
            experiments._persisted_anfis_model_matches(
                metadata,
                2026,
                strict_training_config=False,
            )
        )


if __name__ == "__main__":
    unittest.main()

