from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from digital_twin.application.weather_refresh.persistence import (
    empty_import_stats as _empty_import_stats,
    merge_stats as _merge_stats,
    upsert_weather_hourly as _upsert_weather_hourly,
    upsert_weather_hourly_with_stats as _upsert_weather_hourly_with_stats,
)
from digital_twin.application.weather_refresh.rows import (
    fill_missing_local_weather_hours as _fill_missing_local_weather_hours,
    filter_weather_rows_by_date as _filter_weather_rows_by_date,
    json_ready as _json_ready,
    local_bucket as _local_bucket,
    parse_open_meteo_csv as _parse_open_meteo_csv,
    today_local as _today_local,
    year_chunks as _year_chunks,
)
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.infrastructure.weather import open_meteo

CLUJ_NAPOCA = open_meteo.LOCATION

OPEN_METEO_ARCHIVE_URL = open_meteo.OPEN_METEO_ARCHIVE_URL
OPEN_METEO_FORECAST_URL = open_meteo.OPEN_METEO_FORECAST_URL

ARCHIVE_SOURCE = open_meteo.ARCHIVE_SOURCE
FORECAST_SOURCE = open_meteo.FORECAST_SOURCE

ARCHIVE_START = open_meteo.ARCHIVE_START
ARCHIVE_DELAY_DAYS = open_meteo.ARCHIVE_DELAY_DAYS
FORECAST_DAYS = open_meteo.FORECAST_DAYS
FORECAST_MAX_DAYS = open_meteo.FORECAST_MAX_DAYS
FORECAST_PAST_DAYS_MAX = open_meteo.FORECAST_PAST_DAYS_MAX
WEATHER_REFRESH_LOOKBACK_DAYS = 7

HOURLY_VARIABLES = open_meteo.HOURLY_VARIABLES


def cache_cluj_weather_range(start: date, end: date, include_climate: bool = True) -> dict[str, Any]:
    """Cache available Open-Meteo data for Cluj-Napoca.

    Historical/forecast rows go into weather_hourly. Dates beyond the forecast
    horizon are skipped because they are not real weather data yet.
    """
    if end < start:
        raise ValueError("end date must not be before start date")

    initialize_database()
    today = _today_local()
    archive_end = today - timedelta(days=ARCHIVE_DELAY_DAYS)
    forecast_end = today + timedelta(days=FORECAST_MAX_DAYS)

    summary = {
        "location": CLUJ_NAPOCA["location_name"],
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "hourly_archive_rows": 0,
        "hourly_forecast_rows": 0,
        "skipped_ranges": [],
    }

    if start <= archive_end and end >= ARCHIVE_START:
        archive_start = max(start, ARCHIVE_START)
        archive_to = min(end, archive_end)
        summary["hourly_archive_rows"] = cache_open_meteo_archive(archive_start, archive_to)

    if start <= forecast_end and end >= today:
        forecast_start = max(start, today)
        forecast_to = min(end, forecast_end)
        summary["hourly_forecast_rows"] = cache_open_meteo_forecast(forecast_start, forecast_to)

    if end > forecast_end:
        summary["skipped_ranges"].append(
            {
                "start": (forecast_end + timedelta(days=1)).isoformat(),
                "end": end.isoformat(),
                "reason": "No exact real forecast exists this far ahead; climate projection storage is not used for the irrigation scheduler.",
            }
        )

    if start < ARCHIVE_START:
        summary["skipped_ranges"].append(
            {
                "start": start.isoformat(),
                "end": (ARCHIVE_START - timedelta(days=1)).isoformat(),
                "reason": "Open-Meteo historical weather archive starts in 1940.",
            }
        )

    return summary


def cache_open_meteo_archive(start: date, end: date) -> int:
    return _cache_hourly_chunks(
        url=OPEN_METEO_ARCHIVE_URL,
        source=ARCHIVE_SOURCE,
        is_forecast=False,
        start=start,
        end=end,
    )


def cache_open_meteo_archive_with_stats(start: date, end: date) -> dict[str, Any]:
    return _cache_hourly_chunks_with_stats(
        url=OPEN_METEO_ARCHIVE_URL,
        source=ARCHIVE_SOURCE,
        is_forecast=False,
        start=start,
        end=end,
        skip_existing_observed=False,
    )


def cache_open_meteo_forecast(start: date, end: date) -> int:
    stats = _cache_forecast_range_with_stats(
        start=start,
        end=end,
        skip_existing_observed=True,
    )
    return stats["inserted"] + stats["updated"] + stats["unchanged"]


def cache_open_meteo_forecast_with_stats(start: date, end: date) -> dict[str, Any]:
    return _cache_forecast_range_with_stats(
        start=start,
        end=end,
        skip_existing_observed=True,
    )


def refresh_forecast_once_per_day(force: bool = False) -> dict[str, Any]:
    initialize_database()
    refresh_date = _today_local()
    refresh_start = _latest_weather_refresh_start()
    forecast_end = refresh_date + timedelta(days=FORECAST_MAX_DAYS)
    archive_end = min(refresh_date - timedelta(days=ARCHIVE_DELAY_DAYS), forecast_end)
    archive_stats = _empty_import_stats()
    forecast_stats = _empty_import_stats()

    with get_connection(row_factory=dict_row) as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM weather_refresh_runs
            WHERE refresh_date = %(refresh_date)s
              AND source = %(source)s
              AND status = 'completed'
            """,
            {"refresh_date": refresh_date, "source": FORECAST_SOURCE},
        ).fetchone()
        if existing and not force:
            return _json_ready({"already_refreshed": True, **existing})

        conn.execute(
            """
            INSERT INTO weather_refresh_runs (refresh_date, source, status, started_at)
            VALUES (%(refresh_date)s, %(source)s, 'running', now())
            ON CONFLICT (refresh_date, source) DO UPDATE SET
                status = 'running',
                started_at = now(),
                finished_at = NULL,
                error_detail = NULL
            """,
            {"refresh_date": refresh_date, "source": FORECAST_SOURCE},
        )
        conn.commit()

    try:
        if refresh_start <= archive_end:
            archive_stats = cache_open_meteo_archive_with_stats(refresh_start, archive_end)
        forecast_stats = cache_open_meteo_forecast_with_stats(refresh_start, forecast_end)
        stats = _combined_import_stats(archive_stats, forecast_stats)
    except Exception as exc:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE weather_refresh_runs
                SET status = 'failed',
                    finished_at = now(),
                    error_detail = %(error_detail)s
                WHERE refresh_date = %(refresh_date)s
                  AND source = %(source)s
                """,
                {
                    "refresh_date": refresh_date,
                    "source": FORECAST_SOURCE,
                    "error_detail": str(exc),
                },
            )
            conn.commit()
        raise

    with get_connection(row_factory=dict_row) as conn:
        refreshed = conn.execute(
            """
            UPDATE weather_refresh_runs
            SET status = 'completed',
                finished_at = now(),
                inserted_count = %(inserted)s,
                updated_count = %(updated)s,
                unchanged_count = %(unchanged)s,
                skipped_existing_observed_count = %(skipped_existing_observed)s
            WHERE refresh_date = %(refresh_date)s
              AND source = %(source)s
            RETURNING *
            """,
            {
                "refresh_date": refresh_date,
                "source": FORECAST_SOURCE,
                **stats,
            },
        ).fetchone()
        conn.commit()
        return _json_ready(
            {
                "already_refreshed": False,
                "refresh_start": refresh_start,
                "archive_end": archive_end if refresh_start <= archive_end else None,
                "forecast_end": forecast_end,
                "archive": archive_stats,
                "forecast": forecast_stats,
                **refreshed,
            }
        )


def _latest_weather_refresh_start() -> date:
    with get_connection(row_factory=dict_row) as conn:
        latest = conn.execute(
            """
            SELECT max(COALESCE(changed_at, created_at)) AS latest_change
            FROM weather_hourly
            WHERE location_name = %(location)s
            """,
            {"location": CLUJ_NAPOCA["location_name"]},
        ).fetchone()
    return _refresh_start_from_latest_change(latest["latest_change"] if latest else None)


def _refresh_start_from_latest_change(latest_change: datetime | None) -> date:
    latest_date = _today_local()
    if latest_change is not None:
        latest_date = _local_date_from_datetime(latest_change)
    return latest_date - timedelta(days=WEATHER_REFRESH_LOOKBACK_DAYS)


def _combined_import_stats(*items: dict[str, int]) -> dict[str, int]:
    stats = _empty_import_stats()
    for item in items:
        _merge_stats(stats, item)
    return stats


def _local_date_from_datetime(value: datetime) -> date:
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(ZoneInfo(CLUJ_NAPOCA["timezone"])).date()


def _cache_forecast_range_with_stats(
    start: date,
    end: date,
    skip_existing_observed: bool,
) -> dict[str, int]:
    if end < start:
        return _empty_import_stats()
    payload = open_meteo.fetch_json(
        url=OPEN_METEO_FORECAST_URL,
        params=_forecast_request_params(start, end),
    )
    rows = open_meteo.weather_rows_from_payload(payload, source=FORECAST_SOURCE, is_forecast=True)
    rows = open_meteo.filter_weather_rows_by_date(rows, start, end)
    rows = _fill_missing_local_weather_hours(rows)
    return _upsert_weather_hourly_with_stats(rows, skip_existing_observed=skip_existing_observed)


def import_open_meteo_csv(csv_path: str | Path, skip_existing_observed: bool = True) -> dict[str, Any]:
    initialize_database()
    parsed = _parse_open_meteo_csv(csv_path)
    stats = _upsert_weather_hourly_with_stats(
        parsed["rows"],
        skip_existing_observed=skip_existing_observed,
    )
    timestamps = [row["observed_at"] for row in parsed["rows"]]
    return _json_ready(
        {
            "file": str(csv_path),
            "location": CLUJ_NAPOCA["location_name"],
            "source": FORECAST_SOURCE,
            "rows_in_file": len(parsed["rows"]),
            "first_timestamp": min(timestamps) if timestamps else None,
            "last_timestamp": max(timestamps) if timestamps else None,
            "skipped_current_conditions_rows": parsed["skipped_current_conditions_rows"],
            **stats,
        }
    )


def get_weather_cache_summary() -> dict[str, Any]:
    initialize_database()
    with get_connection(row_factory=dict_row) as conn:
        weather = conn.execute(
            """
            SELECT
                source,
                observed_at > now() AS is_forecast,
                count(*) AS row_count,
                min(observed_local_at) AS first_timestamp,
                max(observed_local_at) AS last_timestamp
            FROM weather_hourly
            WHERE location_name = %(location)s
            GROUP BY source, observed_at > now()
            ORDER BY source, observed_at > now()
            """,
            {"location": CLUJ_NAPOCA["location_name"]},
        ).fetchall()
        refreshes = conn.execute(
            """
            SELECT
                refresh_date,
                source,
                status,
                inserted_count,
                updated_count,
                unchanged_count,
                skipped_existing_observed_count,
                started_at,
                finished_at
            FROM weather_refresh_runs
            ORDER BY refresh_date DESC, started_at DESC
            LIMIT 10
            """
        ).fetchall()
        return _json_ready({"hourly_weather": weather, "recent_refreshes": refreshes})


def get_weather_hourly(start: date, end: date, limit: int = 1000) -> list[dict[str, Any]]:
    initialize_database()
    start_dt = _local_bucket(start)
    end_dt = _local_bucket(end + timedelta(days=1))
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            WITH ranked_weather AS (
                SELECT
                    id,
                    location_name,
                    observed_at,
                    observed_local_at,
                    observed_date,
                    observed_hour,
                    source,
                    observed_at > now() AS is_forecast,
                    temperature_c,
                    relative_humidity_pct,
                    precipitation_mm,
                    wind_speed_kmh,
                    wind_gust_kmh,
                    cloud_cover_pct,
                    apparent_temperature_c,
                    precipitation_probability_pct,
                    evapotranspiration_mm,
                    rain_mm,
                    showers_mm,
                    snowfall_cm,
                    weather_code,
                    pressure_msl_hpa,
                    surface_pressure_hpa,
                    wind_direction_10m_deg,
                    soil_temperature_0cm_c,
                    soil_temperature_6cm_c,
                    soil_moisture_0_to_1cm,
                    soil_moisture_1_to_3cm,
                    shortwave_radiation_w_m2,
                    row_number() OVER (
                        PARTITION BY observed_local_at
                        ORDER BY
                            CASE
                                WHEN source = %(archive_source)s THEN 0
                                WHEN source = %(forecast_source)s THEN 1
                                ELSE 2
                            END,
                            id DESC
                    ) AS source_rank
                FROM weather_hourly
                WHERE location_name = %(location)s
                  AND observed_local_at >= %(start_dt)s
                  AND observed_local_at < %(end_dt)s
            )
            SELECT
                id,
                location_name,
                observed_at,
                observed_local_at,
                observed_date,
                observed_hour,
                source,
                observed_at > now() AS is_forecast,
                temperature_c,
                relative_humidity_pct,
                precipitation_mm,
                wind_speed_kmh,
                wind_gust_kmh,
                cloud_cover_pct,
                apparent_temperature_c,
                precipitation_probability_pct,
                evapotranspiration_mm,
                rain_mm,
                showers_mm,
                snowfall_cm,
                weather_code,
                pressure_msl_hpa,
                surface_pressure_hpa,
                wind_direction_10m_deg,
                soil_temperature_0cm_c,
                soil_temperature_6cm_c,
                soil_moisture_0_to_1cm,
                soil_moisture_1_to_3cm,
                shortwave_radiation_w_m2
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_local_at
            LIMIT %(limit)s
            """,
            {
                "archive_source": ARCHIVE_SOURCE,
                "forecast_source": FORECAST_SOURCE,
                "location": CLUJ_NAPOCA["location_name"],
                "start_dt": start_dt,
                "end_dt": end_dt,
                "limit": limit,
            },
        ).fetchall()
        return _json_ready(rows)


def _cache_hourly_chunks(url: str, source: str, is_forecast: bool, start: date, end: date) -> int:
    total_rows = 0
    for chunk_start, chunk_end in _year_chunks(start, end):
        payload = open_meteo.fetch_json(url, _hourly_request_params(chunk_start, chunk_end))
        rows = open_meteo.weather_rows_from_payload(payload, source=source, is_forecast=is_forecast)
        rows = _fill_missing_local_weather_hours(rows)
        if rows:
            total_rows += _upsert_weather_hourly(rows)
    return total_rows


def _cache_hourly_chunks_with_stats(
    url: str,
    source: str,
    is_forecast: bool,
    start: date,
    end: date,
    skip_existing_observed: bool,
) -> dict[str, int]:
    stats = _empty_import_stats()
    for chunk_start, chunk_end in _year_chunks(start, end):
        payload = open_meteo.fetch_json(url, _hourly_request_params(chunk_start, chunk_end))
        rows = open_meteo.weather_rows_from_payload(payload, source=source, is_forecast=is_forecast)
        rows = _fill_missing_local_weather_hours(rows)
        if rows:
            _merge_stats(stats, _upsert_weather_hourly_with_stats(rows, skip_existing_observed=skip_existing_observed))
    return stats


def _hourly_request_params(start: date, end: date) -> dict[str, Any]:
    return open_meteo.hourly_request_params(start, end)


def _forecast_request_params(start: date, end: date) -> dict[str, Any]:
    return open_meteo.forecast_request_params(start, end, today=_today_local())

