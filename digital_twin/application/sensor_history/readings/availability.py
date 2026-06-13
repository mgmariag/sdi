from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

import digital_twin.application.sensor_history.readings.shared as reading_shared
from digital_twin.application.sensor_history.readings.shared import (
    DAILY_READING_SLOTS_PER_DAY,
    LOCAL_TZ,
    sensor_equipped_pot_ids as _sensor_equipped_pot_ids,
    query_sources as _query_sources,
)
from digital_twin.domain.sensors import (
    ACTUAL_SENSOR_SOURCE,
    DAILY_RESOLUTION,
    DEFAULT_SENSOR_SOURCE,
    HOURLY_RESOLUTION,
    RAW_RESOLUTION,
)
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database

_align_to_reading_interval = reading_shared.align_to_reading_interval
_as_local = reading_shared.as_local
_db_timestamp = reading_shared.db_timestamp
_same_month_day = reading_shared.same_month_day
_slot_count = reading_shared.slot_count
_tiered_periods = reading_shared.tiered_periods
_today_local = reading_shared.today_local


def map_experiment_date_to_sensor_date(
    experiment_date: date,
    first_sensor_date: date,
    last_sensor_date: date,
) -> date:
    """Map any experiment date onto the latest available same month/day sensor date."""
    for year in range(last_sensor_date.year, first_sensor_date.year - 1, -1):
        candidate = _same_month_day(year, experiment_date)
        if first_sensor_date <= candidate <= last_sensor_date:
            return candidate
    raise ValueError("No compatible sensor date is available")


def map_experiment_date_to_available_sensor_date(
    experiment_date: date,
    available_dates: set[date],
) -> date:
    """Map an experiment date to an exact reading date when possible, otherwise same month/day."""
    if experiment_date in available_dates:
        return experiment_date
    if not available_dates:
        raise ValueError("No sensor date is available")
    first_sensor_date = min(available_dates)
    last_sensor_date = max(available_dates)
    for year in range(last_sensor_date.year, first_sensor_date.year - 1, -1):
        candidate = _same_month_day(year, experiment_date)
        if candidate in available_dates:
            return candidate
    raise ValueError("No compatible sensor date is available")


def get_sensor_availability(
    source: str = DEFAULT_SENSOR_SOURCE,
    sensor_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    initialize_database()
    now = datetime.now(LOCAL_TZ)
    sensor_filter = ""
    params: dict[str, Any] = {
        "source": source,
        "sources": _query_sources(source),
        "timezone": LOCAL_TZ.key,
        "now": _db_timestamp(now),
        "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
    }
    if sensor_ids:
        sensor_filter = "AND sensor_id = ANY(%(sensor_ids)s)"
        params["sensor_ids"] = sensor_ids
    with get_connection(row_factory=dict_row) as conn:
        row = conn.execute(
            f"""
            SELECT
                count(*) AS row_count,
                count(DISTINCT sensor_id) AS sensor_count,
                min(recorded_at::date) AS first_date,
                max(recorded_at::date) AS last_date
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at <= %(now)s
              {sensor_filter}
            """,
            params,
        ).fetchone()
    if not row or row["row_count"] == 0:
        return None
    return row


def _complete_sensor_dates_for_range(
    start_date: date,
    end_date: date,
    source: str,
    sensor_ids: list[int],
) -> set[date]:
    if not sensor_ids:
        return set()
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT recorded_at::date AS local_date
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND sensor_id = ANY(%(sensor_ids)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at::date >= %(start_date)s
              AND recorded_at::date <= %(end_date)s
            GROUP BY recorded_at::date
            HAVING count(DISTINCT sensor_id) >= %(sensor_count)s
               AND count(*) >= %(sensor_count)s * %(daily_slots_per_day)s
            """,
            {
                "sources": _query_sources(source),
                "sensor_ids": sensor_ids,
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
                "start_date": start_date,
                "end_date": end_date,
                "sensor_count": len(sensor_ids),
                "daily_slots_per_day": DAILY_READING_SLOTS_PER_DAY,
            },
        ).fetchall()
    return {row["local_date"] for row in rows}


def _available_sensor_dates(
    source: str,
    sensor_ids: list[int],
) -> set[date]:
    if not sensor_ids:
        return set()
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT recorded_at::date AS local_date
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND sensor_id = ANY(%(sensor_ids)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at <= %(now)s
            ORDER BY local_date
            """,
            {
                "sources": _query_sources(source),
                "sensor_ids": sensor_ids,
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
                "now": _db_timestamp(datetime.now(LOCAL_TZ)),
            },
        ).fetchall()
    return {row["local_date"] for row in rows}


def get_tiered_sensor_coverage(
    end_at: datetime | None = None,
    source: str = DEFAULT_SENSOR_SOURCE,
    sensor_ids: list[int] | None = None,
) -> dict[str, Any]:
    initialize_database()
    end_at = _align_to_reading_interval(_as_local(end_at or datetime.now(LOCAL_TZ)))
    period = _tiered_periods(end_at)
    sensor_ids = sensor_ids or _sensor_equipped_pot_ids()
    sensor_count = len(sensor_ids)
    raw_slots = _slot_count(period["raw_start"], end_at, minutes=15)
    hourly_slots = _slot_count(period["hourly_start"], period["raw_start"] - timedelta(hours=1), minutes=60)
    daily_days = max(0, (period["hourly_start"].date() - period["daily_start"].date()).days)
    daily_slots = daily_days * DAILY_READING_SLOTS_PER_DAY
    expected = {
        RAW_RESOLUTION: raw_slots * sensor_count,
        HOURLY_RESOLUTION: hourly_slots * sensor_count,
        DAILY_RESOLUTION: daily_slots * sensor_count,
    }
    params: dict[str, Any] = {
        "source": source,
        "sources": _query_sources(source),
        "sensor_ids": sensor_ids,
        "raw_resolution": RAW_RESOLUTION,
        "hourly_resolution": HOURLY_RESOLUTION,
        "daily_resolution": DAILY_RESOLUTION,
        "raw_start": _db_timestamp(period["raw_start"]),
        "end_at": _db_timestamp(end_at),
        "hourly_start": _db_timestamp(period["hourly_start"]),
        "daily_start_date": period["daily_start"].date(),
    }
    if not sensor_ids:
        return {
            "complete": False,
            "source": source,
            "sensor_count": 0,
            "daily_start": period["daily_start"].date(),
            "hourly_start": period["hourly_start"],
            "raw_start": period["raw_start"],
            "end_at": end_at,
            "expected": expected,
            "actual": {RAW_RESOLUTION: 0, HOURLY_RESOLUTION: 0, DAILY_RESOLUTION: 0},
        }

    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT reading_resolution, count(*)::int AS row_count
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND sensor_id = ANY(%(sensor_ids)s)
              AND (
                    (
                        reading_resolution = %(raw_resolution)s
                        AND recorded_at >= %(raw_start)s
                        AND recorded_at <= %(end_at)s
                    )
                 OR (
                        reading_resolution = %(hourly_resolution)s
                        AND recorded_at >= %(hourly_start)s
                        AND recorded_at < %(raw_start)s
                    )
                 OR (
                        reading_resolution = %(daily_resolution)s
                        AND recorded_at::date >= %(daily_start_date)s
                        AND recorded_at < %(hourly_start)s
                    )
              )
            GROUP BY reading_resolution
            """,
            params,
        ).fetchall()

    actual = {RAW_RESOLUTION: 0, HOURLY_RESOLUTION: 0, DAILY_RESOLUTION: 0}
    for row in rows:
        actual[row["reading_resolution"]] = int(row["row_count"])

    return {
        "complete": all(actual[resolution] >= expected[resolution] for resolution in expected),
        "source": source,
        "sensor_count": sensor_count,
        "daily_start": period["daily_start"].date(),
        "hourly_start": period["hourly_start"],
        "raw_start": period["raw_start"],
        "end_at": end_at,
        "expected": expected,
        "actual": actual,
    }


def load_sensor_readings_for_experiment(
    start_date: date,
    end_date: date,
    sensor_ids: list[int],
    source: str = DEFAULT_SENSOR_SOURCE,
) -> dict[str, Any]:
    if not sensor_ids:
        return {
            "available": False,
            "source": source,
            "lookup": {},
            "mapped_dates": {},
            "future_dates": [],
            "sensor_reading_dates": set(),
            "latest_states": {},
            "row_count": 0,
            "sensor_ids": [],
        }
    sensor_ids = _sensor_equipped_pot_ids(sensor_ids)
    if not sensor_ids:
        return {
            "available": False,
            "source": source,
            "lookup": {},
            "mapped_dates": {},
            "future_dates": [],
            "sensor_reading_dates": set(),
            "latest_states": {},
            "row_count": 0,
            "sensor_ids": [],
        }

    availability = get_sensor_availability(source, sensor_ids)
    if not availability:
        return {
            "available": False,
            "source": source,
            "lookup": {},
            "mapped_dates": {},
            "future_dates": [],
            "sensor_reading_dates": set(),
            "latest_states": {},
            "row_count": 0,
            "sensor_ids": sensor_ids,
        }

    first_sensor_date = availability["first_date"]
    last_sensor_date = availability["last_date"]
    available_dates = _available_sensor_dates(source, sensor_ids)
    sensor_reading_dates = _sensor_reading_dates_for_range(start_date, end_date, sensor_ids)
    today = _today_local()
    mapped_dates: dict[date, date] = {}
    future_dates: list[date] = []
    current = start_date
    while current <= end_date:
        if current > today:
            future_dates.append(current)
        else:
            mapped_dates[current] = map_experiment_date_to_available_sensor_date(current, available_dates)
        current += timedelta(days=1)

    sensor_dates = sorted(set(mapped_dates.values()))
    with get_connection(row_factory=dict_row) as conn:
        rows = []
        if sensor_dates:
            now = datetime.now(LOCAL_TZ)
            rows = conn.execute(
                """
                WITH tiered AS (
                    SELECT
                        sensor_id,
                        recorded_at::date AS local_date,
                        recorded_at::time AS local_time,
                        max(recorded_at) AS recorded_at,
                        round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                        round(avg(air_temperature_c), 2) AS air_temperature_c,
                        round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                        round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                        CASE
                            WHEN bool_or(source = %(actual_source)s) THEN %(actual_source)s
                            ELSE %(source)s
                        END AS source,
                        %(raw_resolution)s AS resolution,
                        sum(sample_count)::int AS sample_count,
                        1 AS tier_priority
                    FROM sensor_readings
                    WHERE source = ANY(%(sources)s)
                      AND sensor_id = ANY(%(sensor_ids)s)
                      AND reading_resolution = %(raw_resolution)s
                      AND recorded_at::date = ANY(%(sensor_dates)s)
                      AND recorded_at <= %(now)s
                    GROUP BY sensor_id, local_date, local_time
                    UNION ALL
                    SELECT
                        sensor_id,
                        recorded_at::date AS local_date,
                        recorded_at::time AS local_time,
                        recorded_at,
                        soil_moisture_pct,
                        air_temperature_c,
                        air_humidity_pct,
                        substrate_temperature_c,
                        source,
                        %(hourly_resolution)s AS resolution,
                        sample_count,
                        2 AS tier_priority
                    FROM sensor_readings
                    WHERE source = ANY(%(sources)s)
                      AND sensor_id = ANY(%(sensor_ids)s)
                      AND reading_resolution = %(hourly_resolution)s
                      AND recorded_at::date = ANY(%(sensor_dates)s)
                      AND recorded_at <= %(now)s
                    UNION ALL
                    SELECT
                        sensor_id,
                        recorded_at::date AS local_date,
                        recorded_at::time AS local_time,
                        recorded_at,
                        soil_moisture_pct,
                        air_temperature_c,
                        air_humidity_pct,
                        substrate_temperature_c,
                        source,
                        %(daily_resolution)s AS resolution,
                        sample_count,
                        3 AS tier_priority
                    FROM sensor_readings
                    WHERE source = ANY(%(sources)s)
                      AND sensor_id = ANY(%(sensor_ids)s)
                      AND reading_resolution = %(daily_resolution)s
                      AND recorded_at::date = ANY(%(sensor_dates)s)
                      AND recorded_at <= %(now)s
                )
                SELECT DISTINCT ON (sensor_id, local_date, local_time)
                    sensor_id,
                    local_date,
                    local_time,
                    recorded_at,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    source,
                    resolution,
                    sample_count
                FROM tiered
                ORDER BY sensor_id, local_date, local_time, tier_priority, recorded_at DESC
                """,
                {
                    "timezone": LOCAL_TZ.key,
                    "source": source,
                    "sources": _query_sources(source),
                    "actual_source": ACTUAL_SENSOR_SOURCE,
                    "sensor_ids": sensor_ids,
                    "raw_resolution": RAW_RESOLUTION,
                    "hourly_resolution": HOURLY_RESOLUTION,
                    "daily_resolution": DAILY_RESOLUTION,
                    "sensor_dates": sensor_dates,
                    "now": _db_timestamp(now),
                },
            ).fetchall()
        latest_rows = conn.execute(
            """
            SELECT DISTINCT ON (sensor_id)
                sensor_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                reading_resolution AS resolution,
                sample_count
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND sensor_id = ANY(%(sensor_ids)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at <= %(now)s
            ORDER BY
                sensor_id,
                recorded_at DESC,
                CASE reading_resolution
                    WHEN %(raw_resolution)s THEN 1
                    WHEN %(hourly_resolution)s THEN 2
                    ELSE 3
                END
            """,
            {
                "timezone": LOCAL_TZ.key,
                "sources": _query_sources(source),
                "sensor_ids": sensor_ids,
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
                "raw_resolution": RAW_RESOLUTION,
                "hourly_resolution": HOURLY_RESOLUTION,
                "now": _db_timestamp(datetime.now(LOCAL_TZ)),
            },
        ).fetchall()

    rows_by_sensor_key = {
        (row["local_date"], _time_key(row["local_time"]), row["sensor_id"]): row
        for row in rows
    }
    lookup = {}
    for experiment_date, sensor_date in mapped_dates.items():
        for row in rows:
            if row["local_date"] == sensor_date:
                _put_sensor_lookup_row(lookup, experiment_date, row)

    return {
        "available": True,
        "source": source,
        "lookup": lookup,
        "mapped_dates": mapped_dates,
        "future_dates": future_dates,
        "sensor_reading_dates": sensor_reading_dates,
        "latest_states": {row["sensor_id"]: row for row in latest_rows},
        "latest_state_at": max((_as_local(row["recorded_at"]) for row in latest_rows), default=None),
        "sensor_dates": sensor_dates,
        "sensor_ids": sensor_ids,
        "first_sensor_date": first_sensor_date,
        "last_sensor_date": last_sensor_date,
        "row_count": len(rows_by_sensor_key),
    }


def _sensor_reading_dates_for_range(
    start_date: date,
    end_date: date,
    sensor_ids: list[int],
) -> set[date]:
    if end_date < start_date:
        return set()
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT recorded_at::date AS local_date
            FROM sensor_readings
            WHERE recorded_at::date >= %(start_date)s
              AND recorded_at::date <= %(end_date)s
            ORDER BY local_date
            """,
            {
                "start_date": start_date,
                "end_date": end_date,
            },
        ).fetchall()
    return {row["local_date"] for row in rows}

def _time_key(value: time | str) -> time:
    if isinstance(value, str):
        value = time.fromisoformat(value)
    return value.replace(second=0, microsecond=0)


def _put_sensor_lookup_row(
    lookup: dict[tuple[Any, ...], dict[str, Any]],
    experiment_date: date,
    row: dict[str, Any],
) -> None:
    sensor_id = row["sensor_id"]
    local_time = _time_key(row["local_time"])
    exact_key = (experiment_date, local_time, sensor_id)
    lookup[exact_key] = row

    bucket_hour = _hour_bucket_for_time(local_time)
    hour_key = (experiment_date, bucket_hour, sensor_id)
    current = lookup.get(hour_key)
    if current is None:
        lookup[hour_key] = row
        return

    current_time = _time_key(current["local_time"])
    hour_start_minutes = bucket_hour * 60
    current_distance = abs(current_time.hour * 60 + current_time.minute - hour_start_minutes)
    incoming_distance = abs(local_time.hour * 60 + local_time.minute - hour_start_minutes)
    if incoming_distance < current_distance:
        lookup[hour_key] = row


def _hour_bucket_for_time(value: time) -> int:
    return min(23, (value.hour * 60 + value.minute + 30) // 60)

