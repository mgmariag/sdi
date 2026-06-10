from __future__ import annotations

import unittest
from datetime import datetime, time

from digital_twin.db.repositories.sensor_repository import (
    _activity_window,
    _irrigation_activity,
    _next_prescription_irrigation,
    _next_recommendation_ready_at,
    _next_dt_planned_irrigation,
    _next_irrigation_window,
)


class OverviewRepositoryTests(unittest.TestCase):
    def test_next_dt_planned_irrigation_ignores_full_refill_plan_when_off(self) -> None:
        next_window = {
            "start_at": "2026-06-05T17:00:00",
            "end_at": "2026-06-05T19:00:00",
        }
        valve_plan = {
            "required_valves": 5,
            "valve_starts": 0,
            "optimized_runtime_min": 86.2,
            "complete_irrigation_volume_l": 135.31,
        }

        self.assertIsNone(_next_dt_planned_irrigation(next_window, valve_plan))

    def test_next_dt_planned_irrigation_uses_immediate_run_only(self) -> None:
        next_window = {
            "start_at": "2026-06-05T05:30:00",
            "end_at": "2026-06-05T07:30:00",
        }
        valve_plan = {
            "required_valves": 5,
            "valve_starts": 2,
            "immediate_optimized_runtime_min": 24.5,
            "immediate_irrigation_volume_l": 18.25,
        }

        planned = _next_dt_planned_irrigation(next_window, valve_plan)

        self.assertIsNotNone(planned)
        self.assertEqual(planned["source"], "digital_twin_immediate_plan")
        self.assertEqual(planned["item_count"], 2)
        self.assertEqual(planned["planned_volume_l"], 18.25)
        self.assertEqual(planned["label"], "2026-06-05 05:30 - 05:54")

    def test_evening_window_is_skipped_on_non_hot_days(self) -> None:
        conn = _FakeOverviewConnection(max_temperatures={"2026-06-05": 26.9, "2026-06-06": 27.0})

        window = _next_irrigation_window(conn, datetime(2026, 6, 5, 7, 10))

        self.assertEqual(window["label"], "2026-06-06 05:30 - 07:30")

    def test_evening_window_is_allowed_on_hot_days(self) -> None:
        conn = _FakeOverviewConnection(max_temperatures={"2026-06-05": 33.1})

        window = _next_irrigation_window(conn, datetime(2026, 6, 5, 7, 10))

        self.assertEqual(window["label"], "2026-06-05 17:00 - 19:00")

    def test_next_prescription_irrigation_reads_baseline_future_events(self) -> None:
        conn = _FakePrescriptionConnection()

        window = _next_prescription_irrigation(conn, datetime(2026, 6, 5, 20, 0))

        self.assertIsNotNone(window)
        self.assertEqual(window["source"], "prescription")
        self.assertEqual(window["mode"], "next_planned")
        self.assertEqual(window["display_label"], "Next planned irrigation")
        self.assertEqual(window["label"], "2026-06-06 05:30 - 05:45")
        self.assertEqual(window["item_count"], 1)
        self.assertEqual(window["planned_volume_l"], 0.5)
        self.assertEqual(window["activated_valves"], "V1")

    def test_activity_window_reports_valve_water_details(self) -> None:
        window = _activity_window(
            [
                {
                    "experiment_type": "baseline",
                    "start_at": datetime(2026, 6, 6, 6, 0),
                    "end_at": datetime(2026, 6, 6, 6, 12),
                    "planned_volume_ml": 12000,
                    "valve_number": 1,
                    "valve_zone": "west_wall",
                },
                {
                    "experiment_type": "baseline",
                    "start_at": datetime(2026, 6, 6, 6, 0),
                    "end_at": datetime(2026, 6, 6, 6, 18),
                    "planned_volume_ml": 18500,
                    "valve_number": 2,
                    "valve_zone": "south_rail",
                },
            ],
            source="actuation_history",
            mode="most_recent",
            display_label="Most recent irrigation",
        )

        self.assertEqual(window["planned_volume_l"], 30.5)
        self.assertEqual(window["valves"][0]["valve_name"], "west wall")
        self.assertEqual(window["valves"][0]["planned_volume_l"], 12.0)
        self.assertEqual(window["valves"][1]["valve_name"], "south rail")
        self.assertEqual(window["valves"][1]["planned_volume_l"], 18.5)

    def test_next_prescription_irrigation_ignores_current_day_leftovers(self) -> None:
        conn = _FakePrescriptionConnection(
            [
                {
                    "experiment_type": "fuzzy_dt",
                    "prescription_date": "2026-06-05",
                    "payload": {
                        "events": [
                            {
                                "scheduled_start_at": "2026-06-05T08:00:00+03:00",
                                "scheduled_end_at": "2026-06-05T08:22:00+03:00",
                                "planned_volume_ml": 68350,
                                "valve_number": 5,
                            }
                        ]
                    },
                }
            ]
        )

        self.assertIsNone(_next_prescription_irrigation(conn, datetime(2026, 6, 5, 7, 0)))

    def test_irrigation_activity_falls_back_to_recent_window(self) -> None:
        recent = {
            "label": "2026-06-05 05:30 - 06:10",
            "start_at": "2026-06-05T05:30:00",
            "end_at": "2026-06-05T06:10:00",
            "source": "actuation_history",
        }

        activity = _irrigation_activity(None, recent)

        self.assertEqual(activity["mode"], "most_recent")
        self.assertEqual(activity["display_label"], "Most recent irrigation")
        self.assertEqual(activity["label"], recent["label"])

    def test_next_recommendation_ready_uses_dispatch_time(self) -> None:
        self.assertEqual(
            _next_recommendation_ready_at(datetime(2026, 6, 5, 20, 0)),
            datetime(2026, 6, 5, 21, 0),
        )
        self.assertEqual(
            _next_recommendation_ready_at(datetime(2026, 6, 5, 22, 0)),
            datetime(2026, 6, 6, 21, 0),
        )


class _FakeOverviewConnection:
    def __init__(self, max_temperatures: dict[str, float]) -> None:
        self.max_temperatures = max_temperatures

    def execute(self, query: str, params: dict | None = None) -> "_FakeResult":
        if "SELECT morning_window_start" in query:
            return _FakeResult(
                {
                    "morning_window_start": time(5, 30),
                    "morning_window_end": time(7, 30),
                    "evening_window_start": time(17, 0),
                    "evening_window_end": time(19, 0),
                }
            )

        if "max(temperature_c)" in query:
            day = params["start_at"].date().isoformat()
            return _FakeResult({"max_temperature_c": self.max_temperatures.get(day)})

        raise AssertionError(f"Unexpected query: {query}")


class _FakePrescriptionConnection:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or [
            {
                "experiment_type": "baseline",
                "prescription_date": "2026-06-06",
                "payload": {
                    "events": [
                        {
                            "scheduled_start_at": "2026-06-06T05:30:00+03:00",
                            "scheduled_end_at": "2026-06-06T05:45:00+03:00",
                            "planned_volume_ml": 500,
                            "valve_number": 1,
                        }
                    ]
                },
            },
            {
                "experiment_type": "fuzzy_dt",
                "prescription_date": "2026-06-06",
                "payload": {
                    "events": [
                        {
                            "scheduled_start_at": "2026-06-06T05:30:00+03:00",
                            "scheduled_end_at": "2026-06-06T05:55:00+03:00",
                            "planned_volume_ml": 200,
                            "valve_number": 2,
                        },
                        {
                            "scheduled_start_at": "2026-06-06T06:30:00+03:00",
                            "scheduled_end_at": "2026-06-06T06:45:00+03:00",
                            "planned_volume_ml": 900,
                            "valve_number": 3,
                        },
                    ]
                },
            },
        ]

    def execute(self, query: str, params: dict | None = None) -> "_FakeResult":
        if "FROM irrigation_prescriptions" not in query:
            raise AssertionError(f"Unexpected query: {query}")
        today = params["today"]
        experiment_type = params.get("experiment_type")
        return _FakeResult([
            row
            for row in self.rows
            if row["prescription_date"] > today.isoformat()
            and (experiment_type is None or row["experiment_type"] == experiment_type)
        ])


class _FakeResult:
    def __init__(self, row) -> None:
        self.row = row

    def fetchone(self):
        if isinstance(self.row, list):
            return self.row[0] if self.row else None
        return self.row

    def fetchall(self) -> list:
        return self.row if isinstance(self.row, list) else [self.row]


if __name__ == "__main__":
    unittest.main()
