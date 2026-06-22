from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.infrastructure.open_meteo import OpenMeteoClient

WEATHER_REFRESH_LOOKBACK_DAYS = 7


class WeatherCacheReader:
    """Reads weather cache state and cached hourly weather rows."""

    def __init__(self, open_meteo: OpenMeteoClient | None = None) -> None:
        self.open_meteo = open_meteo or OpenMeteoClient()

    def summary(self) -> dict[str, Any]:
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
                {"location": self.open_meteo.location.name},
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
            return self.open_meteo.json_ready({"hourly_weather": weather, "recent_refreshes": refreshes})

    def hourly(self, start: date, end: date, limit: int = 1000) -> list[dict[str, Any]]:
        initialize_database()
        start_dt = self.open_meteo.local_bucket(start)
        end_dt = self.open_meteo.local_bucket(end + timedelta(days=1))
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
                    "archive_source": self.open_meteo.archive_source,
                    "forecast_source": self.open_meteo.forecast_source,
                    "location": self.open_meteo.location.name,
                    "start_dt": start_dt,
                    "end_dt": end_dt,
                    "limit": limit,
                },
            ).fetchall()
            return self.open_meteo.json_ready(rows)

    def latest_weather_refresh_start(self) -> date:
        with get_connection(row_factory=dict_row) as conn:
            latest = conn.execute(
                """
                SELECT max(COALESCE(changed_at, created_at)) AS latest_change
                FROM weather_hourly
                WHERE location_name = %(location)s
                """,
                {"location": self.open_meteo.location.name},
            ).fetchone()
        return self.refresh_start_from_latest_change(latest["latest_change"] if latest else None)

    def refresh_start_from_latest_change(self, latest_change: datetime | None) -> date:
        latest_date = self.open_meteo.today_local()
        if latest_change is not None:
            latest_date = self._local_date_from_datetime(latest_change)
        return latest_date - timedelta(days=WEATHER_REFRESH_LOOKBACK_DAYS)

    def _local_date_from_datetime(self, value: datetime) -> date:
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(self.open_meteo.location.timezone_info()).date()
