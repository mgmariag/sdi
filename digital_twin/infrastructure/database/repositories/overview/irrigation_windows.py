from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from digital_twin.application.clock import ApplicationClock
from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.repositories.overview._common import (
    PHYSICAL_ACTUATION_EXPERIMENT_TYPE,
    number as _number,
)
from digital_twin.infrastructure.database.repositories.overview.activity import activity_window

_clock = ApplicationClock()


def next_planned_irrigation(conn, now: datetime) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT scheduled_start_at
        FROM irrigation_actuations
        WHERE status = 'planned'
          AND experiment_type = %(experiment_type)s
          AND scheduled_start_at AT TIME ZONE 'Europe/Bucharest' > %(now)s
        ORDER BY scheduled_start_at
        LIMIT 1
        """,
        {"now": now, "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE},
    ).fetchone()
    if row:
        return _planned_irrigation_window(
            conn,
            table_name="irrigation_actuations",
            scheduled_start_at=row["scheduled_start_at"],
            source="actuation",
        )

    return None


def _planned_irrigation_window(conn, table_name: str, scheduled_start_at: datetime, source: str) -> dict[str, Any] | None:
    if table_name != "irrigation_actuations":
        return None

    row = conn.execute(
        f"""
        SELECT
            min(scheduled_start_at AT TIME ZONE 'Europe/Bucharest') AS start_at,
            max(scheduled_end_at AT TIME ZONE 'Europe/Bucharest') AS end_at,
            count(*) AS item_count,
            coalesce(sum(planned_volume_ml), 0) AS planned_volume_ml
        FROM {table_name}
        WHERE status = 'planned'
          AND experiment_type = %(experiment_type)s
          AND scheduled_start_at = %(scheduled_start_at)s
        """,
        {
            "scheduled_start_at": scheduled_start_at,
            "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE,
        },
    ).fetchone()
    if not row or not row["start_at"] or not row["end_at"]:
        return None

    start = row["start_at"]
    end = row["end_at"]
    return {
        "label": f"{start:%Y-%m-%d %H:%M} - {end:%H:%M}",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "source": source,
        "item_count": int(row["item_count"] or 0),
        "planned_volume_l": round(_number(row["planned_volume_ml"], 0.0) / 1000.0, 2),
    }


def next_prescription_irrigation(conn, now: datetime) -> dict[str, Any] | None:
    prescriptions = conn.execute(
        """
        SELECT experiment_type, prescription_date, payload
        FROM irrigation_prescriptions
        WHERE status = 'dispatched'
          AND experiment_type = %(experiment_type)s
          AND prescription_date > %(today)s
        ORDER BY prescription_date, experiment_type
        """,
        {"today": now.date(), "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE},
    ).fetchall()
    candidates = []
    for prescription in prescriptions:
        payload = prescription.get("payload") or {}
        for event in payload.get("events") or []:
            start = _parse_local_datetime(event.get("scheduled_start_at"))
            if not start or start <= now:
                continue
            candidates.append(
                {
                    "experiment_type": prescription.get("experiment_type"),
                    "start_at": start,
                    "end_at": _event_end_at(event, start),
                    "planned_volume_ml": _number(event.get("planned_volume_ml"), 0.0),
                    "valve_number": event.get("valve_number"),
                    "valve_zone": event.get("valve_zone"),
                }
            )
    if not candidates:
        return None

    next_start = min(item["start_at"] for item in candidates)
    window_events = [item for item in candidates if item["start_at"] == next_start]
    return activity_window(
        window_events,
        source="prescription",
        mode="next_planned",
        display_label="Next planned irrigation",
    )


def most_recent_irrigation(conn, now: datetime) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT scheduled_start_at
        FROM irrigation_actuations
        WHERE status IN ('running', 'completed')
          AND experiment_type = %(experiment_type)s
          AND scheduled_start_at AT TIME ZONE 'Europe/Bucharest' <= %(now)s
        ORDER BY coalesce(completed_at, scheduled_end_at, scheduled_start_at) DESC
        LIMIT 1
        """,
        {"now": now, "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE},
    ).fetchone()
    if not row:
        return None

    rows = conn.execute(
        """
        SELECT
            experiment_type,
            scheduled_start_at AT TIME ZONE 'Europe/Bucharest' AS start_at,
            scheduled_end_at AT TIME ZONE 'Europe/Bucharest' AS end_at,
            coalesce(delivered_volume_ml, planned_volume_ml, 0) AS planned_volume_ml,
            valve_number,
            valve_zone
        FROM irrigation_actuations
        WHERE status IN ('running', 'completed')
          AND experiment_type = %(experiment_type)s
          AND scheduled_start_at = %(scheduled_start_at)s
        ORDER BY experiment_type, valve_number, pot_id
        """,
        {
            "scheduled_start_at": row["scheduled_start_at"],
            "experiment_type": PHYSICAL_ACTUATION_EXPERIMENT_TYPE,
        },
    ).fetchall()
    return activity_window(
        rows,
        source="actuation_history",
        mode="most_recent",
        display_label="Most recent irrigation",
    )


def next_recommendation_ready_at(now: datetime) -> datetime:
    candidate = datetime.combine(now.date(), get_settings().prescription_dispatch_time)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _parse_local_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(_clock.local_timezone()).replace(tzinfo=None)


def _event_end_at(event: dict[str, Any], start: datetime) -> datetime:
    end = _parse_local_datetime(event.get("scheduled_end_at"))
    if end and end > start:
        return end

    duration_min = _number(event.get("duration_min"), 0.0)
    planned_volume_ml = _number(event.get("planned_volume_ml"), 0.0)
    flow_rate_ml_min = _number(event.get("flow_rate_ml_min"), 0.0)
    if duration_min <= 0.0 and planned_volume_ml > 0.0 and flow_rate_ml_min > 0.0:
        duration_min = planned_volume_ml / flow_rate_ml_min
    return start + timedelta(minutes=max(duration_min, 1.0))


def next_dt_planned_irrigation(next_window: dict[str, Any] | None, valve_plan: dict[str, Any]) -> dict[str, Any] | None:
    immediate_starts = int(valve_plan.get("valve_starts") or 0)
    if immediate_starts <= 0:
        return None
    if not next_window or not next_window.get("start_at") or not next_window.get("end_at"):
        return None

    try:
        start = datetime.fromisoformat(str(next_window["start_at"]))
        window_end = datetime.fromisoformat(str(next_window["end_at"]))
    except ValueError:
        return None

    runtime_min = _number(
        valve_plan.get("immediate_optimized_runtime_min"),
        _number(valve_plan.get("total_runtime_min"), 0.0),
    )
    planned_volume_l = _number(valve_plan.get("immediate_irrigation_volume_l"), 0.0)

    planned_end = start + timedelta(minutes=max(runtime_min, 0.0))
    if planned_end <= start or planned_end > window_end:
        planned_end = window_end

    return {
        "label": f"{start:%Y-%m-%d %H:%M} - {planned_end:%H:%M}",
        "start_at": start.isoformat(),
        "end_at": planned_end.isoformat(),
        "source": "digital_twin_immediate_plan",
        "item_count": immediate_starts,
        "planned_volume_l": round(planned_volume_l, 2),
    }


def next_irrigation_window(conn, now: datetime) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT morning_window_start, morning_window_end, evening_window_start, evening_window_end
        FROM pots
        WHERE active = true
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return {"label": "N/A", "start_at": None, "end_at": None}

    candidates = []
    for offset in range(2):
        day = now.date() + timedelta(days=offset)
        for start_key, end_key in (
            ("morning_window_start", "morning_window_end"),
            ("evening_window_start", "evening_window_end"),
        ):
            if start_key == "evening_window_start" and not _is_hot_irrigation_day(conn, day):
                continue
            start_time = row[start_key]
            end_time = row[end_key]
            if start_time and end_time:
                candidates.append((datetime.combine(day, start_time), datetime.combine(day, end_time)))
    future = [(start, end) for start, end in candidates if start > now]
    if not future:
        return {"label": "N/A", "start_at": None, "end_at": None}

    start, end = sorted(future, key=lambda item: item[0])[0]
    return {
        "label": f"{start:%Y-%m-%d %H:%M} - {end:%H:%M}",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
    }


def _is_hot_irrigation_day(conn, day) -> bool:
    row = conn.execute(
        """
        SELECT max(temperature_c) AS max_temperature_c
        FROM weather_hourly
        WHERE location_name = 'Cluj-Napoca'
          AND observed_local_at >= %(start_at)s
          AND observed_local_at < %(end_at)s
        """,
        {
            "start_at": datetime.combine(day, time.min),
            "end_at": datetime.combine(day + timedelta(days=1), time.min),
        },
    ).fetchone()
    return _number(row["max_temperature_c"] if row else None, 0.0) >= 32.0
