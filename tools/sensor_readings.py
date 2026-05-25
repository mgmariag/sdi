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
from database import get_connection, initialize_database


LOCATION_NAME = "Cluj-Napoca"
LOCAL_TZ = ZoneInfo("Europe/Bucharest")
DEFAULT_SENSOR_SOURCE = "simulated_sensor"
DEFAULT_HISTORY_START = date(2025, 5, 22)
RAW_RESOLUTION = "raw_15min"
HOURLY_RESOLUTION = "hourly"
DAILY_RESOLUTION = "daily"
RAW_RETENTION_HOURS = 24
HOURLY_RETENTION_DAYS = 7
DAILY_RETENTION_DAYS = 30


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


def generate_sensor_readings_at(
    recorded_at: datetime,
    source: str = DEFAULT_SENSOR_SOURCE,
) -> dict[str, Any]:
    """Generate one scheduled reading for every selected sensor location."""
    initialize_database()
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
        upserted = _upsert_sensor_rows(conn, rows)
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
    results = []
    for scheduled_at in due_times:
        if not _reading_exists(scheduled_at, source):
            results.append(generate_sensor_readings_at(scheduled_at, source=source))
    return results


def run_sensor_service() -> None:
    settings = get_settings()
    source = settings.sensor_source
    if settings.sensor_seed_history_on_startup:
        placement = SensorPlacementService().ensure_default_if_missing()
        sensor_pot_ids = [int(item["pot_id"]) for item in placement.get("items", [])]
        if placement.get("changed") or get_sensor_availability(source=source, pot_ids=sensor_pot_ids) is None:
            start_date = settings.sensor_history_start
            end_date = settings.sensor_history_end or _today_local()
            summary = seed_historical_sensor_readings(start_date=start_date, end_date=end_date, source=source)
            print(f"Historical sensor seed completed: {summary}", flush=True)
        else:
            print("Existing sensor readings found; startup seed skipped", flush=True)

    due = generate_due_sensor_readings(source=source)
    if due:
        print(f"Generated due sensor readings: {due}", flush=True)

    if settings.sensor_cleanup_enabled:
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
                count(DISTINCT pot_id) AS pot_count,
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
                count(DISTINCT pot_id) AS pot_count,
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
                pot_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                source,
                reading_resolution,
                sample_count,
                weather_observed_at,
                weather_source
            FROM sensor_readings
            {where}
            ORDER BY recorded_at DESC, pot_id
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
    raw_cutoff = now - timedelta(hours=RAW_RETENTION_HOURS)
    hourly_cutoff = now - timedelta(days=HOURLY_RETENTION_DAYS)
    daily_cutoff_date = (now - timedelta(days=DAILY_RETENTION_DAYS)).date()
    params: dict[str, Any] = {
        "timezone": LOCAL_TZ.key,
        "raw_cutoff": raw_cutoff,
        "hourly_cutoff": hourly_cutoff,
        "daily_cutoff_date": daily_cutoff_date,
        "raw_resolution": RAW_RESOLUTION,
        "hourly_resolution": HOURLY_RESOLUTION,
        "daily_resolution": DAILY_RESOLUTION,
    }
    source_filter = ""
    source_filter_update = ""
    if source:
        params["source"] = source
        source_filter = "AND source = %(source)s"
        source_filter_update = "AND sr.source = %(source)s"

    with get_connection() as conn:
        weather_backfilled = conn.execute(
            f"""
            UPDATE sensor_readings sr
            SET
                weather_observed_at = (
                    SELECT wh.observed_at
                    FROM weather_hourly wh
                    WHERE wh.location_name = %(location)s
                      AND wh.observed_at <= sr.recorded_at
                    ORDER BY
                        wh.observed_at DESC,
                        CASE
                            WHEN wh.source = 'open-meteo-archive' THEN 0
                            WHEN wh.source = 'open-meteo-forecast' THEN 1
                            ELSE 2
                        END,
                        wh.id DESC
                    LIMIT 1
                ),
                weather_source = (
                    SELECT wh.source
                    FROM weather_hourly wh
                    WHERE wh.location_name = %(location)s
                      AND wh.observed_at <= sr.recorded_at
                    ORDER BY
                        wh.observed_at DESC,
                        CASE
                            WHEN wh.source = 'open-meteo-archive' THEN 0
                            WHEN wh.source = 'open-meteo-forecast' THEN 1
                            ELSE 2
                        END,
                        wh.id DESC
                    LIMIT 1
                ),
                changed_at = now()
            WHERE sr.reading_resolution = %(raw_resolution)s
              AND sr.weather_observed_at IS NULL
              {source_filter_update}
            """,
            {**params, "location": LOCATION_NAME},
        ).rowcount
        hourly_upserted = conn.execute(
            f"""
            WITH hourly AS (
                SELECT
                    pot_id,
                    date_trunc('hour', recorded_at AT TIME ZONE %(timezone)s) AT TIME ZONE %(timezone)s AS bucket_recorded_at,
                    round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                    round(avg(air_temperature_c), 2) AS air_temperature_c,
                    round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                    round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                    count(*)::int AS sample_count,
                    source,
                    max(weather_observed_at) AS weather_observed_at,
                    max(weather_source) AS weather_source
                FROM sensor_readings
                WHERE reading_resolution = %(raw_resolution)s
                  AND recorded_at < %(raw_cutoff)s
                  AND recorded_at >= %(hourly_cutoff)s
                  {source_filter}
                GROUP BY pot_id, bucket_recorded_at, source
            )
            INSERT INTO sensor_readings (
                pot_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count, weather_observed_at, weather_source
            )
            SELECT
                pot_id, bucket_recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, %(hourly_resolution)s,
                sample_count, weather_observed_at, weather_source
            FROM hourly
            ON CONFLICT (pot_id, recorded_at, source, reading_resolution) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                sample_count = EXCLUDED.sample_count,
                weather_observed_at = EXCLUDED.weather_observed_at,
                weather_source = EXCLUDED.weather_source,
                changed_at = now()
            """,
            params,
        ).rowcount
        daily_from_raw = conn.execute(
            f"""
            WITH daily AS (
                SELECT
                    pot_id,
                    (recorded_at AT TIME ZONE %(timezone)s)::date AS bucket_date,
                    round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                    round(avg(air_temperature_c), 2) AS air_temperature_c,
                    round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                    round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                    count(*)::int AS sample_count,
                    source,
                    max(weather_observed_at) AS weather_observed_at,
                    max(weather_source) AS weather_source
                FROM sensor_readings
                WHERE reading_resolution = %(raw_resolution)s
                  AND recorded_at < %(hourly_cutoff)s
                  AND (recorded_at AT TIME ZONE %(timezone)s)::date >= %(daily_cutoff_date)s
                  {source_filter}
                GROUP BY pot_id, bucket_date, source
            )
            INSERT INTO sensor_readings (
                pot_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count, weather_observed_at, weather_source
            )
            SELECT
                pot_id,
                (bucket_date + time '12:00') AT TIME ZONE %(timezone)s,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                %(daily_resolution)s,
                sample_count,
                weather_observed_at,
                weather_source
            FROM daily
            ON CONFLICT (pot_id, recorded_at, source, reading_resolution) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                sample_count = EXCLUDED.sample_count,
                weather_observed_at = EXCLUDED.weather_observed_at,
                weather_source = EXCLUDED.weather_source,
                changed_at = now()
            """,
            params,
        ).rowcount
        daily_from_hourly = conn.execute(
            f"""
            WITH daily AS (
                SELECT
                    pot_id,
                    (recorded_at AT TIME ZONE %(timezone)s)::date AS bucket_date,
                    round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                    round(avg(air_temperature_c), 2) AS air_temperature_c,
                    round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                    round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                    sum(sample_count)::int AS sample_count,
                    source,
                    max(weather_observed_at) AS weather_observed_at,
                    max(weather_source) AS weather_source
                FROM sensor_readings
                WHERE reading_resolution = %(hourly_resolution)s
                  AND recorded_at < %(hourly_cutoff)s
                  AND (recorded_at AT TIME ZONE %(timezone)s)::date >= %(daily_cutoff_date)s
                  {source_filter}
                GROUP BY pot_id, bucket_date, source
            )
            INSERT INTO sensor_readings (
                pot_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count, weather_observed_at, weather_source
            )
            SELECT
                pot_id,
                (bucket_date + time '12:00') AT TIME ZONE %(timezone)s,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                %(daily_resolution)s,
                sample_count,
                weather_observed_at,
                weather_source
            FROM daily
            ON CONFLICT (pot_id, recorded_at, source, reading_resolution) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                sample_count = EXCLUDED.sample_count,
                weather_observed_at = EXCLUDED.weather_observed_at,
                weather_source = EXCLUDED.weather_source,
                changed_at = now()
            """,
            params,
        ).rowcount
        raw_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(raw_resolution)s
              AND recorded_at < %(raw_cutoff)s
              {source_filter}
            """,
            params,
        ).rowcount
        hourly_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(hourly_resolution)s
              AND recorded_at < %(hourly_cutoff)s
              {source_filter}
            """,
            params,
        ).rowcount
        daily_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(daily_resolution)s
              AND (recorded_at AT TIME ZONE %(timezone)s)::date < %(daily_cutoff_date)s
              {source_filter}
            """,
            params,
        ).rowcount
        conn.commit()

    return {
        "source": source,
        "raw_cutoff": raw_cutoff.isoformat(),
        "hourly_cutoff": hourly_cutoff.isoformat(),
        "daily_cutoff_date": daily_cutoff_date.isoformat(),
        "hourly_upserted": hourly_upserted,
        "daily_from_raw_upserted": daily_from_raw,
        "daily_from_hourly_upserted": daily_from_hourly,
        "weather_backfilled": weather_backfilled,
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


def get_sensor_availability(
    source: str = DEFAULT_SENSOR_SOURCE,
    pot_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    initialize_database()
    now = datetime.now(LOCAL_TZ)
    pot_filter = ""
    params: dict[str, Any] = {
        "source": source,
        "timezone": LOCAL_TZ.key,
        "now": now,
        "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
    }
    if pot_ids:
        pot_filter = "AND pot_id = ANY(%(pot_ids)s)"
        params["pot_ids"] = pot_ids
    with get_connection(row_factory=dict_row) as conn:
        row = conn.execute(
            f"""
            SELECT
                count(*) AS row_count,
                count(DISTINCT pot_id) AS pot_count,
                min((recorded_at AT TIME ZONE %(timezone)s)::date) AS first_date,
                max((recorded_at AT TIME ZONE %(timezone)s)::date) AS last_date
            FROM sensor_readings
            WHERE source = %(source)s
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at <= %(now)s
              {pot_filter}
            """,
            params,
        ).fetchone()
    if not row or row["row_count"] == 0:
        return None
    return row


def load_sensor_readings_for_experiment(
    start_date: date,
    end_date: date,
    pot_ids: list[int],
    source: str = DEFAULT_SENSOR_SOURCE,
) -> dict[str, Any]:
    sensor_pot_ids = _sensor_equipped_pot_ids(pot_ids)
    if not sensor_pot_ids:
        return {
            "available": False,
            "source": source,
            "lookup": {},
            "mapped_dates": {},
            "future_dates": [],
            "latest_states": {},
            "row_count": 0,
            "sensor_pot_ids": [],
        }

    availability = get_sensor_availability(source, sensor_pot_ids)
    if not availability:
        return {
            "available": False,
            "source": source,
            "lookup": {},
            "mapped_dates": {},
            "future_dates": [],
            "latest_states": {},
            "row_count": 0,
            "sensor_pot_ids": sensor_pot_ids,
        }

    first_sensor_date = availability["first_date"]
    last_sensor_date = availability["last_date"]
    today = _today_local()
    mapped_dates: dict[date, date] = {}
    future_dates: list[date] = []
    current = start_date
    while current <= end_date:
        if current > today:
            future_dates.append(current)
        else:
            mapped_dates[current] = map_experiment_date_to_sensor_date(current, first_sensor_date, last_sensor_date)
        current += timedelta(days=1)

    sensor_dates = sorted(set(mapped_dates.values()))
    with get_connection(row_factory=dict_row) as conn:
        rows = []
        if sensor_dates:
            now = datetime.now(LOCAL_TZ)
            raw_cutoff = now - timedelta(hours=RAW_RETENTION_HOURS)
            hourly_cutoff = now - timedelta(days=HOURLY_RETENTION_DAYS)
            daily_cutoff_date = (now - timedelta(days=DAILY_RETENTION_DAYS)).date()
            rows = conn.execute(
                """
                WITH tiered AS (
                    SELECT
                        pot_id,
                        (recorded_at AT TIME ZONE %(timezone)s)::date AS local_date,
                        EXTRACT(HOUR FROM recorded_at AT TIME ZONE %(timezone)s)::int AS local_hour,
                        max(recorded_at) AS recorded_at,
                        round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                        round(avg(air_temperature_c), 2) AS air_temperature_c,
                        round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                        round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                        source,
                        %(raw_resolution)s AS resolution,
                        sum(sample_count)::int AS sample_count,
                        1 AS tier_priority
                    FROM sensor_readings
                    WHERE source = %(source)s
                      AND pot_id = ANY(%(pot_ids)s)
                      AND reading_resolution = %(raw_resolution)s
                      AND recorded_at >= %(raw_cutoff)s
                      AND (recorded_at AT TIME ZONE %(timezone)s)::date = ANY(%(sensor_dates)s)
                      AND recorded_at <= %(now)s
                    GROUP BY pot_id, local_date, local_hour, source
                    UNION ALL
                    SELECT
                        pot_id,
                        (recorded_at AT TIME ZONE %(timezone)s)::date AS local_date,
                        EXTRACT(HOUR FROM recorded_at AT TIME ZONE %(timezone)s)::int AS local_hour,
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
                    WHERE source = %(source)s
                      AND pot_id = ANY(%(pot_ids)s)
                      AND reading_resolution = %(hourly_resolution)s
                      AND recorded_at >= %(hourly_cutoff)s
                      AND recorded_at < %(raw_cutoff)s
                      AND (recorded_at AT TIME ZONE %(timezone)s)::date = ANY(%(sensor_dates)s)
                    UNION ALL
                    SELECT
                        pot_id,
                        (recorded_at AT TIME ZONE %(timezone)s)::date AS local_date,
                        EXTRACT(HOUR FROM recorded_at AT TIME ZONE %(timezone)s)::int AS local_hour,
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
                    WHERE source = %(source)s
                      AND pot_id = ANY(%(pot_ids)s)
                      AND reading_resolution = %(daily_resolution)s
                      AND (recorded_at AT TIME ZONE %(timezone)s)::date >= %(daily_cutoff_date)s
                      AND (recorded_at AT TIME ZONE %(timezone)s)::date < (%(hourly_cutoff)s AT TIME ZONE %(timezone)s)::date
                      AND (recorded_at AT TIME ZONE %(timezone)s)::date = ANY(%(sensor_dates)s)
                )
                SELECT DISTINCT ON (pot_id, local_date, local_hour)
                    pot_id,
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
                ORDER BY pot_id, local_date, local_hour, tier_priority, recorded_at DESC
                """,
                {
                    "timezone": LOCAL_TZ.key,
                    "source": source,
                    "pot_ids": sensor_pot_ids,
                    "raw_resolution": RAW_RESOLUTION,
                    "hourly_resolution": HOURLY_RESOLUTION,
                    "daily_resolution": DAILY_RESOLUTION,
                    "raw_cutoff": raw_cutoff,
                    "hourly_cutoff": hourly_cutoff,
                    "daily_cutoff_date": daily_cutoff_date,
                    "sensor_dates": sensor_dates,
                    "now": now,
                },
            ).fetchall()
        latest_rows = conn.execute(
            """
            SELECT DISTINCT ON (pot_id)
                pot_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                reading_resolution AS resolution,
                sample_count
            FROM sensor_readings
            WHERE source = %(source)s
              AND pot_id = ANY(%(pot_ids)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at <= %(now)s
            ORDER BY
                pot_id,
                recorded_at DESC,
                CASE reading_resolution
                    WHEN %(raw_resolution)s THEN 1
                    WHEN %(hourly_resolution)s THEN 2
                    ELSE 3
                END
            """,
            {
                "timezone": LOCAL_TZ.key,
                "source": source,
                "pot_ids": sensor_pot_ids,
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
                "raw_resolution": RAW_RESOLUTION,
                "hourly_resolution": HOURLY_RESOLUTION,
                "now": datetime.now(LOCAL_TZ),
            },
        ).fetchall()

    rows_by_sensor_key = {
        (row["local_date"], int(row["local_hour"]), row["pot_id"]): row
        for row in rows
    }
    lookup = {}
    for experiment_date, sensor_date in mapped_dates.items():
        for row in rows:
            if row["local_date"] == sensor_date:
                lookup[(experiment_date, int(row["local_hour"]), row["pot_id"])] = row

    return {
        "available": True,
        "source": source,
        "lookup": lookup,
        "mapped_dates": mapped_dates,
        "future_dates": future_dates,
        "latest_states": {row["pot_id"]: row for row in latest_rows},
        "latest_state_at": max((_as_local(row["recorded_at"]) for row in latest_rows), default=None),
        "sensor_dates": sensor_dates,
        "sensor_pot_ids": sensor_pot_ids,
        "first_sensor_date": first_sensor_date,
        "last_sensor_date": last_sensor_date,
        "row_count": len(rows_by_sensor_key),
    }


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
                        PARTITION BY observed_at
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
                  AND observed_at >= %(start_ts)s
                  AND observed_at < %(end_ts)s
            )
            SELECT *
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_at
            """,
            {"location": LOCATION_NAME, "start_ts": start_ts, "end_ts": end_ts},
        ).fetchall()


def _load_latest_weather_at(recorded_at: datetime) -> dict[str, Any] | None:
    recorded_at = _as_local(recorded_at)
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            WITH ranked_weather AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY observed_at
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
                  AND observed_at <= %(recorded_at)s
            )
            SELECT *
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            {"location": LOCATION_NAME, "recorded_at": recorded_at},
        ).fetchone()


def _load_latest_sensor_states(recorded_at: datetime, source: str) -> dict[int, dict[str, Any]]:
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (pot_id)
                pot_id,
                recorded_at,
                soil_moisture_pct
            FROM sensor_readings
            WHERE source = %(source)s
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at < %(recorded_at)s
            ORDER BY pot_id, recorded_at DESC
            """,
            {
                "source": source,
                "recorded_at": recorded_at,
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
            },
        ).fetchall()
    return {row["pot_id"]: row for row in rows}


def _upsert_sensor_rows(conn, rows: list[dict[str, Any]]) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO sensor_readings (
                pot_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                reading_resolution,
                sample_count,
                weather_observed_at,
                weather_source
            )
            VALUES (
                %(pot_id)s,
                %(recorded_at)s,
                %(soil_moisture_pct)s,
                %(air_temperature_c)s,
                %(air_humidity_pct)s,
                %(substrate_temperature_c)s,
                %(source)s,
                %(reading_resolution)s,
                %(sample_count)s,
                %(weather_observed_at)s,
                %(weather_source)s
            )
            ON CONFLICT (pot_id, recorded_at, source, reading_resolution) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                sample_count = EXCLUDED.sample_count,
                weather_observed_at = EXCLUDED.weather_observed_at,
                weather_source = EXCLUDED.weather_source,
                changed_at = now()
            """,
            rows,
        )
    return len(rows)


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
        "pot_id": pot["id"],
        "recorded_at": recorded_at,
        "soil_moisture_pct": round(moisture, 2),
        "air_temperature_c": round(air_temperature, 2),
        "air_humidity_pct": round(air_humidity, 2),
        "substrate_temperature_c": round(substrate_temperature, 2),
        "source": source,
        "reading_resolution": RAW_RESOLUTION,
        "sample_count": 1,
        "weather_observed_at": weather.get("observed_at"),
        "weather_source": weather.get("source", "fallback"),
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
        "temperature_c": temp,
        "relative_humidity_pct": humidity,
        "precipitation_mm": 0.0,
        "wind_speed_kmh": 8.0,
        "wind_gust_kmh": 14.0,
        "evapotranspiration_mm": None,
        "source": "fallback",
    }


def _reading_exists(recorded_at: datetime, source: str) -> bool:
    sensor_pot_ids = _sensor_equipped_pot_ids()
    if not sensor_pot_ids:
        return False
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT pot_id)
            FROM sensor_readings
            WHERE source = %(source)s
              AND reading_resolution = %(raw_resolution)s
              AND recorded_at = %(recorded_at)s
              AND pot_id = ANY(%(sensor_pot_ids)s)
            """,
            {
                "source": source,
                "raw_resolution": RAW_RESOLUTION,
                "recorded_at": recorded_at,
                "sensor_pot_ids": sensor_pot_ids,
            },
        ).fetchone()
    return bool(row and row[0] >= len(sensor_pot_ids))


def _sensor_equipped_pot_ids(candidate_pot_ids: list[int] | None = None) -> list[int]:
    return SensorPlacementService().selected_pot_ids(candidate_pot_ids)


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
    value = _as_local(value).replace(second=0, microsecond=0)
    interval = _reading_interval_minutes()
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


def _local_observed_at(weather: dict[str, Any]) -> datetime:
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
