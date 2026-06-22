from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from digital_twin.application.weather_refresh.cache_reader import WeatherCacheReader
from digital_twin.application.weather_refresh.persistence import WeatherPersistence
from digital_twin.application.weather_refresh.rows import WeatherRows
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.infrastructure.open_meteo import OpenMeteoClient


class WeatherIngestion:
    """Coordinates Open-Meteo weather imports and refresh runs."""

    def __init__(
        self,
        rows: WeatherRows | None = None,
        persistence: WeatherPersistence | None = None,
        open_meteo: OpenMeteoClient | None = None,
        cache_reader: WeatherCacheReader | None = None,
    ) -> None:
        self.open_meteo = open_meteo or (rows.open_meteo if rows is not None else OpenMeteoClient())
        self.rows = rows or WeatherRows(self.open_meteo)
        self.persistence = persistence or WeatherPersistence(self.rows, self.open_meteo)
        self.cache_reader = cache_reader or WeatherCacheReader(self.open_meteo)

    def cache_cluj_weather_range(self, start: date, end: date, include_climate: bool = True) -> dict[str, Any]:
        """Cache available Open-Meteo data for Cluj-Napoca.

        Historical/forecast rows go into weather_hourly. Dates beyond the forecast
        horizon are skipped because they are not real weather data yet.
        """
        if end < start:
            raise ValueError("end date must not be before start date")

        initialize_database()
        today = self.open_meteo.today_local()
        archive_end = today - timedelta(days=self.open_meteo.archive_delay_days)
        forecast_end = today + timedelta(days=self.open_meteo.forecast_max_days)

        summary = {
            "location": self.open_meteo.location.name,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "hourly_archive_rows": 0,
            "hourly_forecast_rows": 0,
            "skipped_ranges": [],
        }

        if start <= archive_end and end >= self.open_meteo.archive_start:
            archive_start = max(start, self.open_meteo.archive_start)
            archive_to = min(end, archive_end)
            summary["hourly_archive_rows"] = self.cache_open_meteo_archive(archive_start, archive_to)

        if start <= forecast_end and end >= today:
            forecast_start = max(start, today)
            forecast_to = min(end, forecast_end)
            summary["hourly_forecast_rows"] = self.cache_open_meteo_forecast(forecast_start, forecast_to)

        if end > forecast_end:
            summary["skipped_ranges"].append(
                {
                    "start": (forecast_end + timedelta(days=1)).isoformat(),
                    "end": end.isoformat(),
                    "reason": "No exact real forecast exists this far ahead; climate projection storage is not used for the irrigation scheduler.",
                }
            )

        if start < self.open_meteo.archive_start:
            summary["skipped_ranges"].append(
                {
                    "start": start.isoformat(),
                    "end": (self.open_meteo.archive_start - timedelta(days=1)).isoformat(),
                    "reason": "Open-Meteo historical weather archive starts in 1940.",
                }
            )

        return summary

    def import_weather_csv(self, csv_path: str | Path, skip_existing_observed: bool = True) -> dict[str, Any]:
        initialize_database()
        parsed = self.rows.parse_open_meteo_csv(csv_path)
        rows = parsed["rows"]
        stats = self.persistence.upsert_weather_hourly_with_stats(
            rows,
            skip_existing_observed=skip_existing_observed,
        )
        timestamps = [row["observed_at"] for row in rows]
        return self.open_meteo.json_ready(
            {
                "file": str(csv_path),
                "location": self.open_meteo.location.name,
                "source": self.open_meteo.forecast_source,
                "rows_in_file": len(rows),
                "first_timestamp": min(timestamps) if timestamps else None,
                "last_timestamp": max(timestamps) if timestamps else None,
                "skipped_current_conditions_rows": parsed["skipped_current_conditions_rows"],
                **stats,
            }
        )

    def cache_open_meteo_archive(self, start: date, end: date) -> int:
        return self._cache_hourly_chunks(
            url=self.open_meteo.archive_url,
            source=self.open_meteo.archive_source,
            is_forecast=False,
            start=start,
            end=end,
        )

    def cache_open_meteo_archive_with_stats(self, start: date, end: date) -> dict[str, Any]:
        return self._cache_hourly_chunks_with_stats(
            url=self.open_meteo.archive_url,
            source=self.open_meteo.archive_source,
            is_forecast=False,
            start=start,
            end=end,
            skip_existing_observed=False,
        )

    def cache_open_meteo_forecast(self, start: date, end: date) -> int:
        stats = self._cache_forecast_range_with_stats(
            start=start,
            end=end,
            skip_existing_observed=True,
        )
        return stats["inserted"] + stats["updated"] + stats["unchanged"]

    def cache_open_meteo_forecast_with_stats(self, start: date, end: date) -> dict[str, Any]:
        return self._cache_forecast_range_with_stats(
            start=start,
            end=end,
            skip_existing_observed=True,
        )

    def refresh_forecast_once_per_day(self, force: bool = False) -> dict[str, Any]:
        initialize_database()
        refresh_date = self.open_meteo.today_local()
        refresh_start = self.cache_reader.latest_weather_refresh_start()
        forecast_end = refresh_date + timedelta(days=self.open_meteo.forecast_max_days)
        archive_end = min(refresh_date - timedelta(days=self.open_meteo.archive_delay_days), forecast_end)
        archive_stats = self.persistence.empty_import_stats()
        forecast_stats = self.persistence.empty_import_stats()

        with get_connection(row_factory=dict_row) as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM weather_refresh_runs
                WHERE refresh_date = %(refresh_date)s
                  AND source = %(source)s
                  AND status = 'completed'
                """,
                {"refresh_date": refresh_date, "source": self.open_meteo.forecast_source},
            ).fetchone()
            if existing and not force:
                return self.open_meteo.json_ready({"already_refreshed": True, **existing})

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
                {"refresh_date": refresh_date, "source": self.open_meteo.forecast_source},
            )
            conn.commit()

        try:
            if refresh_start <= archive_end:
                archive_stats = self.cache_open_meteo_archive_with_stats(refresh_start, archive_end)
            forecast_stats = self.cache_open_meteo_forecast_with_stats(refresh_start, forecast_end)
            stats = self._combined_import_stats(archive_stats, forecast_stats)
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
                        "source": self.open_meteo.forecast_source,
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
                    "source": self.open_meteo.forecast_source,
                    **stats,
                },
            ).fetchone()
            conn.commit()
            return self.open_meteo.json_ready(
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

    def _combined_import_stats(self, *items: dict[str, int]) -> dict[str, int]:
        stats = self.persistence.empty_import_stats()
        for item in items:
            self.persistence.merge_stats(stats, item)
        return stats

    def _cache_forecast_range_with_stats(
        self,
        start: date,
        end: date,
        skip_existing_observed: bool,
    ) -> dict[str, int]:
        if end < start:
            return self.persistence.empty_import_stats()
        payload = self.open_meteo.fetch_json(
            url=self.open_meteo.forecast_url,
            params=self._forecast_request_params(start, end),
        )
        rows = self.open_meteo.weather_rows_from_payload(
            payload,
            source=self.open_meteo.forecast_source,
            is_forecast=True,
        )
        rows = self.open_meteo.filter_weather_rows_by_date(rows, start, end)
        rows = self.rows.fill_missing_local_weather_hours(rows)
        return self.persistence.upsert_weather_hourly_with_stats(rows, skip_existing_observed=skip_existing_observed)

    def _cache_hourly_chunks(self, url: str, source: str, is_forecast: bool, start: date, end: date) -> int:
        total_rows = 0
        for chunk_start, chunk_end in self.open_meteo.year_chunks(start, end):
            payload = self.open_meteo.fetch_json(url, self._hourly_request_params(chunk_start, chunk_end))
            rows = self.open_meteo.weather_rows_from_payload(payload, source=source, is_forecast=is_forecast)
            rows = self.rows.fill_missing_local_weather_hours(rows)
            if rows:
                total_rows += self.persistence.upsert_weather_hourly(rows)
        return total_rows

    def _cache_hourly_chunks_with_stats(
        self,
        url: str,
        source: str,
        is_forecast: bool,
        start: date,
        end: date,
        skip_existing_observed: bool,
    ) -> dict[str, int]:
        stats = self.persistence.empty_import_stats()
        for chunk_start, chunk_end in self.open_meteo.year_chunks(start, end):
            payload = self.open_meteo.fetch_json(url, self._hourly_request_params(chunk_start, chunk_end))
            rows = self.open_meteo.weather_rows_from_payload(payload, source=source, is_forecast=is_forecast)
            rows = self.rows.fill_missing_local_weather_hours(rows)
            if rows:
                self.persistence.merge_stats(
                    stats,
                    self.persistence.upsert_weather_hourly_with_stats(rows, skip_existing_observed=skip_existing_observed),
                )
        return stats

    def _hourly_request_params(self, start: date, end: date) -> dict[str, Any]:
        return self.open_meteo.hourly_request_params(start, end)

    def _forecast_request_params(self, start: date, end: date) -> dict[str, Any]:
        return self.open_meteo.forecast_request_params(start, end, today=self.open_meteo.today_local())
