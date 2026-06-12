from __future__ import annotations

import unittest
from datetime import date

from digital_twin.simulation import engine
from digital_twin.simulation.dto import PotState


class ValveDistributionTests(unittest.TestCase):
    def test_valve_runtime_distributes_water_by_pot_flow_rate(self) -> None:
        pots = [
            _pot(1, "P1", 5.0),
            _pot(2, "P2", 10.0),
            _pot(3, "P3", 20.0),
        ]
        states = {pot["id"]: PotState(moisture=0.0) for pot in pots}
        requested = {1: 120.0, 2: 80.0, 3: 250.0}

        events = engine._execute_valve_zone_distribution(
            states,
            {"west_wall": pots},
            "west_wall",
            date(2026, 5, 21),
            {"observed_local_at": "2026-05-21T06:00:00+03:00"},
            {pot["id"]: {"pot_id": pot["id"], "slot": "morning"} for pot in pots},
            lambda pot, decision: decision,
            lambda pot, weather, decision: _request_event(pot, requested[pot["id"]]),
            {"zone": "west_wall"},
        )

        delivered_by_pot = {event["pot_id"]: event["delivered_volume_ml"] for event in events}
        self.assertEqual(delivered_by_pot, {1: 64.29, 2: 128.57, 3: 257.14})
        self.assertTrue(all(event["valve_runtime_min"] == 12.857 for event in events))
        self.assertEqual(round(sum(delivered_by_pot.values()), 2), 450.0)
        self.assertAlmostEqual(states[1].moisture, 0.64, places=2)
        self.assertAlmostEqual(states[2].moisture, 1.29, places=2)
        self.assertAlmostEqual(states[3].moisture, 2.57, places=2)
        self.assertEqual(events[0]["pre_delivery_moisture_pct"], 0.0)
        self.assertEqual(events[0]["post_delivery_moisture_pct"], 0.64)
        self.assertEqual(events[0]["delivery_moisture_gain_pct"], 0.64)

        valve_event = engine._valve_event_from_group(
            ("2026-05-21", "morning", "2026-05-21T06:00:00+03:00", "west_wall"),
            events,
            {pot["id"]: pot for pot in pots},
            {"west_wall": pots},
        )

        self.assertEqual(valve_event["requested_volume_ml"], 450.0)
        self.assertEqual(valve_event["delivered_volume_ml"], 450.0)
        self.assertEqual(valve_event["duration_min"], 12.9)
        self.assertEqual(valve_event["physical_distribution_policy"], "valve_runtime_x_pot_drip_flow")
        self.assertEqual(len(valve_event["per_pot_distribution"]), 3)
        self.assertEqual(valve_event["affected_pre_moisture_pct"], 0.0)
        self.assertEqual(valve_event["affected_post_moisture_pct"], 1.5)
        self.assertEqual(valve_event["affected_moisture_gain_pct"], 1.5)
        self.assertEqual(valve_event["per_pot_distribution"][0]["post_delivery_moisture_pct"], 0.64)

        entries = [{"date": "2026-05-21", "timestamp": "2026-05-21T12:00:00+03:00"}]
        engine._apply_valve_counts(entries, {"decisions": [], "events": [valve_event]}, hourly=False)
        self.assertEqual(entries[0]["irrigated_pre_moisture"], 0.0)
        self.assertEqual(entries[0]["irrigated_post_moisture"], 1.5)
        self.assertEqual(entries[0]["irrigated_moisture_gain"], 1.5)

    def test_valve_runtime_uses_zero_requested_pots_in_total_flow(self) -> None:
        pots = [
            _pot(1, "P1", 5.0),
            _pot(2, "P2", 10.0),
            _pot(3, "P3", 20.0),
        ]
        states = {pot["id"]: PotState(moisture=0.0) for pot in pots}
        requested = {1: 120.0, 2: 0.0, 3: 0.0}

        events = engine._execute_valve_zone_distribution(
            states,
            {"west_wall": pots},
            "west_wall",
            date(2026, 5, 21),
            {"observed_local_at": "2026-05-21T06:00:00+03:00"},
            {pot["id"]: {"pot_id": pot["id"], "slot": "morning"} for pot in pots},
            lambda pot, decision: decision,
            lambda pot, weather, decision: _request_event(pot, requested[pot["id"]]),
            {"zone": "west_wall"},
        )

        delivered_by_pot = {event["pot_id"]: event["delivered_volume_ml"] for event in events}
        self.assertEqual(delivered_by_pot, {1: 17.14, 2: 34.29, 3: 68.57})
        self.assertTrue(all(event["valve_runtime_min"] == 3.429 for event in events))
        self.assertEqual(round(sum(delivered_by_pot.values()), 2), 120.0)

    def test_valve_runtime_can_use_trigger_pot_subset(self) -> None:
        pots = [
            _pot(1, "P1", 5.0),
            _pot(2, "P2", 10.0),
            _pot(3, "P3", 20.0),
        ]
        states = {pot["id"]: PotState(moisture=0.0) for pot in pots}
        requested = {1: 120.0, 2: 80.0, 3: 250.0}

        events = engine._execute_valve_zone_distribution(
            states,
            {"west_wall": pots},
            "west_wall",
            date(2026, 5, 21),
            {"observed_local_at": "2026-05-21T06:00:00+03:00"},
            {pot["id"]: {"pot_id": pot["id"], "slot": "morning"} for pot in pots},
            lambda pot, decision: decision,
            lambda pot, weather, decision: _request_event(pot, requested[pot["id"]]),
            {"zone": "west_wall", "runtime_request_pot_ids": [1, 2]},
        )

        delivered_by_pot = {event["pot_id"]: event["delivered_volume_ml"] for event in events}
        self.assertEqual(delivered_by_pot, {1: 28.57, 2: 57.14, 3: 114.29})
        self.assertTrue(all(event["valve_runtime_min"] == 5.714 for event in events))
        self.assertEqual(events[0]["zone_requested_volume_ml"], 450.0)
        self.assertEqual(events[0]["zone_runtime_requested_volume_ml"], 200.0)
        self.assertEqual(events[0]["zone_runtime_request_flow_ml_min"], 15.0)
        self.assertEqual(events[0]["zone_runtime_flow_ml_min"], 35.0)
        self.assertEqual(round(sum(delivered_by_pot.values()), 2), 200.0)


def _request_event(pot: dict, requested_volume_ml: float) -> dict:
    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "date": "2026-05-21",
        "slot": "morning",
        "scheduled_start_at": "2026-05-21T06:00:00+03:00",
        "scheduled_end_at": "2026-05-21T06:01:00+03:00",
        "flow_rate_ml_min": pot["drip_flow_ml_min"],
        "requested_volume_ml": requested_volume_ml,
        "planned_volume_ml": requested_volume_ml,
        "duration_min": requested_volume_ml / pot["drip_flow_ml_min"],
        "cycle_count": 1,
        "soak_pause_min": 0,
    }


def _pot(pot_id: int, code: str, flow_rate: float) -> dict:
    return {
        "id": pot_id,
        "pot_code": code,
        "label": code,
        "balcony_zone": "west_wall",
        "moisture_min_pct": 20.0,
        "moisture_target_pct": 35.0,
        "moisture_max_pct": 60.0,
        "volume_l": 10.0,
        "retention_factor": 1.0,
        "drip_flow_ml_min": flow_rate,
        "size_class": "medium",
        "cycle_soak_enabled": False,
        "sun_exposure": "full",
        "water_need_level": "medium",
        "heat_sensitive": False,
    }


if __name__ == "__main__":
    unittest.main()
