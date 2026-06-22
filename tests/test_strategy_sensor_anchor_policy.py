from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from digital_twin.domain.sensor import SensorSource
from digital_twin.simulation.anfis.controller import (
    AnfisModelController,
    AnfisProbabilityCalibrator,
)
from digital_twin.simulation.engine import SimulationEngine
from digital_twin.simulation.anfis.model import ANFIS
from digital_twin.simulation.sampling.execution import SparseSamplingRunner
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import (
    ExperimentSnapshot,
    PotState,
)
from digital_twin.simulation.sensors.calibration import (
    apply_sensor_calibration_marker,
)
from digital_twin.simulation.sensors.context import sensor_control_pots


class StrategySensorAnchorPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SimulationEngine()

    def test_fuzzy_dt_uses_initial_sensor_anchor_without_continuous_calibration(self) -> None:
        snapshot = _snapshot()
        engine = _NoWarmupSimulationEngine()

        with patch(
            "digital_twin.simulation.sensors.calibration.apply_sensor_calibration_marker",
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
        engine = _NoWarmupSimulationEngine()

        with patch(
            "digital_twin.simulation.sensors.calibration.apply_sensor_calibration_marker",
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
        engine = _ExperimentStartSimulationEngine()

        with patch(
            "digital_twin.simulation.daily_irrigation.sensor_calibration.apply_sensor_calibration_marker",
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
        engine = _ExperimentStartSimulationEngine()

        with patch(
            "digital_twin.simulation.daily_irrigation.sensor_calibration.apply_sensor_calibration_marker",
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
        engine = _NoWarmupSimulationEngine()
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

    def test_sampling_forecast_sample_calibrates_associated_pot_from_probe_state(self) -> None:
        snapshot = _two_pot_snapshot(sensor_moisture=35.0, nonsensor_moisture=12.0)
        sensor_pot, associated_pot = snapshot.pots
        snapshot.sensor_context.update(
            {
                "lookup": {},
                "future_dates": [snapshot.start_date],
                "associations": {
                    1: {"sensor_id": 1, "direct": True, "distance": 0.0},
                    2: {"sensor_id": 1, "direct": False, "distance": 1.0},
                },
                "sensor_pots": {1: sensor_pot},
            }
        )
        runner = SparseSamplingRunner(
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            sample_interval_hours=72,
            persist=False,
            selected_snapshot=snapshot,
            simulation_start_date=snapshot.start_date,
            simulation_snapshot=snapshot,
            state_anchor_policy="baseline_warmup_reuse",
            warmup_reuse_policy="baseline_start_state_reuse",
        )
        runner.states[associated_pot["id"]].moisture = 12.0
        runner.probe_states[associated_pot["id"]].moisture = 47.0

        sample_sensor_ids = runner._refresh_sample_states_from_sensor(
            snapshot.start_date,
            datetime.combine(snapshot.start_date, time(6, 0), tzinfo=LOCAL_TZ),
            day_profile(),
            sample_now=True,
        )

        self.assertEqual(sample_sensor_ids, {1})
        self.assertEqual(runner.states[associated_pot["id"]].moisture, 47.0)

    def test_sampling_chart_entries_use_forecast_calibrated_associated_pot(self) -> None:
        snapshot = _two_pot_snapshot(sensor_moisture=35.0, nonsensor_moisture=12.0)
        sensor_pot, associated_pot = snapshot.pots
        for pot in snapshot.pots:
            pot["moisture_min_pct"] = 10.0
            pot["moisture_target_pct"] = 15.0
            pot["winter_moisture_target_pct"] = 10.0
        snapshot.sensor_context.update(
            {
                "lookup": {},
                "future_dates": [snapshot.start_date],
                "associations": {
                    1: {"sensor_id": 1, "direct": True, "distance": 0.0},
                    2: {"sensor_id": 1, "direct": False, "distance": 1.0},
                },
                "sensor_pots": {1: sensor_pot},
            }
        )
        runner = SparseSamplingRunner(
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            sample_interval_hours=72,
            persist=False,
            selected_snapshot=snapshot,
            simulation_start_date=snapshot.start_date,
            simulation_snapshot=snapshot,
            state_anchor_policy="baseline_warmup_reuse",
            warmup_reuse_policy="baseline_start_state_reuse",
        )
        runner.states[associated_pot["id"]].moisture = 12.0
        runner.probe_states[associated_pot["id"]].moisture = 47.0

        result = runner.run()
        chart_by_hour = {entry["hour"]: entry for entry in result["chartEntries"]}

        self.assertLess(chart_by_hour["05:00"]["average_moisture"], 30.0)
        self.assertGreater(chart_by_hour["06:00"]["average_moisture"], 38.0)
        self.assertTrue(chart_by_hour["06:00"]["sparse_sensor_sample"])
        self.assertEqual(chart_by_hour["06:00"]["sparse_sensor_samples"], 1)

    def test_sampling_forecast_decision_slots_sync_to_baseline_reference(self) -> None:
        snapshot = _hot_forecast_snapshot()
        hot_sample_day = snapshot.start_date + timedelta(days=3)
        baseline = self.engine.run_default_dt_irrigation_control(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
            state_anchor_policy="experiment_start_sensor_anchor",
        )

        result = self.engine.run_daily_sampling_experiment(
            snapshot.start_date,
            snapshot.end_date,
            sample_interval_hours=72,
            snapshot=snapshot,
            baseline_result=baseline,
        )

        row = {entry["date"]: entry for entry in result["entries"]}[hot_sample_day.isoformat()]
        self.assertTrue(row["sparse_sensor_sample"])
        self.assertEqual(row["baseline_moisture"], row["sparse_moisture"])
        self.assertEqual(row["baseline_water_usage_l"], row["sparse_water_usage_l"])

    def test_fuzzy_and_anfis_first_point_uses_shared_experiment_start_state(self) -> None:
        snapshot = _snapshot()
        baseline_result = _baseline_result_for_shared_t0(snapshot, shared_moisture=37.5)

        fuzzy = self.engine.run_daily_fuzzy_dt_experiment(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
            baseline_result=baseline_result,
        )
        anfis = self.engine.run_daily_anfis_experiment(
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

        result = self.engine.run_default_dt_irrigation_control(
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

        result = self.engine.run_default_dt_irrigation_control(
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

        fuzzy = self.engine.run_fuzzy_dt_daily_irrigation_with_snapshot(
            snapshot.start_date,
            snapshot.end_date,
            snapshot=snapshot,
        )
        anfis = self.engine.run_anfis_daily_irrigation_with_snapshot(
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


class _NoWarmupSimulationEngine(SimulationEngine):
    def resolve_simulation_snapshot(
        self,
        start_date: date,
        end_date: date,
        selected_snapshot: ExperimentSnapshot,
    ) -> tuple[date, ExperimentSnapshot]:
        raise AssertionError("strategy should not resolve an independent warm-up snapshot")


class _ExperimentStartSimulationEngine(SimulationEngine):
    def state_simulation_start(self, start_date: date, end_date: date) -> date:
        return start_date


def _snapshot() -> ExperimentSnapshot:
    day = date(2026, 5, 24)
    weather_rows = [_weather_row(day, hour) for hour in range(24)]
    pot = _pot()
    sensor_reading = {
        "sensor_id": 1,
        "source": SensorSource.ACTUAL.value,
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
        "source": SensorSource.ACTUAL.value,
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


def _hot_forecast_snapshot() -> ExperimentSnapshot:
    start = date(2026, 6, 22)
    end = start + timedelta(days=4)
    weather_rows = []
    weather_by_day = {}
    day_profiles = {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        rows = []
        for hour in range(24):
            row = _weather_row(day, hour)
            row["temperature_c"] = 36.0 if 8 <= hour <= 20 else 24.0
            row["evapotranspiration_mm"] = 0.08 if 8 <= hour <= 20 else 0.02
            rows.append(row)
        profile = day_profile()
        profile.update(
            {
                "avg_temperature_c": 32.0,
                "max_temperature_c": 36.0,
                "min_temperature_c": 24.0,
                "heatwave_day": True,
                "reference_evapotranspiration_mm": 1.2,
            }
        )
        weather_rows.extend(rows)
        weather_by_day[day] = rows
        day_profiles[day] = profile

    pots = [_pot(pot_id=1, pot_code="P1"), _pot(pot_id=2, pot_code="P2")]
    for pot in pots:
        pot["moisture_min_pct"] = 30.0
        pot["moisture_target_pct"] = 42.0
        pot["winter_moisture_target_pct"] = 16.0
        pot["plant_type_code"] = "vegetables"
        pot["allows_second_watering"] = True
        pot["water_need_level"] = "high"
        pot["sun_exposure"] = "full"
        pot["_sun_factor"] = 1.24

    sensor_context = {
        "available": True,
        "lookup": {},
        "sensor_ids": [1],
        "sensor_reading_dates": [],
        "future_dates": [start + timedelta(days=offset) for offset in range((end - start).days + 1)],
        "associations": {
            1: {"sensor_id": 1, "direct": True, "distance": 0.0},
            2: {"sensor_id": 1, "direct": False, "distance": 1.0},
        },
        "sensor_pots": {1: pots[0]},
    }
    return ExperimentSnapshot(
        start_date=start,
        end_date=end,
        pot_count=2,
        pots=pots,
        weather_rows=weather_rows,
        selected_weather_rows=weather_rows,
        weather_by_day=weather_by_day,
        day_profiles=day_profiles,
        sensor_context=sensor_context,
        initial_pot_states={1: PotState(moisture=26.0), 2: PotState(moisture=26.0)},
        estimated_weather_rows=0,
        estimated_selected_weather_rows=0,
        estimated_lookahead_weather_rows=0,
        loaded_at=datetime.combine(start, time(0, 0), tzinfo=LOCAL_TZ),
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
