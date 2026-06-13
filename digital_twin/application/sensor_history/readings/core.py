from __future__ import annotations

import argparse
import time as sleep_time
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

import digital_twin.application.sensor_history.readings.state_rows as sensor_rows
import digital_twin.application.sensor_history.readings.shared as reading_shared
from digital_twin.application.sensor_history.readings.shared import (
    DAILY_READING_TIMES,
    DAILY_RETENTION_DAYS,
    DEFAULT_HISTORY_START,
    HOURLY_RETENTION_DAYS,
    LOCAL_TZ,
    RAW_RETENTION_HOURS,
    query_sources as _query_sources,
)
from digital_twin.application.sensor_placement.sensor_placement_service import (
    SensorPlacementService,
)
from digital_twin.core.config import get_settings
from digital_twin.domain.sensors import (
    ACTUAL_READING_INTERVAL_MINUTES,
    ACTUAL_SENSOR_SOURCE,
    DAILY_RESOLUTION,
    DEFAULT_SENSOR_SOURCE,
    HOURLY_RESOLUTION,
    RAW_RESOLUTION,
)
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.simulation.soil_model import (
    local_observed_at,
    number,
)

_align_to_reading_interval = reading_shared.align_to_reading_interval
_as_local = reading_shared.as_local
_db_timestamp = reading_shared.db_timestamp
_next_scheduled_datetime = reading_shared.next_scheduled_datetime
_reading_interval_minutes = reading_shared.reading_interval_minutes
_scheduled_datetimes = reading_shared.scheduled_datetimes
_tiered_periods = reading_shared.tiered_periods
_today_local = reading_shared.today_local


def seed_historical_sensor_readings(
    start_date: date = DEFAULT_HISTORY_START,
    end_date: date | None = None,
    source: str = DEFAULT_SENSOR_SOURCE,
    batch_size: int = 5000,
) -> dict[str, Any]:
    """Generate scheduled readings for pots selected as sensor locations."""
    if end_date is None:
        end_date = _today_local()
    start_date = max(start_date, end_date - timedelta(days=DAILY_RETENTION_DAYS - 1))
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    initialize_database()
    pots = sensor_rows.load_pots()
    weather_rows = sensor_rows.load_weather(start_date, end_date)
    weather_index = 0
    latest_weather: dict[str, Any] | None = None

    states = sensor_rows.initial_sensor_states(pots)
    pending_rows: list[dict[str, Any]] = []
    inserted_or_updated = 0
    total_readings = 0

    current = datetime.combine(start_date, time(0, 0), tzinfo=LOCAL_TZ)
    interval_minutes = _reading_interval_minutes()
    interval_hours = interval_minutes / 60.0
    end_dt = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=LOCAL_TZ) - timedelta(minutes=interval_minutes)

    with get_connection() as conn:
        while current <= end_dt:
            while weather_index < len(weather_rows) and local_observed_at(weather_rows[weather_index]) <= current:
                latest_weather = weather_rows[weather_index]
                weather_index += 1
            weather = latest_weather or sensor_rows.fallback_weather(current)
            for pot in pots:
                sensor_rows.apply_hourly_environment(states[pot["id"]], pot, weather, current.date(), hours=interval_hours)

            for pot in pots:
                row = sensor_rows.sensor_row(pot, states[pot["id"]], weather, current, source)
                pending_rows.append(row)
                sensor_rows.apply_virtual_irrigation_if_due(states[pot["id"]], pot, weather, current)
                total_readings += 1

            if len(pending_rows) >= batch_size:
                inserted_or_updated += sensor_rows.upsert_sensor_rows(conn, pending_rows)
                pending_rows.clear()

            current += timedelta(minutes=interval_minutes)

        if pending_rows:
            inserted_or_updated += sensor_rows.upsert_sensor_rows(conn, pending_rows)
            pending_rows.clear()

        conn.commit()

    return {
        "source": source,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "pot_count": len(pots),
        "reading_interval_minutes": interval_minutes,
        "expected_readings": total_readings,
        "upserted_readings": inserted_or_updated,
        "weather_rows": len(weather_rows),
    }


def seed_tiered_sensor_readings(
    end_at: datetime | None = None,
    start_date: date | None = None,
    source: str = DEFAULT_SENSOR_SOURCE,
    batch_size: int = 5000,
    replace_existing: bool = True,
) -> dict[str, Any]:
    """Generate one year of tiered readings for the selected sensor locations."""
    initialize_database()
    placement = SensorPlacementService().ensure_default_if_missing()
    end_at = _align_to_reading_interval(_as_local(end_at or datetime.now(LOCAL_TZ)))
    start_date = start_date or (end_at.date() - timedelta(days=DAILY_RETENTION_DAYS))
    start_at = datetime.combine(start_date, time(0, 0), tzinfo=LOCAL_TZ)
    if end_at < start_at:
        raise ValueError("end_at must not be before start_date")

    pots = sensor_rows.load_pots()
    weather_rows = sensor_rows.load_weather(start_date, end_at.date())
    weather_index = 0
    latest_weather: dict[str, Any] | None = None
    states = sensor_rows.initial_sensor_states(pots)
    for state in states.values():
        state["last_recorded_at"] = start_at

    period = _tiered_periods(end_at)
    pending_rows: list[dict[str, Any]] = []
    inserted_or_updated = 0
    deleted_existing = 0
    by_resolution = {RAW_RESOLUTION: 0, HOURLY_RESOLUTION: 0, DAILY_RESOLUTION: 0}

    with get_connection() as conn:
        if replace_existing and pots:
            deleted_existing = conn.execute(
                """
                DELETE FROM sensor_readings
                WHERE source = %(source)s
                  AND recorded_at >= %(start_at)s
                  AND recorded_at <= %(end_at)s
                """,
                {
                    "source": source,
                    "start_at": _db_timestamp(start_at),
                    "end_at": _db_timestamp(end_at),
                },
            ).rowcount

        current = start_at
        while current <= end_at:
            while weather_index < len(weather_rows) and local_observed_at(weather_rows[weather_index]) <= current:
                latest_weather = weather_rows[weather_index]
                weather_index += 1
            weather = latest_weather or sensor_rows.fallback_weather(current)

            for pot in pots:
                sensor_rows.apply_hourly_environment(states[pot["id"]], pot, weather, current.date(), hours=0.25)

            resolution = _tiered_resolution_for_time(current, period)
            if resolution:
                sample_count = _sample_count_for_resolution(resolution)
                for pot in pots:
                    row = sensor_rows.sensor_row(pot, states[pot["id"]], weather, current, source)
                    row["reading_resolution"] = resolution
                    row["sample_count"] = sample_count
                    pending_rows.append(row)
                    by_resolution[resolution] += 1

                if len(pending_rows) >= batch_size:
                    inserted_or_updated += sensor_rows.upsert_sensor_rows(conn, pending_rows)
                    pending_rows.clear()

            for pot in pots:
                sensor_rows.apply_virtual_irrigation_if_due(states[pot["id"]], pot, weather, current)

            current += timedelta(minutes=15)

        if pending_rows:
            inserted_or_updated += sensor_rows.upsert_sensor_rows(conn, pending_rows)
            pending_rows.clear()

        conn.commit()

    return {
        "source": source,
        "start_date": start_date.isoformat(),
        "end_at": end_at.isoformat(),
        "raw_start": period["raw_start"].isoformat(),
        "hourly_start": period["hourly_start"].isoformat(),
        "daily_start": period["daily_start"].isoformat(),
        "pot_count": len(pots),
        "placement_sensor_count": placement.get("sensor_count", 0),
        "deleted_existing": deleted_existing,
        "upserted_readings": inserted_or_updated,
        "expected_readings": sum(by_resolution.values()),
        "by_resolution": by_resolution,
        "weather_rows": len(weather_rows),
    }


def ensure_tiered_sensor_readings(
    end_at: datetime | None = None,
    source: str = DEFAULT_SENSOR_SOURCE,
    cleanup: bool = True,
) -> dict[str, Any]:
    """Ensure simulated tiered readings cover the current local calendar periods."""
    from digital_twin.application.sensor_history.readings.availability import get_tiered_sensor_coverage

    initialize_database()
    end_at = _align_to_reading_interval(_as_local(end_at or datetime.now(LOCAL_TZ)))
    placement = SensorPlacementService().ensure_default_if_missing()
    sensor_ids = [int(item["pot_id"]) for item in placement.get("items", [])]
    coverage = get_tiered_sensor_coverage(end_at=end_at, source=source, sensor_ids=sensor_ids)
    seed = None
    if placement.get("changed") or not coverage["complete"]:
        seed = seed_tiered_sensor_readings(
            end_at=end_at,
            start_date=coverage["daily_start"],
            source=source,
            replace_existing=True,
        )
        coverage = get_tiered_sensor_coverage(end_at=end_at, source=source, sensor_ids=sensor_ids)

    cleanup_summary = aggregate_and_cleanup_sensor_readings(now=end_at, source=source) if cleanup else None
    return {
        "source": source,
        "end_at": end_at.isoformat(),
        "placement_sensor_count": placement.get("sensor_count", 0),
        "coverage": coverage,
        "seed": seed,
        "cleanup": cleanup_summary,
    }


def ensure_sensor_readings_for_experiment_range(
    start_date: date,
    end_date: date,
    source: str = DEFAULT_SENSOR_SOURCE,
) -> dict[str, Any]:
    """Ensure simulated readings exist for the non-future part of an experiment range."""
    initialize_database()
    placement = SensorPlacementService().ensure_default_if_missing()
    sensor_ids = [int(item["pot_id"]) for item in placement.get("items", [])]
    now = _align_to_reading_interval(datetime.now(LOCAL_TZ))
    generation_end = min(end_date, now.date())

    if not sensor_ids or generation_end < start_date:
        return {
            "source": source,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "generated": False,
            "missing_dates": [],
            "placement_sensor_count": len(sensor_ids),
        }

    requested_dates = [
        start_date + timedelta(days=offset)
        for offset in range((generation_end - start_date).days + 1)
    ]
    complete_dates = _complete_sensor_dates_for_range(start_date, generation_end, source, sensor_ids)
    missing_dates = [day for day in requested_dates if day not in complete_dates]
    seed = None
    if missing_dates:
        if generation_end == now.date():
            end_at = now
        else:
            end_at = datetime.combine(generation_end + timedelta(days=1), time.min, tzinfo=LOCAL_TZ) - timedelta(minutes=15)
        seed = seed_tiered_sensor_readings(
            end_at=end_at,
            start_date=start_date,
            source=source,
            replace_existing=True,
        )

    return {
        "source": source,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "generated": seed is not None,
        "missing_dates": [day.isoformat() for day in missing_dates],
        "placement_sensor_count": len(sensor_ids),
        "seed": seed,
    }


def generate_sensor_readings_at(
    recorded_at: datetime,
    source: str = DEFAULT_SENSOR_SOURCE,
) -> dict[str, Any]:
    """Generate one scheduled reading for every selected sensor location."""
    initialize_database()
    SensorPlacementService().ensure_default_if_missing()
    recorded_at = _align_to_reading_interval(_as_local(recorded_at))
    pots = sensor_rows.load_pots()
    previous = sensor_rows.load_latest_sensor_states(recorded_at, source)
    weather = sensor_rows.load_latest_weather_at(recorded_at) or sensor_rows.fallback_weather(recorded_at)

    rows = []
    for pot in pots:
        previous_row = previous.get(pot["id"])
        if previous_row:
            state = {
                "moisture": number(previous_row["soil_moisture_pct"], float(pot["moisture_target_pct"])),
                "last_recorded_at": _as_local(previous_row["recorded_at"]),
            }
            previous_weather = sensor_rows.load_latest_weather_at(state["last_recorded_at"]) or sensor_rows.fallback_weather(state["last_recorded_at"])
            sensor_rows.apply_virtual_irrigation_if_due(state, pot, previous_weather, state["last_recorded_at"])
        else:
            state = sensor_rows.initial_state_for_pot(pot)

        interval_minutes = _reading_interval_minutes()
        intervals_elapsed = max(1, min(24 * 60 // interval_minutes, int((recorded_at - state["last_recorded_at"]).total_seconds() // (interval_minutes * 60))))
        for _ in range(intervals_elapsed):
            sensor_rows.apply_hourly_environment(state, pot, weather, recorded_at.date(), hours=interval_minutes / 60.0)
        rows.append(sensor_rows.sensor_row(pot, state, weather, recorded_at, source))

    with get_connection() as conn:
        upserted = sensor_rows.upsert_sensor_rows(conn, rows, update_changed_at=source == ACTUAL_SENSOR_SOURCE)
        conn.commit()

    return {
        "source": source,
        "recorded_at": recorded_at.isoformat(),
        "pot_count": len(pots),
        "upserted_readings": upserted,
    }

# This method generates sensor readings for a specific timestamp, 
# applying any necessary state changes based on the elapsed time since the last reading.   
# Usage: generate_sensor_readings_at(datetime(2025, 5, 22, 12, 0), source="simulated_sensor")    
def generate_due_sensor_readings(
    now: datetime | None = None,
    source: str = DEFAULT_SENSOR_SOURCE,
) -> list[dict[str, Any]]:
    """Generate readings for scheduled times that are due today and missing."""
    now = _as_local(now or datetime.now(LOCAL_TZ))
    due_times = [item for item in _scheduled_datetimes(now.date()) if item <= now]
    if source == ACTUAL_SENSOR_SOURCE:
        if not due_times:
            return []
        latest_due = due_times[-1]
        return [] if sensor_rows.reading_exists(latest_due, source) else [generate_sensor_readings_at(latest_due, source=source)]

    results = []
    for scheduled_at in due_times:
        if not sensor_rows.reading_exists(scheduled_at, source):
            results.append(generate_sensor_readings_at(scheduled_at, source=source))
    return results


def ingest_actual_sensor_readings(
    readings: list[dict[str, Any]],
    recorded_at: datetime | None = None,
    source: str = ACTUAL_SENSOR_SOURCE,
) -> dict[str, Any]:
    """Store actual raw sensor readings in 15-minute local slots."""
    initialize_database()
    if source != ACTUAL_SENSOR_SOURCE:
        raise ValueError(f"actual sensor ingestion must use source={ACTUAL_SENSOR_SOURCE!r}")

    default_recorded_at = _as_local(recorded_at or datetime.now(LOCAL_TZ)).replace(second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    with get_connection() as conn:
        for item in readings:
            sensor_id = item.get("sensor_id", item.get("pot_id"))
            if sensor_id is None:
                raise ValueError("Each reading must include sensor_id or pot_id")
            item_recorded_at = _as_local(item.get("recorded_at") or default_recorded_at).replace(second=0, microsecond=0)
            rows.append(
                {
                    "sensor_id": int(sensor_id),
                    "recorded_at": sensor_rows.closest_actual_recorded_at(conn, int(sensor_id), item_recorded_at),
                    "soil_moisture_pct": _required_number(item, "soil_moisture_pct"),
                    "air_temperature_c": _optional_number(item.get("air_temperature_c")),
                    "air_humidity_pct": _optional_number(item.get("air_humidity_pct")),
                    "substrate_temperature_c": _optional_number(item.get("substrate_temperature_c")),
                    "source": ACTUAL_SENSOR_SOURCE,
                    "reading_resolution": RAW_RESOLUTION,
                    "sample_count": 1,
                }
            )
        upserted = sensor_rows.upsert_sensor_rows(conn, rows, update_changed_at=True)
        conn.commit()

    slots = sorted({row["recorded_at"].isoformat() for row in rows})
    return {
        "source": source,
        "reading_resolution": RAW_RESOLUTION,
        "reading_interval_minutes": ACTUAL_READING_INTERVAL_MINUTES,
        "received_readings": len(readings),
        "upserted_readings": upserted,
        "stored_slots": slots,
    }



def run_sensor_service() -> None:
    settings = get_settings()
    source = settings.sensor_source
    placement = SensorPlacementService().ensure_default_if_missing()
    print(f"Sensor placement ready: {placement.get('sensor_count', 0)} sensors", flush=True)
    if settings.sensor_seed_history_on_startup:
        summary = ensure_tiered_sensor_readings(source=source, cleanup=settings.sensor_cleanup_enabled)
        print(f"Tiered sensor history ready: {summary}", flush=True)

    # Fill missing scheduled readings that are already due today.
    # Cleanup runs during startup history seeding, or once here when seeding is disabled.
    due = generate_due_sensor_readings(source=source)
    if due:
        print(f"Generated due sensor readings: {due}", flush=True)

    if settings.sensor_cleanup_enabled and not settings.sensor_seed_history_on_startup:
        cleanup = aggregate_and_cleanup_sensor_readings(source=source)
        print(f"Sensor aggregate cleanup completed: {cleanup}", flush=True)

    while True:
        next_run = _next_scheduled_datetime(datetime.now(LOCAL_TZ))
        seconds = max(1, int((next_run - datetime.now(LOCAL_TZ)).total_seconds()))
        print(f"Next sensor reading scheduled at {next_run.isoformat()}", flush=True)
        sleep_time.sleep(seconds)
        result = generate_sensor_readings_at(next_run, source=source)
        print(f"Generated scheduled sensor readings: {result}", flush=True)


def get_sensor_reading_summary(source: str | None = None) -> dict[str, Any]:
    initialize_database()
    params: dict[str, Any] = {}
    where = ""
    if source:
        where = "WHERE source = %(source)s"
        params["source"] = source

    with get_connection(row_factory=dict_row) as conn:
        by_source = conn.execute(
            f"""
            SELECT
                source,
                count(*) AS row_count,
                count(DISTINCT sensor_id) AS sensor_count,
                min(recorded_at) AS first_recorded_at,
                max(recorded_at) AS last_recorded_at
            FROM sensor_readings
            {where}
            GROUP BY source
            ORDER BY source
            """,
            params,
        ).fetchall()
        by_resolution = conn.execute(
            f"""
            SELECT
                source,
                reading_resolution,
                count(*) AS row_count,
                count(DISTINCT sensor_id) AS sensor_count,
                min(recorded_at) AS first_recorded_at,
                max(recorded_at) AS last_recorded_at,
                sum(sample_count)::int AS sample_count
            FROM sensor_readings
            {where}
            GROUP BY source, reading_resolution
            ORDER BY source, reading_resolution
            """,
            params,
        ).fetchall()
        recent = conn.execute(
            f"""
            SELECT
                sensor_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                source,
                reading_resolution,
                sample_count
            FROM sensor_readings
            {where}
            ORDER BY recorded_at DESC, sensor_id
            LIMIT 20
            """,
            params,
        ).fetchall()
    return _json_ready(
        {
            "sources": by_source,
            "resolutions": by_resolution,
            "recent": recent,
            "retention": {
                "raw_hours": RAW_RETENTION_HOURS,
                "hourly_days": HOURLY_RETENTION_DAYS,
                "daily_days": DAILY_RETENTION_DAYS,
                "daily_reading_times": [slot.strftime("%H:%M") for slot in DAILY_READING_TIMES],
                "reading_interval_minutes": _reading_interval_minutes(),
            },
        }
    )


def aggregate_and_cleanup_sensor_readings(
    now: datetime | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    initialize_database()
    now = _as_local(now or datetime.now(LOCAL_TZ))
    period = _tiered_periods(now)
    params: dict[str, Any] = {
        "timezone": LOCAL_TZ.key,
        "raw_start": _db_timestamp(period["raw_start"]),
        "hourly_start": _db_timestamp(period["hourly_start"]),
        "daily_start_date": period["daily_start"].date(),
        "raw_resolution": RAW_RESOLUTION,
        "hourly_resolution": HOURLY_RESOLUTION,
        "daily_resolution": DAILY_RESOLUTION,
        "actual_source": ACTUAL_SENSOR_SOURCE,
        "default_source": DEFAULT_SENSOR_SOURCE,
        "output_source": source or DEFAULT_SENSOR_SOURCE,
        "daily_slot_offsets_minutes": _daily_slot_offsets_minutes(),
    }
    source_filter = ""
    if source:
        params["sources"] = _query_sources(source)
        source_filter = "AND source = ANY(%(sources)s)"

    with get_connection() as conn:
        hourly_upserted = conn.execute(
            f"""
            WITH hourly AS (
                SELECT
                    sensor_id,
                    date_trunc('hour', recorded_at) AS bucket_recorded_at,
                    round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                    round(avg(air_temperature_c), 2) AS air_temperature_c,
                    round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                    round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                    count(*)::int AS sample_count,
                    CASE
                        WHEN bool_or(source = %(actual_source)s) THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source
                FROM sensor_readings
                WHERE reading_resolution = %(raw_resolution)s
                  AND recorded_at < %(raw_start)s
                  AND recorded_at >= %(hourly_start)s
                  {source_filter}
                GROUP BY sensor_id, bucket_recorded_at
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id, bucket_recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, %(hourly_resolution)s,
                sample_count
            FROM hourly
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = NULL
            """,
            params,
        ).rowcount
        daily_from_raw = conn.execute(
            f"""
            WITH daily AS (
                SELECT
                    sensor_id,
                    recorded_at::date AS bucket_date,
                    (EXTRACT(HOUR FROM recorded_at)::int * 60 + EXTRACT(MINUTE FROM recorded_at)::int) AS bucket_slot_offset_minutes,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    sample_count,
                    CASE
                        WHEN source = %(actual_source)s THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source
                FROM sensor_readings
                WHERE reading_resolution = %(raw_resolution)s
                  AND recorded_at < %(hourly_start)s
                  AND recorded_at::date >= %(daily_start_date)s
                  AND (EXTRACT(HOUR FROM recorded_at)::int * 60 + EXTRACT(MINUTE FROM recorded_at)::int)
                        = ANY(%(daily_slot_offsets_minutes)s::int[])
                  {source_filter}
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id,
                bucket_date + make_interval(mins => bucket_slot_offset_minutes),
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                %(daily_resolution)s,
                sample_count
            FROM daily
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = NULL
            """,
            params,
        ).rowcount
        daily_from_hourly = conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    sr.sensor_id,
                    sr.recorded_at::date AS bucket_date,
                    slot.slot_offset_minutes AS bucket_slot_offset_minutes,
                    sr.soil_moisture_pct,
                    sr.air_temperature_c,
                    sr.air_humidity_pct,
                    sr.substrate_temperature_c,
                    sr.sample_count,
                    CASE
                        WHEN sr.source = %(actual_source)s THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source,
                    row_number() OVER (
                        PARTITION BY sr.sensor_id, sr.recorded_at::date, slot.slot_offset_minutes
                        ORDER BY
                            abs(EXTRACT(EPOCH FROM (
                                sr.recorded_at - (sr.recorded_at::date + make_interval(mins => slot.slot_offset_minutes))
                            ))),
                            sr.recorded_at DESC
                    ) AS slot_rank
                FROM sensor_readings sr
                CROSS JOIN unnest(%(daily_slot_offsets_minutes)s::int[]) AS slot(slot_offset_minutes)
                WHERE reading_resolution = %(hourly_resolution)s
                  AND recorded_at < %(hourly_start)s
                  AND recorded_at::date >= %(daily_start_date)s
                  {source_filter}
            ),
            daily AS (
                SELECT *
                FROM ranked
                WHERE slot_rank = 1
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id,
                bucket_date + make_interval(mins => bucket_slot_offset_minutes),
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                %(daily_resolution)s,
                sample_count
            FROM daily
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = NULL
            """,
            params,
        ).rowcount
        raw_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(raw_resolution)s
              AND recorded_at < %(raw_start)s
              {source_filter}
            """,
            params,
        ).rowcount
        hourly_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(hourly_resolution)s
              AND recorded_at < %(hourly_start)s
              {source_filter}
            """,
            params,
        ).rowcount
        daily_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(daily_resolution)s
              AND recorded_at::date < %(daily_start_date)s
              {source_filter}
            """,
            params,
        ).rowcount
        conn.commit()

    return {
        "source": source,
        "raw_start": period["raw_start"].isoformat(),
        "hourly_start": period["hourly_start"].isoformat(),
        "daily_start_date": period["daily_start"].date().isoformat(),
        "hourly_upserted": hourly_upserted,
        "daily_from_raw_upserted": daily_from_raw,
        "daily_from_hourly_upserted": daily_from_hourly,
        "raw_deleted": raw_deleted,
        "hourly_deleted": hourly_deleted,
        "daily_deleted": daily_deleted,
    }



def _tiered_resolution_for_time(recorded_at: datetime, period: dict[str, datetime]) -> str | None:
    if period["raw_start"] <= recorded_at <= period["end_at"]:
        return RAW_RESOLUTION
    if period["hourly_start"] <= recorded_at < period["raw_start"]:
        return HOURLY_RESOLUTION if recorded_at.minute == 0 else None
    if period["daily_start"] <= recorded_at < period["hourly_start"]:
        return DAILY_RESOLUTION if recorded_at.time().replace(second=0, microsecond=0) in DAILY_READING_TIMES else None
    return None


def _sample_count_for_resolution(resolution: str) -> int:
    if resolution == HOURLY_RESOLUTION:
        return 4
    if resolution == DAILY_RESOLUTION:
        return 1
    return 1


def _daily_slot_offsets_minutes() -> list[int]:
    return [slot.hour * 60 + slot.minute for slot in DAILY_READING_TIMES]



def _date_from_env(name: str, default: date) -> date:
    settings = get_settings()
    if name == "SENSOR_HISTORY_START":
        return settings.sensor_history_start
    if name == "SENSOR_HISTORY_END":
        return settings.sensor_history_end or default
    return default


def date_from_env(name: str, default: date) -> date:
    return _date_from_env(name, default)


def _required_number(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if value is None:
        raise ValueError(f"Each reading must include {key}")
    return float(value)


def _optional_number(value) -> float | None:
    return None if value is None else float(value)


def _json_ready(value):
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and maintain simulated pot sensor readings.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser("seed-history")
    seed_parser.add_argument("--start", default=DEFAULT_HISTORY_START.isoformat())
    seed_parser.add_argument("--end", default=_today_local().isoformat())
    seed_parser.add_argument("--source", default=DEFAULT_SENSOR_SOURCE)

    tiered_parser = subparsers.add_parser("seed-tiered")
    tiered_parser.add_argument("--start", default=None)
    tiered_parser.add_argument("--end-at", default=None)
    tiered_parser.add_argument("--source", default=DEFAULT_SENSOR_SOURCE)
    tiered_parser.add_argument("--append", action="store_true")

    once_parser = subparsers.add_parser("run-once")
    once_parser.add_argument("--at", default=datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0).isoformat())
    once_parser.add_argument("--source", default=DEFAULT_SENSOR_SOURCE)

    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("--source", default=None)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--source", default=None)

    subparsers.add_parser("service")

    args = parser.parse_args()
    if args.command == "seed-history":
        result = seed_historical_sensor_readings(
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end),
            source=args.source,
        )
        print(_json_ready(result))
    elif args.command == "seed-tiered":
        result = seed_tiered_sensor_readings(
            start_date=date.fromisoformat(args.start) if args.start else None,
            end_at=datetime.fromisoformat(args.end_at) if args.end_at else None,
            source=args.source,
            replace_existing=not args.append,
        )
        print(_json_ready(result))
    elif args.command == "run-once":
        result = generate_sensor_readings_at(datetime.fromisoformat(args.at), source=args.source)
        print(_json_ready(result))
    elif args.command == "summary":
        print(get_sensor_reading_summary(source=args.source))
    elif args.command == "cleanup":
        print(_json_ready(aggregate_and_cleanup_sensor_readings(source=args.source)))
    elif args.command == "service":
        run_sensor_service()


if __name__ == "__main__":
    main()




