from __future__ import annotations

import unittest
from datetime import date, datetime, time

from digital_twin.domain.sensors import (
    ACTUAL_SENSOR_SOURCE,
    DEFAULT_SENSOR_SOURCE,
)
from digital_twin.domain.sensors import SPARSE_FORECAST_SENSOR_SOURCE
from digital_twin.simulation.metrics import (
    daily_moisture_summary,
    new_daily_moisture_tracker,
    post_irrigation_snapshot_index,
    record_daily_moisture_snapshot,
    sampling_moisture_chart_summary,
)
from digital_twin.simulation.shared.constants import LOCAL_TZ
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.state.projection import (
    initialize_states_from_first_day_sensor_readings,
)
from digital_twin.simulation.sensors.calibration import (
    apply_calibration_reading,
    apply_sensor_calibration_marker,
    apply_sensor_reading,
    apply_stored_sensor_calibration,
    forecast_sensor_reading_for_pot,
    sampling_calibration_at,
)
from digital_twin.simulation.valves.rollups import (
    activated_valve_label,
    comparison_window_fields,
)


class SimulationSensorStateTests(unittest.TestCase):
    def test_simulated_sensor_reading_does_not_override_physical_state(self) -> None:
        state = PotState(moisture=31.0)
        reading = _sensor_reading(DEFAULT_SENSOR_SOURCE, 52.0)

        result = apply_sensor_reading(
            state,
            _pot(),
            date(2026, 5, 21),
            datetime(2026, 5, 21, 14, 0, tzinfo=LOCAL_TZ),
            _sensor_context(reading),
        )

        self.assertIsNotNone(result)
        self.assertEqual(state.moisture, 31.0)

    def test_actual_sensor_reading_overrides_physical_state(self) -> None:
        state = PotState(moisture=31.0)
        reading = _sensor_reading(ACTUAL_SENSOR_SOURCE, 52.0)

        apply_sensor_reading(
            state,
            _pot(),
            date(2026, 5, 21),
            datetime(2026, 5, 21, 14, 0, tzinfo=LOCAL_TZ),
            _sensor_context(reading),
        )

        self.assertEqual(state.moisture, 52.0)

    def test_stored_sensor_marker_calibrates_state_at_530_anchor(self) -> None:
        state = PotState(moisture=31.0)
        reading = _sensor_reading(DEFAULT_SENSOR_SOURCE, 52.0)

        apply_sensor_calibration_marker(
            state,
            _pot(),
            date(2026, 5, 21),
            datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ),
            _sensor_context(reading, time(5, 30)),
            {"max_temperature_c": 25.0},
        )

        self.assertEqual(state.moisture, 52.0)

    def test_hot_day_evening_marker_calibrates_state(self) -> None:
        state = PotState(moisture=31.0)
        reading = _sensor_reading(DEFAULT_SENSOR_SOURCE, 44.0)

        apply_sensor_calibration_marker(
            state,
            _pot(),
            date(2026, 5, 21),
            datetime(2026, 5, 21, 18, 0, tzinfo=LOCAL_TZ),
            _sensor_context(reading, time(17, 30)),
            {"max_temperature_c": 33.0},
        )

        self.assertEqual(state.moisture, 44.0)

    def test_stored_associated_sensor_calibration_replaces_prior_state(self) -> None:
        state = PotState(moisture=12.0)
        reading = _sensor_reading(DEFAULT_SENSOR_SOURCE, 52.0)
        sensor_context = {
            "available": True,
            "lookup": {
                (date(2026, 5, 21), time(5, 30), 1): reading,
            },
            "associations": {
                2: {"sensor_id": 1, "direct": False, "distance": 1.0},
            },
            "sensor_pots": {
                1: _associated_pot(1),
            },
        }

        result = apply_stored_sensor_calibration(
            state,
            _associated_pot(2),
            date(2026, 5, 21),
            datetime(2026, 5, 21, 5, 30, tzinfo=LOCAL_TZ),
            sensor_context,
        )

        self.assertIsNotNone(result)
        self.assertEqual(state.moisture, 52.0)
        self.assertEqual(result["sensor_blend_weight"], 1.0)

    def test_initial_state_anchor_uses_associated_sensor_readings(self) -> None:
        start_date = date(2026, 5, 21)
        states = {1: PotState(moisture=12.0), 2: PotState(moisture=18.0)}
        reading = _sensor_reading(DEFAULT_SENSOR_SOURCE, 52.0)
        sensor_context = {
            "available": True,
            "lookup": {
                (start_date, time(5, 30), 1): reading,
            },
            "associations": {
                1: {"sensor_id": 1, "direct": True, "distance": 0.0},
                2: {"sensor_id": 1, "direct": False, "distance": 1.0},
            },
            "sensor_pots": {
                1: _associated_pot(1),
            },
        }

        anchor = initialize_states_from_first_day_sensor_readings(
            states,
            [_associated_pot(1), _associated_pot(2)],
            sensor_context,
            start_date,
        )

        self.assertEqual(anchor["anchored_pots"], 2)
        self.assertEqual(states[1].moisture, 52.0)
        self.assertEqual(states[2].moisture, 52.0)

    def test_evening_marker_is_skipped_when_day_is_not_hot(self) -> None:
        state = PotState(moisture=31.0)
        reading = _sensor_reading(DEFAULT_SENSOR_SOURCE, 44.0)

        apply_sensor_calibration_marker(
            state,
            _pot(),
            date(2026, 5, 21),
            datetime(2026, 5, 21, 18, 0, tzinfo=LOCAL_TZ),
            _sensor_context(reading, time(17, 30)),
            {"max_temperature_c": 32.0},
        )

        self.assertEqual(state.moisture, 31.0)

    def test_sampling_first_morning_sample_uses_same_530_anchor(self) -> None:
        calibration_at = sampling_calibration_at(
            date(2026, 5, 21),
            datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ),
            {"max_temperature_c": 25.0},
        )

        self.assertEqual(calibration_at, datetime(2026, 5, 21, 5, 30, tzinfo=LOCAL_TZ))

    def test_sampling_forecast_calibration_uses_default_reference_state(self) -> None:
        forecast_day = date(2026, 5, 21)
        probe_states = {1: PotState(moisture=44.0), 2: PotState(moisture=37.0)}
        sparse_states = {1: PotState(moisture=20.0), 2: PotState(moisture=20.0)}
        sensor_context = {
            "available": True,
            "future_dates": [forecast_day],
            "sensor_ids": [1],
            "associations": {
                1: {"sensor_id": 1, "direct": True, "distance": 0.0},
                2: {"sensor_id": 1, "direct": False, "distance": 1.0},
            },
            "sensor_pots": {
                1: _associated_pot(1),
            },
        }

        reading = forecast_sensor_reading_for_pot(
            probe_states,
            sensor_context,
            _associated_pot(2),
            forecast_day,
            datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ),
        )
        applied = apply_calibration_reading(
            sparse_states[2],
            reading,
            datetime(2026, 5, 21, 6, 0, tzinfo=LOCAL_TZ),
        )

        self.assertIsNotNone(applied)
        self.assertEqual(applied["source"], SPARSE_FORECAST_SENSOR_SOURCE)
        self.assertEqual(applied["associated_sensor_id"], 1)
        self.assertEqual(applied["association_source"], "default_strategy_reference")
        self.assertEqual(sparse_states[2].moisture, 37.0)
        self.assertEqual(applied["sensor_blend_weight"], 1.0)

    def test_sampling_chart_summary_keeps_simulated_sparse_moisture(self) -> None:
        rows = [
            {"baseline_moisture": 40.0, "sparse_moisture": 40.0, "sparse_sensor_sample": True},
            {"baseline_moisture": 42.0, "sparse_moisture": 39.25, "sparse_sensor_sample": False},
            {"baseline_moisture": 43.0, "sparse_moisture": 38.75, "sparse_sensor_sample": False},
        ]

        summary = sampling_moisture_chart_summary(rows, sample_interval_hours=72, hourly=False)

        self.assertEqual([row["sparse_moisture"] for row in rows], [40.0, 39.25, 38.75])
        self.assertEqual(summary["sample_count"], 1)

    def test_sparse_daily_summary_uses_post_morning_marker(self) -> None:
        rows = [_weather_hour(date(2026, 5, 21), hour) for hour in range(7, 11)]

        index, label = post_irrigation_snapshot_index(
            date(2026, 5, 21),
            rows,
            0,
            {"max_temperature_c": 31.0},
        )

        self.assertEqual(index, 2)
        self.assertEqual(label, "after_morning")

    def test_sparse_daily_summary_uses_hot_day_post_evening_marker(self) -> None:
        rows = [_weather_hour(date(2026, 6, 3), hour) for hour in range(19, 23)]

        index, label = post_irrigation_snapshot_index(
            date(2026, 6, 3),
            rows,
            0,
            {"max_temperature_c": 33.0},
        )

        self.assertEqual(index, 2)
        self.assertEqual(label, "after_evening")

    def test_daily_moisture_summary_reports_end_of_day_moisture(self) -> None:
        tracker = new_daily_moisture_tracker()
        states = {1: PotState(moisture=31.0), 2: PotState(moisture=41.0)}
        record_daily_moisture_snapshot(tracker, states, "before_morning")

        states[1].moisture = 51.0
        states[2].moisture = 61.0
        record_daily_moisture_snapshot(tracker, states, "after_morning")

        states[1].moisture = 34.0
        states[2].moisture = 44.0
        summary = daily_moisture_summary(tracker, states)

        self.assertEqual(summary["average_moisture"], 39.0)
        self.assertEqual(summary["pre_irrigation_moisture"], 36.0)
        self.assertEqual(summary["post_irrigation_moisture"], 56.0)
        self.assertEqual(summary["moisture_sample_method"], "end_of_day")

    def test_comparison_window_fields_include_moisture_markers(self) -> None:
        fields = comparison_window_fields(
            "baseline",
            {
                "pre_irrigation_moisture": 35.68,
                "post_irrigation_moisture": 36.8,
            },
        )

        self.assertEqual(fields["baseline_pre_irrigation_moisture"], 35.68)
        self.assertEqual(fields["baseline_post_irrigation_moisture"], 36.8)

    def test_activated_valve_label_compacts_configured_valves(self) -> None:
        self.assertEqual(activated_valve_label([]), "none")
        self.assertEqual(activated_valve_label(_valve_events([1, 2, 3, 4, 5])), "all")
        self.assertEqual(activated_valve_label(_valve_events([1, 2, 3, 4])), "V1-V4")
        self.assertEqual(activated_valve_label(_valve_events([1, 3, 5])), "V1, V3, V5")


def _pot() -> dict:
    return {"id": 1}


def _associated_pot(pot_id: int) -> dict:
    return {
        "id": pot_id,
        "moisture_target_pct": 50.0,
        "moisture_min_pct": 35.0,
        "moisture_max_pct": 70.0,
        "rain_exposure": "partially_exposed",
        "sun_exposure": "partial",
        "wind_exposure": "moderate",
        "retention_factor": 0.8,
        "volume_l": 10.0,
    }


def _sensor_context(reading: dict, slot_time: time = time(14, 0)) -> dict:
    return {
        "available": True,
        "lookup": {
            (date(2026, 5, 21), slot_time, 1): reading,
        },
    }


def _sensor_reading(source: str, moisture: float) -> dict:
    return {
        "sensor_id": 1,
        "source": source,
        "soil_moisture_pct": moisture,
    }


def _weather_hour(day: date, hour: int) -> dict:
    return {"observed_local_at": datetime.combine(day, time(hour, 0), tzinfo=LOCAL_TZ)}


def _valve_events(numbers: list[int]) -> list[dict]:
    return [{"valve_number": number} for number in numbers]


if __name__ == "__main__":
    unittest.main()
