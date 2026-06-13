from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_twin.core.time import local_timezone, now_local
from digital_twin.infrastructure.database.connection import get_connection

PHYSICAL_ACTUATION_EXPERIMENT_TYPE = "baseline"


def _today_window(current: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    now = _local_datetime(current or now_local())
    today_start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return now, today_start, today_start + timedelta(days=1)


def _local_datetime(value: str | datetime) -> datetime:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        local_value = value
    if local_value.tzinfo is None:
        return local_value.replace(tzinfo=local_timezone())
    return local_value.astimezone(local_timezone())


def _first_int(values: Any) -> int | None:
    if values is None:
        return None
    if isinstance(values, (list, tuple)):
        candidates = values
    else:
        candidates = [values]
    for value in candidates:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _representative_pot_id(event: dict[str, Any]) -> int | None:
    return (
        _first_int(event.get("affected_pot_ids"))
        or _first_int(event.get("managed_pot_ids"))
        or _first_int(event.get("trigger_pot_ids"))
        or _first_int(event.get("zone_trigger_pot_ids"))
        or _first_int(event.get("pot_id"))
        or _first_int(event.get("sensor_id"))
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _experiment_run_row(
    experiment_type: str,
    start_date: date,
    end_date: date,
    parameters: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    summary = result.get("summary") or {}
    completed_at = now_local()
    execution_seconds = max(0.0, _float(summary.get("execution_time_seconds"), 0.0))
    started_at = completed_at - timedelta(seconds=execution_seconds)
    return {
        "experiment_type": experiment_type,
        "start_date": start_date,
        "end_date": end_date,
        "computed_at": completed_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "parameters": Jsonb(_json_ready(parameters or {})),
        "summary": Jsonb(_json_ready(summary)),
        "payload": Jsonb(_json_ready(result)),
    }


def _prescription_event_row(prescription: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    start_raw = event.get("scheduled_start_at")
    if not start_raw:
        return None

    pot_id = _representative_pot_id(event)
    if pot_id is None:
        return None

    planned_volume_ml = float(event.get("planned_volume_ml") or 0.0)
    flow_rate_ml_min = float(event.get("flow_rate_ml_min") or 0.0)
    duration_min = float(event.get("duration_min") or 0.0)
    if flow_rate_ml_min <= 0.0 and planned_volume_ml > 0.0 and duration_min > 0.0:
        flow_rate_ml_min = planned_volume_ml / duration_min
    if planned_volume_ml <= 0.0 or flow_rate_ml_min <= 0.0:
        return None

    scheduled_start_at = _local_datetime(start_raw)
    if event.get("scheduled_end_at"):
        scheduled_end_at = _local_datetime(event["scheduled_end_at"])
    else:
        duration = duration_min if duration_min > 0.0 else planned_volume_ml / flow_rate_ml_min
        scheduled_end_at = scheduled_start_at + timedelta(minutes=max(duration, 1.0))
    if scheduled_end_at <= scheduled_start_at:
        scheduled_end_at = scheduled_start_at + timedelta(minutes=1)

    return {
        "experiment_type": prescription["experiment_type"],
        "sensor_id": int(event.get("sensor_id") or pot_id),
        "pot_id": pot_id,
        "scheduled_start_at": scheduled_start_at,
        "scheduled_end_at": scheduled_end_at,
        "flow_rate_ml_min": round(flow_rate_ml_min, 2),
        "planned_volume_ml": round(planned_volume_ml, 2),
        "cycle_count": int(event.get("cycle_count") or 1),
        "soak_pause_min": int(event.get("soak_pause_min") or 0),
        "prescription_id": int(prescription["id"]),
        "prescription_date": prescription["prescription_date"],
        "valve_zone": event.get("valve_zone"),
        "valve_number": event.get("valve_number"),
        "payload": Jsonb(event),
    }


class ExperimentRunRepository:
    """Persistence boundary for reproducible experiment result snapshots."""

    def create(
        self,
        *,
        experiment_type: str,
        start_date: date,
        end_date: date,
        parameters: dict[str, Any] | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        row = _experiment_run_row(experiment_type, start_date, end_date, parameters, result)
        with get_connection(row_factory=dict_row) as conn:
            saved = conn.execute(
                """
                INSERT INTO experiment_runs (
                    experiment_type, start_date, end_date, computed_at, started_at, completed_at,
                    parameters, summary, payload
                )
                VALUES (
                    %(experiment_type)s, %(start_date)s, %(end_date)s,
                    %(computed_at)s, %(started_at)s, %(completed_at)s,
                    %(parameters)s, %(summary)s, %(payload)s
                )
                RETURNING id, experiment_type, start_date, end_date, computed_at, started_at, completed_at, parameters, summary, created_at
                """,
                row,
            ).fetchone()
            conn.commit()
            return saved

    def latest(
        self,
        *,
        experiment_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with get_connection(row_factory=dict_row) as conn:
            if experiment_type:
                return conn.execute(
                    """
                    SELECT id, experiment_type, start_date, end_date, computed_at, started_at, completed_at, parameters, summary, created_at
                    FROM experiment_runs
                    WHERE experiment_type = %(experiment_type)s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %(limit)s
                    """,
                    {"experiment_type": experiment_type, "limit": limit},
                ).fetchall()
            return conn.execute(
                """
                SELECT id, experiment_type, start_date, end_date, computed_at, started_at, completed_at, parameters, summary, created_at
                FROM experiment_runs
                ORDER BY created_at DESC, id DESC
                LIMIT %(limit)s
                """,
                {"limit": limit},
            ).fetchall()

    def get(self, run_id: int) -> dict[str, Any] | None:
        with get_connection(row_factory=dict_row) as conn:
            return conn.execute(
                """
                SELECT *
                FROM experiment_runs
                WHERE id = %(id)s
                """,
                {"id": int(run_id)},
            ).fetchone()


class ActuationRepository:
    """Persistence boundary for planned irrigation actuator work."""

    def store_prescriptions(self, prescriptions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not prescriptions:
            return []
        rows = [{**prescription, "payload": prescription.get("payload") or {}} for prescription in prescriptions]
        with get_connection(row_factory=dict_row) as conn:
            stored = conn.execute(
                """
                WITH input_rows AS (
                    SELECT *
                    FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
                        experiment_type text,
                        prescription_date date,
                        computed_at timestamptz,
                        dispatched_at timestamptz,
                        planned_volume_ml numeric,
                        valve_runs integer,
                        payload jsonb
                    )
                )
                INSERT INTO irrigation_prescriptions (
                    experiment_type, prescription_date, computed_at, dispatched_at,
                    planned_volume_ml, valve_runs, payload, status
                )
                SELECT
                    experiment_type, prescription_date, computed_at, dispatched_at,
                    planned_volume_ml, valve_runs, payload, 'dispatched'
                FROM input_rows
                ON CONFLICT (experiment_type, prescription_date) DO UPDATE SET
                    computed_at = EXCLUDED.computed_at,
                    dispatched_at = EXCLUDED.dispatched_at,
                    planned_volume_ml = EXCLUDED.planned_volume_ml,
                    valve_runs = EXCLUDED.valve_runs,
                    payload = EXCLUDED.payload,
                    status = EXCLUDED.status,
                    changed_at = now()
                RETURNING *
                """,
                {"rows": Jsonb(rows)},
            ).fetchall()
            conn.commit()
            return stored

    def materialize_due_prescription_events(
        self,
        current: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        now, today_start, today_end = _today_window(current)
        with get_connection(row_factory=dict_row) as conn:
            prescriptions = conn.execute(
                """
                SELECT id, experiment_type, prescription_date, payload
                FROM irrigation_prescriptions
                WHERE status = 'dispatched'
                  AND prescription_date = %(today)s
                  AND experiment_type = %(experiment_type)s
                ORDER BY experiment_type
                """,
                {
                    "today": today_start.date(),
                    "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE,
                },
            ).fetchall()

            materialized = []
            for prescription in prescriptions:
                payload = prescription.get("payload") or {}
                for event in payload.get("events") or []:
                    row = _prescription_event_row(prescription, event)
                    if not row:
                        continue
                    if not (today_start <= row["scheduled_start_at"] < today_end):
                        continue
                    if row["scheduled_start_at"] > now:
                        continue
                    event_row = conn.execute(
                        """
                        INSERT INTO irrigation_events (
                            experiment_type, sensor_id, scheduled_start_at, scheduled_end_at,
                            flow_rate_ml_min, planned_volume_ml, valve_number, valve_zone,
                            payload, cycle_count, soak_pause_min, status
                        )
                        VALUES (
                            %(experiment_type)s, %(sensor_id)s, %(scheduled_start_at)s,
                            %(scheduled_end_at)s, %(flow_rate_ml_min)s, %(planned_volume_ml)s,
                            %(valve_number)s, %(valve_zone)s, %(payload)s,
                            %(cycle_count)s, %(soak_pause_min)s, 'planned'
                        )
                        ON CONFLICT (experiment_type, sensor_id, scheduled_start_at) DO UPDATE SET
                            scheduled_end_at = EXCLUDED.scheduled_end_at,
                            flow_rate_ml_min = EXCLUDED.flow_rate_ml_min,
                            planned_volume_ml = EXCLUDED.planned_volume_ml,
                            valve_number = EXCLUDED.valve_number,
                            valve_zone = EXCLUDED.valve_zone,
                            payload = EXCLUDED.payload,
                            cycle_count = EXCLUDED.cycle_count,
                            soak_pause_min = EXCLUDED.soak_pause_min,
                            status = CASE
                                WHEN irrigation_events.status IN ('completed', 'running') THEN irrigation_events.status
                                ELSE EXCLUDED.status
                            END,
                            changed_at = now()
                        WHERE irrigation_events.status NOT IN ('completed', 'running')
                        RETURNING id, status
                        """,
                        row,
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
                            %(valve_number)s, %(valve_zone)s, %(payload)s,
                            %(cycle_count)s, %(soak_pause_min)s, 'planned'
                        )
                        ON CONFLICT (experiment_type, pot_id, scheduled_start_at) DO UPDATE SET
                            event_id = EXCLUDED.event_id,
                            scheduled_end_at = EXCLUDED.scheduled_end_at,
                            flow_rate_ml_min = EXCLUDED.flow_rate_ml_min,
                            planned_volume_ml = EXCLUDED.planned_volume_ml,
                            valve_number = EXCLUDED.valve_number,
                            valve_zone = EXCLUDED.valve_zone,
                            payload = EXCLUDED.payload,
                            cycle_count = EXCLUDED.cycle_count,
                            soak_pause_min = EXCLUDED.soak_pause_min,
                            status = CASE
                                WHEN irrigation_actuations.status IN ('completed', 'running') THEN irrigation_actuations.status
                                ELSE EXCLUDED.status
                            END,
                            changed_at = now()
                        WHERE irrigation_actuations.status NOT IN ('completed', 'running', 'failed')
                        RETURNING id, status
                        """,
                        {**row, "event_id": event_row["id"]},
                    ).fetchone()
                    if not actuation_row:
                        continue
                    materialized.append(
                        {
                            "prescriptionId": row["prescription_id"],
                            "experimentType": row["experiment_type"],
                            "prescriptionDate": row["prescription_date"].isoformat(),
                            "eventId": event_row["id"],
                            "eventStatus": event_row["status"],
                            "actuationId": actuation_row["id"],
                            "actuationStatus": actuation_row["status"],
                            "potId": row["pot_id"],
                            "valveZone": row["valve_zone"],
                            "valveNumber": row["valve_number"],
                            "scheduledStartAt": row["scheduled_start_at"].isoformat(),
                            "plannedVolumeMl": row["planned_volume_ml"],
                        }
                    )
                    if len(materialized) >= limit:
                        conn.commit()
                        return materialized
            conn.commit()
            return materialized

    def due(self, limit: int = 100) -> list[dict[str, Any]]:
        now, today_start, today_end = _today_window()
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    ia.*,
                    p.pot_code,
                    p.label AS pot_label
                FROM irrigation_actuations ia
                JOIN pots p ON p.id = ia.pot_id
                WHERE ia.status = 'planned'
                  AND ia.experiment_type = %(experiment_type)s
                  AND ia.scheduled_start_at >= %(today_start)s
                  AND ia.scheduled_start_at < %(today_end)s
                  AND ia.scheduled_end_at <= %(now)s
                ORDER BY ia.scheduled_start_at, ia.id
                LIMIT %(limit)s
                """,
                {
                    "limit": limit,
                    "now": now,
                    "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE,
                    "today_start": today_start,
                    "today_end": today_end,
                },
            ).fetchall()
            return rows

    def mark_completed(self, actuation_id: int, actuator_node: str) -> dict[str, Any]:
        now, today_start, today_end = _today_window()
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                UPDATE irrigation_actuations
                SET status = 'completed',
                    actuator_node = %(actuator_node)s,
                    started_at = COALESCE(started_at, now()),
                    completed_at = now(),
                    delivered_volume_ml = planned_volume_ml,
                    last_error = NULL,
                    changed_at = now()
                WHERE id = %(id)s
                  AND status = 'planned'
                  AND scheduled_start_at >= %(today_start)s
                  AND scheduled_start_at < %(today_end)s
                  AND scheduled_end_at <= %(now)s
                RETURNING *
                """,
                {
                    "id": actuation_id,
                    "actuator_node": actuator_node,
                    "now": now,
                    "today_start": today_start,
                    "today_end": today_end,
                },
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE irrigation_events
                    SET status = 'completed',
                        changed_at = now()
                    WHERE id = %(event_id)s
                      AND status = 'planned'
                    """,
                    {"event_id": row["event_id"]},
                )
            conn.commit()
            return row

    def mark_failed(self, actuation_id: int, actuator_node: str, error: str) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                UPDATE irrigation_actuations
                SET status = 'failed',
                    actuator_node = %(actuator_node)s,
                    last_error = %(error)s,
                    changed_at = now()
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": actuation_id, "actuator_node": actuator_node, "error": error},
            ).fetchone()
            conn.commit()
            return row

    def summary(self) -> dict[str, Any]:
        _, today_start, today_end = _today_window()
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT status, count(*) AS count
                FROM irrigation_actuations
                WHERE scheduled_start_at >= %(today_start)s
                  AND scheduled_start_at < %(today_end)s
                  AND experiment_type = %(experiment_type)s
                GROUP BY status
                ORDER BY status
                """,
                {
                    "today_start": today_start,
                    "today_end": today_end,
                    "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE,
                },
            ).fetchall()
            prescriptions = conn.execute(
                """
                SELECT experiment_type, prescription_date, planned_volume_ml, valve_runs, status, dispatched_at
                FROM irrigation_prescriptions
                WHERE prescription_date = %(today)s
                   OR (dispatched_at >= %(today_start)s AND dispatched_at < %(today_end)s)
                ORDER BY prescription_date, experiment_type
                """,
                {"today": today_start.date(), "today_start": today_start, "today_end": today_end},
            ).fetchall()
            return {"date": today_start.date().isoformat(), "actuations": rows, "prescriptions": prescriptions}
