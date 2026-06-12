from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta

from digital_twin.experiments.anfis import ANFIS, DEFAULT_INPUTS
from digital_twin.services.anfis_model_service import (
    DEFAULT_ANFIS_GENERATIONS,
    DEFAULT_ANFIS_POPULATION,
    AnfisModelService,
)
from digital_twin.services import experiment_service
from digital_twin.simulation import engine


class AnfisTrainingSignalTests(unittest.TestCase):
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
        signals, weight = engine._anfis_training_signals(
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

    def test_moisture_safe_savings_discounts_water_savings_by_comfort(self) -> None:
        metrics = engine._moisture_safe_savings_metrics(
            [
                {"baseline_moisture": 40.0, "anfis_moisture": 38.0},
                {"baseline_moisture": 45.0, "anfis_moisture": 40.0},
                {"baseline_moisture": 45.0, "anfis_moisture": 39.0},
            ],
            "anfis",
            [_pot()],
            10.0,
        )

        self.assertEqual(metrics["comfort_threshold_pct"], 40.0)
        self.assertEqual(metrics["comfort_preserved_days"], 2)
        self.assertAlmostEqual(metrics["comfort_preserved_percent"], 66.67)
        self.assertAlmostEqual(metrics["moisture_safe_savings_percent"], 6.67)

    def test_water_saving_policy_raises_operating_thresholds(self) -> None:
        policy = engine.ANFIS_WATER_SAVING_POLICY

        self.assertAlmostEqual(policy.threshold(0.72), 0.76)
        self.assertAlmostEqual(policy.threshold(0.78, forecast=True), 0.82)

    def test_near_threshold_light_deficit_gets_half_dose(self) -> None:
        dose_factor = engine._anfis_zone_dose_factor(
            [
                {
                    "target_moisture_pct": 40.0,
                    "current_moisture_pct": 37.0,
                    "anfis_below_safety_threshold": False,
                }
            ],
            slot_probability=0.77,
            day_profile={"precipitation_mm": 0.0},
            decision_threshold=0.76,
        )

        self.assertEqual(dose_factor, 0.5)

    def test_safety_override_keeps_partial_dose_with_runtime_guard(self) -> None:
        dose_factor = engine._anfis_zone_dose_factor(
            [
                {
                    "target_moisture_pct": 40.0,
                    "current_moisture_pct": 31.0,
                    "anfis_below_safety_threshold": True,
                }
            ],
            slot_probability=0.77,
            day_profile={"precipitation_mm": 3.0},
            decision_threshold=0.76,
        )

        self.assertEqual(dose_factor, 0.75)

    def test_high_confidence_severe_anfis_signal_can_use_full_dose(self) -> None:
        dose_factor = engine._anfis_zone_dose_factor(
            [
                {
                    "target_moisture_pct": 45.0,
                    "current_moisture_pct": 28.0,
                    "anfis_below_safety_threshold": False,
                }
            ],
            slot_probability=0.99,
            day_profile={"precipitation_mm": 0.0},
            decision_threshold=0.76,
        )

        self.assertEqual(dose_factor, 1.0)

    def test_recent_non_safety_zone_is_blocked_by_signal_cadence(self) -> None:
        today = date(2026, 6, 6)
        blocked = engine._anfis_zone_cadence_blocks(
            {"west_wall": today - timedelta(days=1)},
            "west_wall",
            today,
            [
                {
                    "target_moisture_pct": 40.0,
                    "current_moisture_pct": 34.0,
                    "anfis_below_safety_threshold": False,
                }
            ],
            slot_probability=0.86,
            day_profile={
                "avg_temperature_c": 21.0,
                "precipitation_mm": 0.7,
                "heatwave_day": False,
                "dry_windy_day": False,
            },
        )

        self.assertTrue(blocked)

    def test_high_probability_bypasses_anfis_cadence_without_weather_rules(self) -> None:
        today = date(2026, 6, 6)

        blocked = engine._anfis_zone_cadence_blocks(
            {"west_wall": today - timedelta(days=1)},
            "west_wall",
            today,
            [
                {
                    "target_moisture_pct": 45.0,
                    "current_moisture_pct": 31.0,
                    "anfis_below_safety_threshold": False,
                }
            ],
            slot_probability=0.99,
            day_profile={
                "avg_temperature_c": 29.0,
                "precipitation_mm": 0.0,
                "heatwave_day": False,
                "dry_windy_day": False,
            },
        )

        self.assertFalse(blocked)

    def test_rain_and_temperature_are_learned_through_anfis_target_probability(self) -> None:
        dry_warm_no_rain = engine._anfis_training_target_probability(
            {
                "moisture": 34.0,
                "temperature": 29.0,
                "rain": 0.0,
            }
        )
        same_moisture_heavy_rain = engine._anfis_training_target_probability(
            {
                "moisture": 34.0,
                "temperature": 29.0,
                "rain": 8.0,
            }
        )

        self.assertGreater(dry_warm_no_rain, 0.76)
        self.assertLess(same_moisture_heavy_rain, 0.76)

    def test_fuzzy_actuation_uses_daily_prescription_not_cadence(self) -> None:
        summary = engine.FUZZY_ACTUATION_POLICY.summary()

        self.assertEqual(summary["cadence_policy"], "not_used")
        self.assertEqual(
            summary["daily_execution_policy"],
            "one_daily_prescription_plus_heatwave_evening_supplement",
        )
        self.assertEqual(summary["heatwave_supplement_policy"], "safety_floor_or_fuzzy_prescription_signal")
        self.assertTrue(
            engine.FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 36.0, "heatwave_day": False},
                [{"current_moisture_pct": 28.0, "fuzzy_safety_floor_pct": 30.0}],
            )
        )
        self.assertTrue(
            engine.FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 36.0, "heatwave_day": False},
                [{"current_moisture_pct": 34.0, "fuzzy_safety_floor_pct": 30.0, "prescription_mm": 1.2}],
            )
        )
        self.assertFalse(
            engine.FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 36.0, "heatwave_day": False},
                [{"current_moisture_pct": 34.0, "fuzzy_safety_floor_pct": 30.0}],
            )
        )
        self.assertFalse(
            engine.FUZZY_ACTUATION_POLICY.allows_heatwave_supplement(
                "evening",
                {"max_temperature_c": 28.0, "heatwave_day": False},
                [{"current_moisture_pct": 28.0, "fuzzy_safety_floor_pct": 30.0}],
            )
        )

    def test_trained_anfis_controller_round_trips_through_payload(self) -> None:
        controller = engine._AnfisModelController(
            global_model=engine.ANFIS(),
            global_calibrator=engine._AnfisProbabilityCalibrator([(0.2, 0.25), (0.8, 0.75)]),
            zone_models={"west_wall": engine.ANFIS()},
            zone_calibrators={"west_wall": engine._AnfisProbabilityCalibrator([(0.2, 0.3), (0.8, 0.7)])},
        )
        inputs = {
            "moisture": 34.0,
            "temperature": 31.0,
            "rain": 0.0,
        }

        restored = engine.deserialize_trained_anfis_model(engine.serialize_trained_anfis_model(controller))

        self.assertAlmostEqual(restored.predict(inputs, "west_wall"), controller.predict(inputs, "west_wall"))

    def test_database_anfis_dataset_uses_exact_sensor_reading_once(self) -> None:
        reading_date = date(2026, 6, 1)
        slot = time(5, 30)
        recorded_at = datetime.combine(reading_date, slot, tzinfo=engine.LOCAL_TZ)
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

        dataset = engine._generate_database_anfis_dataset(
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
                "training_dataset_version": engine.ANFIS_TRAINING_DATASET_VERSION,
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
            "training_dataset_version": engine.ANFIS_TRAINING_DATASET_VERSION,
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
                "training_dataset_version": engine.ANFIS_TRAINING_DATASET_VERSION - 1,
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
        current_year = experiment_service.today_local().year

        self.assertEqual(
            experiment_service._resolve_anfis_model_key(
                date(current_year, 1, 1),
                date(current_year, 6, 1),
            ),
            "anfis-default",
        )
        self.assertEqual(
            experiment_service._resolve_anfis_model_key(
                date(2023, 5, 1),
                date(2023, 6, 1),
            ),
            "anfis-2023-simulated",
        )
        self.assertEqual(
            experiment_service._resolve_anfis_model_key(
                date(2025, 5, 1),
                date(2025, 6, 1),
            ),
            "anfis-default",
        )

    def test_non_default_anfis_model_can_use_its_own_ga_budget(self) -> None:
        metadata = {
            "training_sample_policy": "all_available_sensor_readings",
            "training_dataset_version": engine.ANFIS_TRAINING_DATASET_VERSION,
            "seed": 2026,
            "generations": 20,
            "population": 16,
            "anfis_input_features": list(DEFAULT_INPUTS),
        }

        self.assertFalse(experiment_service._persisted_anfis_model_matches(metadata, 2026))
        self.assertTrue(
            experiment_service._persisted_anfis_model_matches(
                metadata,
                2026,
                strict_training_config=False,
            )
        )


def _pot() -> dict:
    return {
        "moisture_min_pct": 30.0,
        "moisture_target_pct": 40.0,
        "balcony_zone": "west_wall",
    }


if __name__ == "__main__":
    unittest.main()
