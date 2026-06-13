from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from psycopg.types.json import Jsonb

import digital_twin.application.sensor_history.readings.state_rows as sensor_rows
from digital_twin.application.sensor_history.readings.shared import (
    LOCAL_TZ,
    as_local as _as_local,
    ceil_to_interval as _ceil_to_interval,
    db_timestamp as _db_timestamp,
)
from digital_twin.domain.sensors import (
    ACTUATOR_FEEDBACK_SOURCE,
    DEFAULT_SENSOR_SOURCE,
    RAW_RESOLUTION,
)
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.simulation.soil_model import clamp, number


class ActuationFeedbackService:
    """Projects completed actuator delivery back into the soil-state read model."""

    corrective_experiment_type = "corrective"
    corrective_delay_minutes = 30

    def apply_completed_actuation(self, actuation: dict[str, Any] | None) -> dict[str, Any]:
        if not actuation:
            return self._empty_result()

        deliveries = self._delivery_items(actuation)
        if not deliveries:
            return self._empty_result(actuation)

        feedback_at = self._feedback_recorded_at(actuation)
        scheduled_start = self._actuation_datetime(actuation.get("scheduled_start_at"), feedback_at)
        scheduled_end = self._actuation_datetime(actuation.get("scheduled_end_at"), feedback_at)
        pot_ids = sorted({item["pot_id"] for item in deliveries})
        pots_by_id = sensor_rows.load_pots_by_ids(pot_ids)
        previous_states = sensor_rows.load_latest_sensor_states(feedback_at, DEFAULT_SENSOR_SOURCE)

        rows = []
        below_minimum = []
        for delivery in deliveries:
            pot = pots_by_id.get(delivery["pot_id"])
            if not pot:
                continue
            state = self._state_before_actuation(pot, previous_states.get(delivery["pot_id"]), scheduled_start)
            self._apply_delivery(state, pot, delivery["delivered_volume_ml"], scheduled_end)
            self._advance_state(state, pot, feedback_at)
            rows.append(self._feedback_row(pot, state, feedback_at, delivery))
            min_moisture = float(pot["moisture_min_pct"])
            if state["moisture"] < min_moisture:
                below_minimum.append(
                    {
                        "pot": pot,
                        "moisture_pct": round(state["moisture"], 2),
                        "moisture_min_pct": round(min_moisture, 2),
                    }
                )

        if not rows:
            return self._empty_result(actuation)

        with get_connection() as conn:
            upserted = sensor_rows.upsert_sensor_rows(conn, rows)
            corrective = self._schedule_corrective_actuations(conn, below_minimum, feedback_at)
            conn.commit()

        return {
            "actuationId": actuation.get("id"),
            "source": ACTUATOR_FEEDBACK_SOURCE,
            "feedbackAt": feedback_at.isoformat(),
            "feedbackRows": upserted,
            "belowMinimumCount": len(below_minimum),
            "correctiveCount": len(corrective),
            "corrective": corrective,
        }

    def _empty_result(self, actuation: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "actuationId": actuation.get("id") if actuation else None,
            "source": ACTUATOR_FEEDBACK_SOURCE,
            "feedbackRows": 0,
            "belowMinimumCount": 0,
            "correctiveCount": 0,
            "corrective": [],
        }

    def _delivery_items(self, actuation: dict[str, Any]) -> list[dict[str, Any]]:
        payload = sensor_rows.payload_dict(actuation.get("payload"))
        distribution = payload.get("per_pot_distribution") if isinstance(payload, dict) else None
        actual_delivered_ml = number(
            actuation.get("delivered_volume_ml"),
            number(actuation.get("planned_volume_ml"), 0.0),
        )
        if isinstance(distribution, list) and distribution:
            base_items = [
                {
                    "pot_id": int(item["pot_id"]),
                    "delivered_volume_ml": max(
                        0.0,
                        number(item.get("delivered_volume_ml"), item.get("planned_volume_ml", 0.0)),
                    ),
                }
                for item in distribution
                if item and item.get("pot_id") is not None
            ]
            base_total = sum(item["delivered_volume_ml"] for item in base_items)
            scale = actual_delivered_ml / base_total if base_total > 0 and actual_delivered_ml > 0 else 1.0
            return [
                {
                    "pot_id": item["pot_id"],
                    "delivered_volume_ml": round(item["delivered_volume_ml"] * scale, 2),
                }
                for item in base_items
            ]

        pot_id = actuation.get("pot_id") or payload.get("pot_id") if isinstance(payload, dict) else None
        if pot_id is None:
            return []
        return [
            {
                "pot_id": int(pot_id),
                "delivered_volume_ml": round(max(0.0, actual_delivered_ml), 2),
            }
        ]

    def _feedback_recorded_at(self, actuation: dict[str, Any]) -> datetime:
        completed_at = self._optional_actuation_datetime(actuation.get("completed_at"))
        scheduled_end = self._optional_actuation_datetime(actuation.get("scheduled_end_at"))
        fallback = _as_local(datetime.now(LOCAL_TZ))
        if completed_at and scheduled_end:
            return max(completed_at, scheduled_end)
        return completed_at or scheduled_end or fallback

    def _optional_actuation_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _as_local(value)
        try:
            return _as_local(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            return None

    def _actuation_datetime(self, value: Any, fallback: datetime) -> datetime:
        return self._optional_actuation_datetime(value) or fallback

    def _state_before_actuation(
        self,
        pot: dict[str, Any],
        previous_row: dict[str, Any] | None,
        scheduled_start: datetime,
    ) -> dict[str, Any]:
        if previous_row:
            state = {
                "moisture": number(previous_row["soil_moisture_pct"], float(pot["moisture_target_pct"])),
                "last_recorded_at": _as_local(previous_row["recorded_at"]),
            }
        else:
            state = sensor_rows.initial_state_for_pot(pot)
        self._advance_state(state, pot, scheduled_start)
        return state

    def _advance_state(self, state: dict[str, Any], pot: dict[str, Any], target_at: datetime) -> None:
        target_at = _as_local(target_at)
        current_at = _as_local(state["last_recorded_at"])
        if target_at <= current_at:
            return
        hours = (target_at - current_at).total_seconds() / 3600.0
        weather = sensor_rows.load_latest_weather_at(target_at) or sensor_rows.fallback_weather(target_at)
        sensor_rows.apply_hourly_environment(state, pot, weather, target_at.date(), hours=hours)
        state["last_recorded_at"] = target_at

    def _apply_delivery(
        self,
        state: dict[str, Any],
        pot: dict[str, Any],
        delivered_volume_ml: float,
        scheduled_end: datetime,
    ) -> None:
        volume_l = float(pot["volume_l"])
        retention = max(float(pot["retention_factor"]), 0.1)
        moisture_gain = max(0.0, delivered_volume_ml) * retention / max(volume_l * 10.0, 1.0)
        state["moisture"] = clamp(state["moisture"] + moisture_gain, 0.0, 100.0)
        if _as_local(scheduled_end) > _as_local(state["last_recorded_at"]):
            state["last_recorded_at"] = _as_local(scheduled_end)

    def _feedback_row(
        self,
        pot: dict[str, Any],
        state: dict[str, Any],
        recorded_at: datetime,
        delivery: dict[str, Any],
    ) -> dict[str, Any]:
        weather = sensor_rows.load_latest_weather_at(recorded_at) or sensor_rows.fallback_weather(recorded_at)
        air_temperature = sensor_rows.microclimate_temperature(pot, weather, recorded_at)
        humidity = number(weather.get("relative_humidity_pct"), 60.0)
        return {
            "sensor_id": pot["id"],
            "recorded_at": _db_timestamp(recorded_at),
            "soil_moisture_pct": round(state["moisture"], 2),
            "air_temperature_c": round(air_temperature, 2),
            "air_humidity_pct": round(humidity, 2),
            "substrate_temperature_c": round(air_temperature + sensor_rows.substrate_delta(pot, recorded_at), 2),
            "source": ACTUATOR_FEEDBACK_SOURCE,
            "reading_resolution": RAW_RESOLUTION,
            "sample_count": 1,
        }

    def _schedule_corrective_actuations(
        self,
        conn,
        below_minimum: list[dict[str, Any]],
        feedback_at: datetime,
    ) -> list[dict[str, Any]]:
        corrective_rows = []
        for item in below_minimum:
            pot = item["pot"]
            prescription = self._corrective_prescription(conn, pot, item["moisture_pct"], feedback_at)
            if not prescription:
                continue
            event_row = conn.execute(
                """
                INSERT INTO irrigation_events (
                    experiment_type, sensor_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, valve_number, valve_zone,
                    payload, cycle_count, soak_pause_min, status
                )
                VALUES (
                    %(experiment_type)s, %(pot_id)s, %(scheduled_start_at)s,
                    %(scheduled_end_at)s, %(flow_rate_ml_min)s, %(planned_volume_ml)s,
                    %(valve_number)s, %(valve_zone)s, %(payload)s, 1, 0, 'planned'
                )
                ON CONFLICT (experiment_type, sensor_id, scheduled_start_at) DO UPDATE SET
                    scheduled_end_at = EXCLUDED.scheduled_end_at,
                    flow_rate_ml_min = EXCLUDED.flow_rate_ml_min,
                    planned_volume_ml = EXCLUDED.planned_volume_ml,
                    valve_number = EXCLUDED.valve_number,
                    valve_zone = EXCLUDED.valve_zone,
                    payload = EXCLUDED.payload,
                    status = CASE
                        WHEN irrigation_events.status IN ('completed', 'running') THEN irrigation_events.status
                        ELSE EXCLUDED.status
                    END,
                    changed_at = now()
                RETURNING id, status
                """,
                prescription,
            ).fetchone()
            if not event_row:
                continue
            actuation_row = conn.execute(
                """
                INSERT INTO irrigation_actuations (
                    event_id, experiment_type, pot_id, scheduled_start_at, scheduled_end_at,
                    flow_rate_ml_min, planned_volume_ml, valve_number, valve_zone,
                    payload, cycle_count, soak_pause_min, status
                )
                VALUES (
                    %(event_id)s, %(experiment_type)s, %(pot_id)s, %(scheduled_start_at)s,
                    %(scheduled_end_at)s, %(flow_rate_ml_min)s, %(planned_volume_ml)s,
                    %(valve_number)s, %(valve_zone)s, %(payload)s, 1, 0, 'planned'
                )
                ON CONFLICT (experiment_type, pot_id, scheduled_start_at) DO UPDATE SET
                    event_id = EXCLUDED.event_id,
                    scheduled_end_at = EXCLUDED.scheduled_end_at,
                    flow_rate_ml_min = EXCLUDED.flow_rate_ml_min,
                    planned_volume_ml = EXCLUDED.planned_volume_ml,
                    valve_number = EXCLUDED.valve_number,
                    valve_zone = EXCLUDED.valve_zone,
                    payload = EXCLUDED.payload,
                    status = CASE
                        WHEN irrigation_actuations.status IN ('completed', 'running') THEN irrigation_actuations.status
                        ELSE EXCLUDED.status
                    END,
                    changed_at = now()
                RETURNING id, status
                """,
                {**prescription, "event_id": event_row["id"]},
            ).fetchone()
            if actuation_row:
                corrective_rows.append(
                    {
                        "potId": pot["id"],
                        "potCode": pot["pot_code"],
                        "valveNumber": prescription["valve_number"],
                        "scheduledStartAt": prescription["scheduled_start_at"].isoformat(),
                        "plannedVolumeMl": prescription["planned_volume_ml"],
                    }
                )
        return corrective_rows

    def _corrective_prescription(
        self,
        conn,
        pot: dict[str, Any],
        current_moisture: float,
        feedback_at: datetime,
    ) -> dict[str, Any] | None:
        if self._has_same_day_corrective(conn, int(pot["id"]), feedback_at):
            return None
        planned_volume_ml = self._corrective_volume_ml(pot, current_moisture)
        if planned_volume_ml < 10.0:
            return None
        flow_rate = max(float(pot["drip_flow_ml_min"]), 1.0)
        duration_min = max(1.0, planned_volume_ml / flow_rate)
        start_at = self._corrective_start_at(conn, pot, feedback_at, duration_min)
        if not start_at:
            return None
        end_at = start_at + timedelta(minutes=duration_min)
        valve_number = sensor_rows.valve_number_for_zone(str(pot["balcony_zone"]))
        payload = {
            "reason": "post_actuation_below_minimum",
            "pot_id": int(pot["id"]),
            "pot_code": pot["pot_code"],
            "current_moisture_pct": round(current_moisture, 2),
            "moisture_min_pct": round(float(pot["moisture_min_pct"]), 2),
            "target_moisture_pct": round(float(pot["moisture_target_pct"]), 2),
            "physical_distribution_policy": "corrective_pot_refill",
        }
        return {
            "experiment_type": self.corrective_experiment_type,
            "pot_id": int(pot["id"]),
            "scheduled_start_at": start_at,
            "scheduled_end_at": end_at,
            "flow_rate_ml_min": round(flow_rate, 2),
            "planned_volume_ml": round(planned_volume_ml, 2),
            "valve_number": valve_number,
            "valve_zone": pot["balcony_zone"],
            "payload": Jsonb(payload),
        }

    def _corrective_volume_ml(self, pot: dict[str, Any], current_moisture: float) -> float:
        target = float(pot["moisture_target_pct"])
        need_pct = max(0.0, target - current_moisture)
        volume_l = float(pot["volume_l"])
        retention = max(float(pot["retention_factor"]), 0.1)
        flow_rate = max(float(pot["drip_flow_ml_min"]), 1.0)
        max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}.get(str(pot["size_class"]), 35)
        return min(need_pct * volume_l * 10.0 / retention, flow_rate * max_minutes)

    def _corrective_start_at(
        self,
        conn,
        pot: dict[str, Any],
        feedback_at: datetime,
        duration_min: float,
    ) -> datetime | None:
        start_after = _ceil_to_interval(_as_local(feedback_at) + timedelta(minutes=self.corrective_delay_minutes), 15)
        hot_day = sensor_rows.is_hot_irrigation_day(conn, start_after.date())
        windows = [
            (pot["morning_window_start"], pot["morning_window_end"], True),
            (pot["evening_window_start"], pot["evening_window_end"], hot_day or bool(pot.get("allows_second_watering"))),
        ]
        for start_time, end_time, allowed in windows:
            if not allowed:
                continue
            window_start = datetime.combine(start_after.date(), start_time, tzinfo=LOCAL_TZ)
            window_end = datetime.combine(start_after.date(), end_time, tzinfo=LOCAL_TZ)
            candidate = max(start_after, window_start)
            if candidate + timedelta(minutes=duration_min) <= window_end:
                return candidate
        return None

    def _has_same_day_corrective(self, conn, pot_id: int, feedback_at: datetime) -> bool:
        day_start = datetime.combine(_as_local(feedback_at).date(), time.min, tzinfo=LOCAL_TZ)
        day_end = day_start + timedelta(days=1)
        row = conn.execute(
            """
            SELECT 1
            FROM irrigation_actuations
            WHERE experiment_type = %(experiment_type)s
              AND pot_id = %(pot_id)s
              AND scheduled_start_at >= %(day_start)s
              AND scheduled_start_at < %(day_end)s
              AND status IN ('planned', 'running', 'completed')
            LIMIT 1
            """,
            {
                "experiment_type": self.corrective_experiment_type,
                "pot_id": pot_id,
                "day_start": day_start,
                "day_end": day_end,
            },
        ).fetchone()
        return bool(row)

