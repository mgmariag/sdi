from __future__ import annotations

import unittest
from datetime import date, datetime, time
from unittest.mock import patch

from digital_twin.application.sensor_history.readings.core import ACTUAL_SENSOR_SOURCE
from digital_twin.simulation import engine
from digital_twin.simulation.anfis.model import ANFIS
from digital_twin.simulation.anfis.modeling import (
    AnfisModelController,
    AnfisProbabilityCalibrator,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import (
    ExperimentSnapshot,
    PotState,
)
from digital_twin.simulation.state.sensor_calibration import (
    apply_sensor_calibration_marker,
)
from digital_twin.simulation.state.sensor_context import sensor_control_pots


class StrategySensorAnchorPolicyTests(unittest.TestCase):
    def test_fuzzy_dt_uses_initial_sensor_anchor_without_continuous_calibration(self) -> None:
        snapshot = _snapshot()

        with patch(
            "digital_twin.simulation.engine.resolve_simulation_snapshot",
            side_effect=AssertionError("strategy comparison should start from selected snapshot"),
        ), patch(
            "digital_twin.simulation.state.sensor_calibration.apply_sensor_calibration_marker",
            side_effect=AssertionError("continuous calibration should not run"),
        ):
            result = engine.run_fuzzy_dt_daily_irrigation_with_snapshot(
                snapshot.start_date,
                snapshot.end_date,
                snapshot=snapshot,
            )

        self.assertEqual(result["summary"]["fuzzySensorCalibrationPolicy"], "initial_state_only")
        self.assertEqual(
            result["summary"]["stateSimulationStartDate"],
            snapshot.start_date.isoformat(),
        )
        self.assertEqual(result["summary"]["stateAnchorPolicy"], "experiment_start_sensor_anchor")
        self.assertEqual(result["summary"]["stateSensorAnchor"]["anchored_pots"], 1)

    def test_anfis_uses_initial_sensor_anchor_without_continuous_calibration(self) -> None:
        snapshot = _snapshot()

        with patch(
            "digital_twin.simulation.engine.resolve_simulation_snapshot",
            side_effect=AssertionError("strategy comparison should start from selected snapshot"),
        ), patch(
            "digital_twin.simulation.state.sensor_calibration.apply_sensor_calibration_marker",
            side_effect=AssertionError("continuous calibration should not run"),
        ):
            result = engine.run_anfis_daily_irrigation_with_snapshot(
                snapshot.start_date,
                snapshot.end_date,
                ANFIS(),
                snapshot=snapshot,
            )

        self.assertEqual(result["summary"]["anfisSensorCalibrationPolicy"], "initial_state_only")
        self.assertEqual(
            result["summary"]["stateSimulationStartDate"],
            snapshot.start_date.isoformat(),
        )
        self.assertEqual(result["summary"]["stateAnchorPolicy"], "experiment_start_sensor_anchor")
        self.assertEqual(result["summary"]["anfisSensorStateAnchor"]["anchored_pots"], 1)

    def test_fuzzy_comparison_baseline_uses_calibrated_reference_policy(self) -> None:
        snapshot = _snapshot()

        with patch(
            "digital_twin.simulation.engine.state_simulation_start",
            return_value=snapshot.start_date,
        ), patch(
            "digital_twin.simulation.baseline.execution.sensor_calibration.apply_sensor_calibration_marker",
            wraps=apply_sensor_calibration_marker,
        ) as calibration_marker:
            result = engine.run_daily_fuzzy_dt_experiment(
                snapshot.start_date,
                snapshot.end_date,
                snapshot=snapshot,
            )

        self.assertEqual(
            result["summary"]["comparisonBaselineStateAnchorPolicy"],
            "stable_historical_timeline",
        )
        self.assertEqual(
            result["summary"]["comparisonBaselineSensorCalibrationPolicy"],
            "continuous_sensor_calibration",
        )
        self.assertGreater(calibration_marker.call_count, 0)

    def test_anfis_comparison_baseline_uses_calibrated_reference_policy(self) -> None:
        snapshot = _snapshot()

        with patch(
            "digital_twin.simulation.engine.state_simulation_start",
            return_value=snapshot.start_date,
        ), patch(
            "digital_twin.simulation.baseline.execution.sensor_calibration.apply_sensor_calibration_marker",
            wraps=apply_sensor_calibration_marker,
        ) as calibration_marker:
            result = engine.run_daily_anfis_experiment(
                snapshot.start_date,
                snapshot.end_date,
                snapshot=snapshot,
                trained_model=_anfis_controller(),
                training_metadata={"evaluation": {}, "seed": 2026},
            )

        self.assertEqual(
            result["summary"]["comparisonBaselineStateAnchorPolicy"],
            "stable_historical_timeline",
        )
        self.assertEqual(
            result["summary"]["comparisonBaselineSensorCalibrationPolicy"],
            "continuous_sensor_calibration",
        )
        self.assertGreater(calibration_marker.call_count, 0)

    def test_sampling_reuses_baseline_warmed_start_state(self) -> None:
        snapshot = _snapshot()
        baseline_result = {
            "summary": {
                "stateSimulationStartDate": "2026-04-01",
                "stateLookbackDays": 54,
                "baselineSensorCalibrationPolicy": "continuous_sensor_calibration",
            },
            "stateAtExperimentStart": {
                "1": {
                    "moisture": 37.5,
                    "too_wet_hours": 2,
                },
            },
        }

        with patch(
            "digital_twin.simulation.engine.resolve_simulation_snapshot",
            side_effect=AssertionError("sampling should reuse baseline warm-up state"),
        ):
            result = engine.run_sparse_daily_irrigation_with_snapshot(
                snapshot.start_date,
                snapshot.end_date,
                sample_interval_hours=48,
                snapshot=snapshot,
                baseline_result=baseline_result,
            )

        summary = result["summary"]
        self.assertEqual(summary["stateAnchorPolicy"], "baseline_warmup_reuse")
        self.assertEqual(summary["samplingWarmupReusePolicy"], "baseline_start_state_reuse")
        self.assertEqual(summary["stateSimulationStartDate"], snapshot.start_date.isoformat())
        self.assertEqual(summary["stateSensorAnchor"]["source"], "baseline_warmup_reuse")
        self.assertEqual(summary["stateSensorAnchor"]["anchored_pots"], 1)
        self.assertEqual(summary["baselineWarmupStateSimulationStartDate"], "2026-04-01")

    def test_fuzzy_and_anfis_first_point_uses_shared_experiment_start_state(self) -> None:
        snapshot = _snapshot()
        baseline_result = _baseline_result_for_shared_t0(snapshot, shared_moisture=37.5)

        fuzzy = engine.run_daily_fuzzy_dt_experiment(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
            baseline_result=baseline_result,
        )
        anfis = engine.run_daily_anfis_experiment(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
            baseline_result=baseline_result,
            trained_model=_anfis_controller(),
            training_metadata={"evaluation": {}, "seed": 2026},
        )

        self.assertEqual(fuzzy["entries"][0]["baseline_moisture"], 37.5)
        self.assertEqual(fuzzy["entries"][0]["fuzzy_moisture"], 37.5)
        self.assertEqual(anfis["entries"][0]["baseline_moisture"], 37.5)
        self.assertEqual(anfis["entries"][0]["anfis_moisture"], 37.5)
        self.assertEqual(fuzzy["summary"]["firstPointAlignmentPolicy"], "shared_experiment_start_state")
        self.assertEqual(anfis["summary"]["firstPointAlignmentPolicy"], "shared_experiment_start_state")
        anfis_mismatches = [
            entry
            for entry in anfis["entries"]
            if entry["baseline_irrigation_active"] != entry["anfis_irrigation_active"]
        ]
        self.assertEqual(
            anfis["summary"]["baseline_agreement_percent"],
            round((len(anfis["entries"]) - len(anfis_mismatches)) / len(anfis["entries"]) * 100.0, 2),
        )
        self.assertEqual(anfis["summary"]["baseline_mismatch_days"], len(anfis_mismatches))

    def test_baseline_ignores_non_sensor_pot_as_decision_trigger(self) -> None:
        snapshot = _two_pot_snapshot(sensor_moisture=45.0, nonsensor_moisture=8.0)

        result = engine.run_default_dt_irrigation_control(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
            state_anchor_policy="experiment_start_sensor_anchor",
        )

        self.assertEqual(result["summary"]["controllerInputPolicy"], "sensor_locations_only")
        self.assertEqual(result["summary"]["sensorDecisionPotCount"], 1)
        self.assertEqual(result["summary"]["potIrrigationDecisions"], 1)
        self.assertEqual(result["summary"]["valveRuns"], 0)

    def test_sensor_pot_trigger_waters_whole_valve_zone(self) -> None:
        snapshot = _two_pot_snapshot(sensor_moisture=20.0, nonsensor_moisture=45.0)

        result = engine.run_default_dt_irrigation_control(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
            state_anchor_policy="experiment_start_sensor_anchor",
        )

        self.assertEqual(result["summary"]["potIrrigationDecisions"], 1)
        self.assertEqual(result["summary"]["valveRuns"], 1)
        self.assertEqual(result["sampleEvents"][0]["trigger_pot_ids"], [1])
        self.assertEqual(result["sampleEvents"][0]["trigger_sensor_ids"], [1])
        self.assertEqual(result["sampleEvents"][0]["affected_pot_ids"], [1, 2])
        self.assertEqual(len(result["samplePotEvents"]), 2)
        self.assertEqual(
            {event["zone_runtime_policy"] for event in result["samplePotEvents"]},
            {"sensor_trigger_zone_budget_runtime"},
        )
        self.assertEqual(
            {tuple(event["zone_runtime_request_sensor_ids"]) for event in result["samplePotEvents"]},
            {(1,)},
        )
        self.assertEqual(
            {tuple(event["zone_runtime_request_pot_ids"]) for event in result["samplePotEvents"]},
            {(1,)},
        )
        self.assertEqual({event["request_sensor_id"] for event in result["samplePotEvents"]}, {1})
        self.assertGreater(
            result["sampleEvents"][0]["planned_volume_ml"],
            result["samplePotEvents"][0]["requested_volume_ml"],
        )

    def test_fuzzy_and_anfis_use_sensor_pots_as_decision_inputs(self) -> None:
        snapshot = _two_pot_snapshot(sensor_moisture=45.0, nonsensor_moisture=8.0)

        fuzzy = engine.run_fuzzy_dt_daily_irrigation_with_snapshot(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
        )
        anfis = engine.run_anfis_daily_irrigation_with_snapshot(
            snapshot.start_date,
            snapshot.end_date,
            ANFIS(),
            snapshot=snapshot,
        )

        self.assertEqual(fuzzy["summary"]["controllerInputPolicy"], "sensor_locations_only")
        self.assertEqual(anfis["summary"]["controllerInputPolicy"], "sensor_locations_only")
        self.assertEqual(fuzzy["summary"]["potIrrigationDecisions"], 1)
        self.assertEqual(anfis["summary"]["potIrrigationDecisions"], 1)
        self.assertEqual({row["pot_id"] for row in fuzzy["samplePotDecisions"]}, {1})
        self.assertEqual({row["pot_id"] for row in anfis["samplePotDecisions"]}, {1})
        self.assertEqual({row["sensor_id"] for row in fuzzy["samplePotDecisions"]}, {1})
        self.assertEqual({row["sensor_id"] for row in anfis["samplePotDecisions"]}, {1})

    def test_sensor_threshold_config_overrides_sensor_location_thresholds(self) -> None:
        control_pots = sensor_control_pots(
            [_pot()],
            {
                "sensor_ids": [1],
                "sensor_thresholds": {
                    1: {
                        "comfort_threshold_pct": 48.0,
                        "minimum_moisture_pct": 32.0,
                    },
                },
            },
        )

        self.assertEqual(control_pots[0]["moisture_target_pct"], 48.0)
        self.assertEqual(control_pots[0]["moisture_min_pct"], 32.0)
        self.assertEqual(control_pots[0]["sensor_threshold_override"]["moisture_target_pct"], 48.0)


def _snapshot() -> ExperimentSnapshot:
    day = date(2026, 5, 24)
    weather_rows = [_weather_row(day, hour) for hour in range(24)]
    pot = _pot()
    sensor_reading = {
        "sensor_id": 1,
        "source": ACTUAL_SENSOR_SOURCE,
        "soil_moisture_pct": 24.0,
        "air_temperature_c": 22.0,
    }
    sensor_context = {
        "available": True,
        "lookup": {(day, time(5, 30), 1): sensor_reading},
        "sensor_ids": [1],
        "sensor_reading_dates": [day],
    }
    return ExperimentSnapshot(
        start_date=day,
        end_date=day,
        pot_count=1,
        pots=[pot],
        weather_rows=weather_rows,
        selected_weather_rows=weather_rows,
        weather_by_day={day: weather_rows},
        day_profiles={day: day_profile()},
        sensor_context=sensor_context,
        initial_pot_states={1: PotState(moisture=18.0)},
        estimated_weather_rows=0,
        estimated_selected_weather_rows=0,
        estimated_lookahead_weather_rows=0,
        loaded_at=datetime.combine(day, time(0, 0), tzinfo=LOCAL_TZ),
    )


def _two_pot_snapshot(sensor_moisture: float, nonsensor_moisture: float) -> ExperimentSnapshot:
    day = date(2026, 5, 24)
    weather_rows = [_weather_row(day, hour) for hour in range(24)]
    sensor_pot = _pot(pot_id=1, pot_code="P1")
    nonsensor_pot = _pot(pot_id=2, pot_code="P2")
    sensor_reading = {
        "sensor_id": 1,
        "source": ACTUAL_SENSOR_SOURCE,
        "soil_moisture_pct": sensor_moisture,
        "air_temperature_c": 22.0,
    }
    sensor_context = {
        "available": True,
        "lookup": {(day, time(5, 30), 1): sensor_reading},
        "sensor_ids": [1],
        "sensor_reading_dates": [day],
    }
    return ExperimentSnapshot(
        start_date=day,
        end_date=day,
        pot_count=2,
        pots=[sensor_pot, nonsensor_pot],
        weather_rows=weather_rows,
        selected_weather_rows=weather_rows,
        weather_by_day={day: weather_rows},
        day_profiles={day: day_profile()},
        sensor_context=sensor_context,
        initial_pot_states={
            1: PotState(moisture=sensor_moisture),
            2: PotState(moisture=nonsensor_moisture),
        },
        estimated_weather_rows=0,
        estimated_selected_weather_rows=0,
        estimated_lookahead_weather_rows=0,
        loaded_at=datetime.combine(day, time(0, 0), tzinfo=LOCAL_TZ),
    )


def _anfis_controller() -> AnfisModelController:
    return AnfisModelController(
        global_model=ANFIS(),
        global_calibrator=AnfisProbabilityCalibrator([]),
        zone_models={},
        zone_calibrators={},
    )


def _baseline_result_for_shared_t0(snapshot: ExperimentSnapshot, shared_moisture: float) -> dict:
    day = snapshot.start_date
    return {
        "summary": {
            "stateSimulationStartDate": "2026-04-01",
            "stateLookbackDays": 54,
            "stateAnchorPolicy": "stable_historical_timeline",
            "baselineSensorCalibrationPolicy": "continuous_sensor_calibration",
            "irrigationDecisions": 0,
            "source": "database-weather-pot-inventory-and-sensor-readings",
        },
        "entries": [
            {
                "date": day.isoformat(),
                "timestamp": datetime.combine(day, time(12, 0), tzinfo=LOCAL_TZ).isoformat(),
                "day_label": day.isoformat(),
                "chart_label": day.isoformat(),
                "average_moisture": 49.0,
                "temperature": 22.0,
                "max_temperature": 22.0,
                "min_temperature": 18.0,
                "humidity": 55.0,
                "cloud_cover_pct": 35.0,
                "rain_prediction": False,
                "rain_amount": 0.0,
                "irrigation_active": False,
                "irrigation_events": 0,
                "valve_runs": 0,
                "water_usage_l": 0.0,
                "water_usage_ml": 0.0,
                "alerts": 0,
            }
        ],
        "chartEntries": [],
        "pots": [],
        "stateAtExperimentStart": {
            str(pot_id): {
                "moisture": shared_moisture,
                "too_wet_hours": 0,
            }
            for pot_id in snapshot.initial_pot_states
        },
    }


def _weather_row(day: date, hour: int) -> dict:
    return {
        "id": hour + 1,
        "observed_local_at": datetime.combine(day, time(hour, 0), tzinfo=LOCAL_TZ),
        "temperature_c": 22.0,
        "relative_humidity_pct": 55.0,
        "precipitation_mm": 0.0,
        "precipitation_probability_pct": 0.0,
        "wind_speed_kmh": 8.0,
        "wind_gust_kmh": 12.0,
        "cloud_cover_pct": 35.0,
        "shortwave_radiation_w_m2": 250.0,
        "evapotranspiration_mm": 0.04,
    }


def day_profile() -> dict:
    return {
        "season": "spring",
        "dormant_period": False,
        "avg_temperature_c": 22.0,
        "max_temperature_c": 22.0,
        "min_temperature_c": 18.0,
        "avg_humidity_pct": 55.0,
        "avg_cloud_cover_pct": 35.0,
        "avg_shortwave_radiation_w_m2": 250.0,
        "max_shortwave_radiation_w_m2": 250.0,
        "precipitation_mm": 0.0,
        "precipitation_next_14_days_mm": 0.0,
        "reference_evapotranspiration_mm": 0.96,
        "max_precipitation_probability_pct": 0.0,
        "max_precipitation_probability_next_14_days_pct": 0.0,
        "max_wind_gust_kmh": 12.0,
        "min_temperature_next_14_days_c": 18.0,
        "heatwave_day": False,
        "dry_windy_day": False,
        "freeze_risk": False,
        "no_rain_10_days": True,
        "dry_streak_days": 3,
    }


def _pot(pot_id: int = 1, pot_code: str = "P1") -> dict:
    return {
        "id": pot_id,
        "pot_code": pot_code,
        "label": f"Test pot {pot_id}",
        "moisture_min_pct": 30.0,
        "moisture_target_pct": 42.0,
        "winter_moisture_target_pct": 16.0,
        "moisture_max_pct": 70.0,
        "volume_l": 12.0,
        "retention_factor": 0.9,
        "drip_flow_ml_min": 120.0,
        "size_class": "medium",
        "small_subtype": None,
        "cycle_soak_enabled": False,
        "plant_type_code": "ornamental",
        "heat_sensitive": False,
        "allows_second_watering": False,
        "water_need_level": "medium",
        "sun_exposure": "partial",
        "wind_exposure": "moderate",
        "rain_exposure": "partially_exposed",
        "container_material": "plastic",
        "soil_profile": "standard",
        "balcony_zone": "west_wall",
        "evaporation_factor": 1.0,
        "_sun_factor": 1.0,
        "_wind_factor": 1.0,
    }


if __name__ == "__main__":
    unittest.main()
