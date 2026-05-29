from __future__ import annotations

import argparse
import random
import time as sleep_time
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from digital_twin.core.config import get_settings
from digital_twin.services.sensor_placements import SensorPlacementService
from digital_twin.db.connection import get_connection
from digital_twin.db.schema import initialize_database


LOCATION_NAME = "Cluj-Napoca"
LOCAL_TZ = ZoneInfo("Europe/Bucharest")
DEFAULT_SENSOR_SOURCE = "simulated_sensor"
ACTUAL_SENSOR_SOURCE = "actual_sensor"
DEFAULT_HISTORY_START = date(2025, 5, 22)
RAW_RESOLUTION = "raw_15min"
ACTUAL_READING_INTERVAL_MINUTES = 15
HOURLY_RESOLUTION = "hourly"
DAILY_RESOLUTION = "daily"
RAW_RETENTION_HOURS = 24
HOURLY_RETENTION_DAYS = 7
DAILY_RETENTION_DAYS = 366


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
    pots = _load_pots()
    weather_rows = _load_weather(start_date, end_date)
    weather_index = 0
    latest_weather: dict[str, Any] | None = None

    states = _initial_sensor_states(pots)
    pending_rows: list[dict[str, Any]] = []
    inserted_or_updated = 0
    total_readings = 0

    current = datetime.combine(start_date, time(0, 0), tzinfo=LOCAL_TZ)
    interval_minutes = _reading_interval_minutes()
    interval_hours = interval_minutes / 60.0
    end_dt = datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=LOCAL_TZ) - timedelta(minutes=interval_minutes)

    with get_connection() as conn:
        while current <= end_dt:
            while weather_index < len(weather_rows) and _local_observed_at(weather_rows[weather_index]) <= current:
                latest_weather = weather_rows[weather_index]
                weather_index += 1
            weather = latest_weather or _fallback_weather(current)
            for pot in pots:
                _apply_hourly_environment(states[pot["id"]], pot, weather, current.date(), hours=interval_hours)

            for pot in pots:
                row = _sensor_row(pot, states[pot["id"]], weather, current, source)
                pending_rows.append(row)
                _apply_virtual_irrigation_if_due(states[pot["id"]], pot, weather, current)
                total_readings += 1

            if len(pending_rows) >= batch_size:
                inserted_or_updated += _upsert_sensor_rows(conn, pending_rows)
                pending_rows.clear()

            current += timedelta(minutes=interval_minutes)

        if pending_rows:
            inserted_or_updated += _upsert_sensor_rows(conn, pending_rows)
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

    pots = _load_pots()
    weather_rows = _load_weather(start_date, end_at.date())
    weather_index = 0
    latest_weather: dict[str, Any] | None = None
    states = _initial_sensor_states(pots)
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
            while weather_index < len(weather_rows) and _local_observed_at(weather_rows[weather_index]) <= current:
                latest_weather = weather_rows[weather_index]
                weather_index += 1
            weather = latest_weather or _fallback_weather(current)

            for pot in pots:
                _apply_hourly_environment(states[pot["id"]], pot, weather, current.date(), hours=0.25)

            resolution = _tiered_resolution_for_time(current, period)
            if resolution:
                sample_count = _sample_count_for_resolution(resolution)
                for pot in pots:
                    row = _sensor_row(pot, states[pot["id"]], weather, current, source)
                    row["reading_resolution"] = resolution
                    row["sample_count"] = sample_count
                    pending_rows.append(row)
                    by_resolution[resolution] += 1

                if len(pending_rows) >= batch_size:
                    inserted_or_updated += _upsert_sensor_rows(conn, pending_rows)
                    pending_rows.clear()

            for pot in pots:
                _apply_virtual_irrigation_if_due(states[pot["id"]], pot, weather, current)

            current += timedelta(minutes=15)

        if pending_rows:
            inserted_or_updated += _upsert_sensor_rows(conn, pending_rows)
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
    pots = _load_pots()
    previous = _load_latest_sensor_states(recorded_at, source)
    weather = _load_latest_weather_at(recorded_at) or _fallback_weather(recorded_at)

    rows = []
    for pot in pots:
        previous_row = previous.get(pot["id"])
        if previous_row:
            state = {
                "moisture": _number(previous_row["soil_moisture_pct"], float(pot["moisture_target_pct"])),
                "last_recorded_at": _as_local(previous_row["recorded_at"]),
            }
            previous_weather = _load_latest_weather_at(state["last_recorded_at"]) or _fallback_weather(state["last_recorded_at"])
            _apply_virtual_irrigation_if_due(state, pot, previous_weather, state["last_recorded_at"])
        else:
            state = _initial_state_for_pot(pot)

        interval_minutes = _reading_interval_minutes()
        intervals_elapsed = max(1, min(24 * 60 // interval_minutes, int((recorded_at - state["last_recorded_at"]).total_seconds() // (interval_minutes * 60))))
        for _ in range(intervals_elapsed):
            _apply_hourly_environment(state, pot, weather, recorded_at.date(), hours=interval_minutes / 60.0)
        rows.append(_sensor_row(pot, state, weather, recorded_at, source))

    with get_connection() as conn:
        upserted = _upsert_sensor_rows(conn, rows, update_changed_at=source == ACTUAL_SENSOR_SOURCE)
        conn.commit()

    return {
        "source": source,
        "recorded_at": recorded_at.isoformat(),
        "pot_count": len(pots),
        "upserted_readings": upserted,
    }


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
        return [] if _reading_exists(latest_due, source) else [generate_sensor_readings_at(latest_due, source=source)]

    results = []
    for scheduled_at in due_times:
        if not _reading_exists(scheduled_at, source):
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
                    "recorded_at": _closest_actual_recorded_at(conn, int(sensor_id), item_recorded_at),
                    "soil_moisture_pct": _required_number(item, "soil_moisture_pct"),
                    "air_temperature_c": _optional_number(item.get("air_temperature_c")),
                    "air_humidity_pct": _optional_number(item.get("air_humidity_pct")),
                    "substrate_temperature_c": _optional_number(item.get("substrate_temperature_c")),
                    "source": ACTUAL_SENSOR_SOURCE,
                    "reading_resolution": RAW_RESOLUTION,
                    "sample_count": 1,
                }
            )
        upserted = _upsert_sensor_rows(conn, rows, update_changed_at=True)
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
                  AND recorded_at < %(hourly_start)s
                  AND recorded_at::date >= %(daily_start_date)s
                  {source_filter}
                GROUP BY sensor_id, bucket_date
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id,
                bucket_date + time '12:00',
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
            WITH daily AS (
                SELECT
                    sensor_id,
                    recorded_at::date AS bucket_date,
                    round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                    round(avg(air_temperature_c), 2) AS air_temperature_c,
                    round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                    round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                    sum(sample_count)::int AS sample_count,
                    CASE
                        WHEN bool_or(source = %(actual_source)s) THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source
                FROM sensor_readings
                WHERE reading_resolution = %(hourly_resolution)s
                  AND recorded_at < %(hourly_start)s
                  AND recorded_at::date >= %(daily_start_date)s
                  {source_filter}
                GROUP BY sensor_id, bucket_date
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id,
                bucket_date + time '12:00',
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
            """,
            {
                "sources": _query_sources(source),
                "sensor_ids": sensor_ids,
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
                "start_date": start_date,
                "end_date": end_date,
                "sensor_count": len(sensor_ids),
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
    daily_slots = max(0, (period["hourly_start"].date() - period["daily_start"].date()).days)
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
                        EXTRACT(HOUR FROM recorded_at)::int AS local_hour,
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
                    GROUP BY sensor_id, local_date, local_hour
                    UNION ALL
                    SELECT
                        sensor_id,
                        recorded_at::date AS local_date,
                        EXTRACT(HOUR FROM recorded_at)::int AS local_hour,
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
                        EXTRACT(HOUR FROM recorded_at)::int AS local_hour,
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
                SELECT DISTINCT ON (sensor_id, local_date, local_hour)
                    sensor_id,
                    local_date,
                    local_hour,
                    recorded_at,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    source,
                    resolution,
                    sample_count
                FROM tiered
                ORDER BY sensor_id, local_date, local_hour, tier_priority, recorded_at DESC
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
        (row["local_date"], int(row["local_hour"]), row["sensor_id"]): row
        for row in rows
    }
    lookup = {}
    for experiment_date, sensor_date in mapped_dates.items():
        for row in rows:
            if row["local_date"] == sensor_date:
                lookup[(experiment_date, int(row["local_hour"]), row["sensor_id"])] = row

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


def _load_pots() -> list[dict[str, Any]]:
    sensor_pot_ids = _sensor_equipped_pot_ids()
    if not sensor_pot_ids:
        return []
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT
                p.*,
                pt.water_need_level,
                pt.heat_sensitive,
                pt.allows_second_watering,
                ps.volume_l,
                ps.evaporation_factor,
                ps.retention_factor
            FROM pots p
            JOIN plant_types pt ON pt.code = p.plant_type_code
            JOIN pot_size_profiles ps
              ON ps.code = CASE
                    WHEN p.size_class = 'small' THEN 'small_' || p.small_subtype
                    ELSE p.size_class
                 END
            WHERE p.active = true
              AND p.id = ANY(%(sensor_pot_ids)s)
            ORDER BY p.id
            """,
            {"sensor_pot_ids": sensor_pot_ids},
        ).fetchall()


def _load_weather(start_date: date, end_date: date) -> list[dict[str, Any]]:
    start_ts = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_ts = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            WITH ranked_weather AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY observed_local_at
                        ORDER BY
                            CASE
                                WHEN source = 'open-meteo-archive' THEN 0
                                WHEN source = 'open-meteo-forecast' THEN 1
                                ELSE 2
                            END,
                            id DESC
                    ) AS source_rank
                FROM weather_hourly
                WHERE location_name = %(location)s
                  AND observed_local_at >= %(start_ts)s
                  AND observed_local_at < %(end_ts)s
            )
            SELECT *
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_local_at
            """,
            {"location": LOCATION_NAME, "start_ts": start_ts.replace(tzinfo=None), "end_ts": end_ts.replace(tzinfo=None)},
        ).fetchall()


def _load_latest_weather_at(recorded_at: datetime) -> dict[str, Any] | None:
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            WITH ranked_weather AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY observed_local_at
                        ORDER BY
                            CASE
                                WHEN source = 'open-meteo-archive' THEN 0
                                WHEN source = 'open-meteo-forecast' THEN 1
                                ELSE 2
                            END,
                            id DESC
                    ) AS source_rank
                FROM weather_hourly
                WHERE location_name = %(location)s
                  AND observed_local_at <= %(recorded_at)s
            )
            SELECT *
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_local_at DESC
            LIMIT 1
            """,
            {"location": LOCATION_NAME, "recorded_at": _db_timestamp(recorded_at)},
        ).fetchone()


def _load_latest_sensor_states(recorded_at: datetime, source: str) -> dict[int, dict[str, Any]]:
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (sensor_id)
                sensor_id,
                recorded_at,
                soil_moisture_pct
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at < %(recorded_at)s
            ORDER BY sensor_id, recorded_at DESC
            """,
            {
                "sources": _query_sources(source),
                "recorded_at": _db_timestamp(recorded_at),
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
            },
        ).fetchall()
    return {row["sensor_id"]: row for row in rows}


def _closest_actual_recorded_at(conn, sensor_id: int, recorded_at: datetime) -> datetime:
    aligned_recorded_at = _db_timestamp(_align_to_interval(recorded_at, ACTUAL_READING_INTERVAL_MINUTES))
    requested_at = _db_timestamp(recorded_at)
    row = conn.execute(
        """
        SELECT recorded_at
        FROM sensor_readings
        WHERE sensor_id = %(sensor_id)s
          AND reading_resolution = %(raw_resolution)s
          AND recorded_at BETWEEN %(start_at)s AND %(end_at)s
        ORDER BY abs(extract(epoch FROM (recorded_at - %(requested_at)s::timestamp)))
        LIMIT 1
        """,
        {
            "sensor_id": sensor_id,
            "raw_resolution": RAW_RESOLUTION,
            "requested_at": requested_at,
            "start_at": requested_at - timedelta(minutes=ACTUAL_READING_INTERVAL_MINUTES / 2),
            "end_at": requested_at + timedelta(minutes=ACTUAL_READING_INTERVAL_MINUTES / 2),
        },
    ).fetchone()
    return row[0] if row else aligned_recorded_at


def _upsert_sensor_rows(conn, rows: list[dict[str, Any]], update_changed_at: bool = False) -> int:
    changed_at_value = "now() AT TIME ZONE 'Europe/Bucharest'" if update_changed_at else "NULL"
    conflict_filter = "" if update_changed_at else f"WHERE sensor_readings.source <> '{ACTUAL_SENSOR_SOURCE}'"
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO sensor_readings (
                sensor_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                reading_resolution,
                sample_count,
                changed_at
            )
            VALUES (
                %(sensor_id)s,
                %(recorded_at)s,
                %(soil_moisture_pct)s,
                %(air_temperature_c)s,
                %(air_humidity_pct)s,
                %(substrate_temperature_c)s,
                %(source)s,
                %(reading_resolution)s,
                %(sample_count)s,
                {changed_at_value}
            )
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = {changed_at_value}
            {conflict_filter}
            """,
            rows,
        )
        rowcount = cur.rowcount
    return rowcount if rowcount else len(rows)


def _tiered_periods(end_at: datetime) -> dict[str, datetime]:
    end_at = _align_to_reading_interval(_as_local(end_at))
    raw_start = datetime.combine(end_at.date(), time(0, 0), tzinfo=LOCAL_TZ)
    hourly_start = raw_start - timedelta(days=HOURLY_RETENTION_DAYS)
    daily_start = raw_start - timedelta(days=DAILY_RETENTION_DAYS)
    return {
        "daily_start": daily_start,
        "hourly_start": hourly_start,
        "raw_start": raw_start,
        "end_at": end_at,
    }


def _tiered_resolution_for_time(recorded_at: datetime, period: dict[str, datetime]) -> str | None:
    if period["raw_start"] <= recorded_at <= period["end_at"]:
        return RAW_RESOLUTION
    if period["hourly_start"] <= recorded_at < period["raw_start"]:
        return HOURLY_RESOLUTION if recorded_at.minute == 0 else None
    if period["daily_start"] <= recorded_at < period["hourly_start"]:
        return DAILY_RESOLUTION if recorded_at.hour == 12 and recorded_at.minute == 0 else None
    return None


def _slot_count(start_at: datetime, end_at: datetime, minutes: int) -> int:
    if end_at < start_at:
        return 0
    return int((end_at - start_at).total_seconds() // (minutes * 60)) + 1


def _sample_count_for_resolution(resolution: str) -> int:
    if resolution == HOURLY_RESOLUTION:
        return 4
    if resolution == DAILY_RESOLUTION:
        return 96
    return 1


def _initial_sensor_states(pots: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {pot["id"]: _initial_state_for_pot(pot) for pot in pots}


def _initial_state_for_pot(pot: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(2026 + int(pot["id"]))
    target = float(pot["moisture_target_pct"])
    return {
        "moisture": _clamp(target + rng.uniform(-6.0, 4.0), 5.0, 95.0),
        "last_recorded_at": datetime.combine(DEFAULT_HISTORY_START, time(0, 0), tzinfo=LOCAL_TZ),
    }


def _apply_hourly_environment(
    state: dict[str, Any],
    pot: dict[str, Any],
    weather: dict[str, Any],
    local_day: date,
    hours: float = 1.0,
) -> None:
    outdoor = _is_outdoor(pot, local_day)
    if outdoor:
        evap_mm = _number(weather.get("evapotranspiration_mm"), None)
        if evap_mm is None:
            temp = _number(weather.get("temperature_c"), 20.0)
            humidity = _number(weather.get("relative_humidity_pct"), 60.0)
            wind = _number(weather.get("wind_speed_kmh"), 5.0)
            evap_mm = max(0.01, 0.025 + (temp / 38.0) * ((100.0 - humidity) / 100.0) * (1.0 + wind / 45.0))

        loss = evap_mm * float(pot["evaporation_factor"]) * _sun_factor(pot) * _wind_factor(pot) * hours
        if pot["plant_type_code"] in {"vegetables", "herbs"}:
            loss *= 1.12
        elif pot["plant_type_code"] == "succulents":
            loss *= 0.48

        rain_gain = min(8.0, _number(weather.get("precipitation_mm"), 0.0) * 0.85 * hours)
        state["moisture"] += rain_gain - loss
    else:
        state["moisture"] -= (0.018 if pot["plant_type_code"] != "succulents" else 0.006) * hours

    state["moisture"] = _clamp(state["moisture"], 0.0, 100.0)


def _apply_virtual_irrigation_if_due(state: dict[str, Any], pot: dict[str, Any], weather: dict[str, Any], recorded_at: datetime) -> None:
    hour = recorded_at.hour
    if hour not in {7, 18}:
        return

    temp = _number(weather.get("temperature_c"), 20.0)
    precipitation = _number(weather.get("precipitation_mm"), 0.0)
    threshold = float(pot["moisture_min_pct"])
    target = float(pot["moisture_target_pct"])

    if recorded_at.month in {12, 1, 2}:
        threshold = 10.0
        target = float(pot["winter_moisture_target_pct"])
        if temp <= 10.0:
            return

    if temp <= 0.0:
        return
    if precipitation >= 2.0 and state["moisture"] > threshold * 0.85:
        return
    if hour == 18 and pot["plant_type_code"] not in {"vegetables", "herbs"} and pot["size_class"] != "small":
        return
    if state["moisture"] >= threshold:
        return

    volume_l = float(pot["volume_l"])
    retention = max(float(pot["retention_factor"]), 0.1)
    flow_rate = max(float(pot["drip_flow_ml_min"]), 1.0)
    need_pct = max(0.0, target - state["moisture"])
    planned_volume_ml = need_pct * volume_l * 10.0 / retention
    max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[pot["size_class"]]
    planned_volume_ml = min(planned_volume_ml, flow_rate * max_minutes)
    moisture_gain = planned_volume_ml * retention / max(volume_l * 10.0, 1.0)
    state["moisture"] = _clamp(state["moisture"] + moisture_gain, 0.0, 100.0)


def _sensor_row(
    pot: dict[str, Any],
    state: dict[str, Any],
    weather: dict[str, Any],
    recorded_at: datetime,
    source: str,
) -> dict[str, Any]:
    rng = random.Random(f"{pot['id']}|{recorded_at.isoformat()}|{source}")
    air_temperature = _microclimate_temperature(pot, weather, recorded_at)
    air_humidity = _clamp(_number(weather.get("relative_humidity_pct"), 60.0) + rng.uniform(-4.0, 4.0), 20.0, 100.0)
    substrate_temperature = air_temperature + _substrate_delta(pot, recorded_at)
    moisture = _clamp(state["moisture"] + rng.uniform(-1.2, 1.2), 0.0, 100.0)
    return {
        "sensor_id": pot["id"],
        "recorded_at": _db_timestamp(recorded_at),
        "soil_moisture_pct": round(moisture, 2),
        "air_temperature_c": round(air_temperature, 2),
        "air_humidity_pct": round(air_humidity, 2),
        "substrate_temperature_c": round(substrate_temperature, 2),
        "source": source,
        "reading_resolution": RAW_RESOLUTION,
        "sample_count": 1,
    }


def _microclimate_temperature(pot: dict[str, Any], weather: dict[str, Any], recorded_at: datetime) -> float:
    base = _number(weather.get("temperature_c"), 20.0)
    hour = recorded_at.hour
    sun_delta = {
        "shade": -0.4,
        "partial": 0.5,
        "full": 1.4,
        "reflected_heat": 2.6,
    }[pot["sun_exposure"]]
    if hour < 8 or hour > 19:
        sun_delta *= 0.2
    elif 11 <= hour <= 16:
        sun_delta *= 1.25
    wind_delta = -0.3 if pot["wind_exposure"] == "gusty" else 0.0
    indoor_delta = 1.8 if not _is_outdoor(pot, recorded_at.date()) else 0.0
    return base + sun_delta + wind_delta + indoor_delta


def _substrate_delta(pot: dict[str, Any], recorded_at: datetime) -> float:
    material_delta = {
        "terracotta": 0.6,
        "plastic": 1.1,
        "ceramic": 0.4,
        "fabric": -0.2,
    }.get(pot["container_material"], 0.0)
    if 11 <= recorded_at.hour <= 16 and pot["sun_exposure"] in {"full", "reflected_heat"}:
        material_delta += 1.5
    return material_delta


def _fallback_weather(observed_at: datetime) -> dict[str, Any]:
    month = observed_at.month
    if month in {12, 1, 2}:
        temp = 3.0
        humidity = 78.0
    elif month in {6, 7, 8}:
        temp = 25.0
        humidity = 55.0
    elif month in {3, 4, 5}:
        temp = 16.0
        humidity = 65.0
    else:
        temp = 13.0
        humidity = 70.0
    return {
        "observed_at": observed_at,
        "observed_local_at": observed_at.replace(tzinfo=None) if observed_at.tzinfo else observed_at,
        "observed_date": observed_at.date(),
        "observed_hour": observed_at.hour,
        "temperature_c": temp,
        "relative_humidity_pct": humidity,
        "precipitation_mm": 0.0,
        "wind_speed_kmh": 8.0,
        "wind_gust_kmh": 14.0,
        "evapotranspiration_mm": None,
        "source": "fallback",
    }


def _reading_exists(recorded_at: datetime, source: str) -> bool:
    sensor_ids = _sensor_equipped_pot_ids()
    if not sensor_ids:
        return False
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT sensor_id)
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND reading_resolution = %(raw_resolution)s
              AND recorded_at = %(recorded_at)s
              AND sensor_id = ANY(%(sensor_ids)s)
            """,
            {
                "sources": _query_sources(source),
                "raw_resolution": RAW_RESOLUTION,
                "recorded_at": _db_timestamp(recorded_at),
                "sensor_ids": sensor_ids,
            },
        ).fetchone()
    return bool(row and row[0] >= len(sensor_ids))


def _sensor_equipped_pot_ids(candidate_pot_ids: list[int] | None = None) -> list[int]:
    return SensorPlacementService().selected_pot_ids(candidate_pot_ids)


def _query_sources(source: str | None) -> list[str]:
    if source == DEFAULT_SENSOR_SOURCE:
        return [DEFAULT_SENSOR_SOURCE, ACTUAL_SENSOR_SOURCE]
    if source:
        return [source]
    return [DEFAULT_SENSOR_SOURCE, ACTUAL_SENSOR_SOURCE]


def _scheduled_datetimes(day: date) -> list[datetime]:
    interval_minutes = _reading_interval_minutes()
    current = datetime.combine(day, time(0, 0), tzinfo=LOCAL_TZ)
    end = current + timedelta(days=1)
    scheduled = []
    while current < end:
        scheduled.append(current)
        current += timedelta(minutes=interval_minutes)
    return scheduled


def _next_scheduled_datetime(now: datetime) -> datetime:
    now = _as_local(now)
    for candidate in _scheduled_datetimes(now.date()):
        if candidate > now:
            return candidate
    return _scheduled_datetimes(now.date() + timedelta(days=1))[0]


def next_scheduled_sensor_datetime(now: datetime) -> datetime:
    return _next_scheduled_datetime(now)


def _reading_interval_minutes() -> int:
    return max(1, min(24 * 60, get_settings().sensor_reading_interval_minutes))


def _align_to_reading_interval(value: datetime) -> datetime:
    return _align_to_interval(value, _reading_interval_minutes())


def _align_to_interval(value: datetime, interval: int) -> datetime:
    value = _as_local(value).replace(second=0, microsecond=0)
    minutes_since_midnight = value.hour * 60 + value.minute
    aligned_minutes = (minutes_since_midnight // interval) * interval
    return datetime.combine(value.date(), time(0, 0), tzinfo=LOCAL_TZ) + timedelta(minutes=aligned_minutes)


def _date_from_env(name: str, default: date) -> date:
    settings = get_settings()
    if name == "SENSOR_HISTORY_START":
        return settings.sensor_history_start
    if name == "SENSOR_HISTORY_END":
        return settings.sensor_history_end or default
    return default


def date_from_env(name: str, default: date) -> date:
    return _date_from_env(name, default)


def _today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def _as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def _db_timestamp(value: datetime) -> datetime:
    return _as_local(value).replace(tzinfo=None)


def _local_observed_at(weather: dict[str, Any]) -> datetime:
    observed_local_at = weather.get("observed_local_at")
    if observed_local_at is not None:
        if observed_local_at.tzinfo is None:
            return observed_local_at.replace(tzinfo=LOCAL_TZ)
        return observed_local_at.astimezone(LOCAL_TZ)
    observed_at = weather["observed_at"]
    if observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=LOCAL_TZ)
    return observed_at.astimezone(LOCAL_TZ)


def _same_month_day(year: int, source_date: date) -> date:
    try:
        return date(year, source_date.month, source_date.day)
    except ValueError:
        return date(year, 2, 28)


def _is_outdoor(pot: dict[str, Any], day: date) -> bool:
    if day.month in {12, 1, 2}:
        return pot["winter_location"] == "outdoor"
    return pot["default_location"] == "outdoor"


def _sun_factor(pot: dict[str, Any]) -> float:
    return {
        "shade": 0.75,
        "partial": 1.0,
        "full": 1.24,
        "reflected_heat": 1.42,
    }[pot["sun_exposure"]]


def _wind_factor(pot: dict[str, Any]) -> float:
    return {
        "sheltered": 0.86,
        "moderate": 1.0,
        "gusty": 1.22,
    }[pot["wind_exposure"]]


def _number(value, default: float | None = 0.0) -> float | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _required_number(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if value is None:
        raise ValueError(f"Each reading must include {key}")
    return float(value)


def _optional_number(value) -> float | None:
    return None if value is None else float(value)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


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
