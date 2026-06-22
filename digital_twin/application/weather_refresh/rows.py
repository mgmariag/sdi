from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from digital_twin.infrastructure.open_meteo import OpenMeteoClient

WEATHER_INSERT_COLUMNS = [
    "location_name",
    "latitude",
    "longitude",
    "observed_at",
    "observed_local_at",
    "observed_date",
    "observed_hour",
    "source",
    "temperature_c",
    "relative_humidity_pct",
    "precipitation_mm",
    "wind_speed_kmh",
    "wind_gust_kmh",
    "cloud_cover_pct",
    "apparent_temperature_c",
    "is_day",
    "precipitation_probability_pct",
    "evapotranspiration_mm",
    "rain_mm",
    "showers_mm",
    "snowfall_cm",
    "weather_code",
    "pressure_msl_hpa",
    "surface_pressure_hpa",
    "wind_direction_10m_deg",
    "soil_temperature_0cm_c",
    "soil_temperature_6cm_c",
    "soil_moisture_0_to_1cm",
    "soil_moisture_1_to_3cm",
    "shortwave_radiation_w_m2",
    "raw_payload",
]

WEATHER_COMPARISON_COLUMNS = [
    column
    for column in WEATHER_INSERT_COLUMNS
    if column
    not in {
        "location_name",
        "latitude",
        "longitude",
        "observed_at",
        "observed_local_at",
        "observed_date",
        "observed_hour",
        "source",
        "raw_payload",
    }
]


class WeatherRows:
    """Builds and normalizes weather row payloads."""

    def __init__(self, open_meteo: OpenMeteoClient | None = None) -> None:
        self.open_meteo = open_meteo or OpenMeteoClient()

    def fill_missing_local_weather_hours(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return rows

        by_bucket = {row["observed_local_at"]: row for row in rows}
        dates = sorted({bucket.date() for bucket in by_bucket})
        for cursor in dates:
            for hour in range(24):
                bucket = datetime.combine(cursor, time(hour, 0))
                if bucket not in by_bucket:
                    buckets = sorted(by_bucket)
                    by_bucket[bucket] = self._synthetic_local_weather_row(bucket, buckets, by_bucket)

        return [by_bucket[bucket] for bucket in sorted(by_bucket)]

    def parse_open_meteo_csv(self, csv_path: str | Path) -> dict[str, Any]:
        parsed = self.open_meteo.parse_csv(csv_path)
        return {
            **parsed,
            "rows": self.fill_missing_local_weather_hours(parsed["rows"]),
        }

    def _synthetic_local_weather_row(
        self,
        bucket: datetime,
        sorted_buckets: list[datetime],
        by_bucket: dict[datetime, dict[str, Any]],
    ) -> dict[str, Any]:
        previous_bucket = max((item for item in sorted_buckets if item < bucket), default=None)
        next_bucket = min((item for item in sorted_buckets if item > bucket), default=None)
        previous = by_bucket.get(previous_bucket) if previous_bucket else None
        following = by_bucket.get(next_bucket) if next_bucket else None
        template = previous or following
        if template is None:
            raise ValueError("Cannot synthesize weather row without a neighboring row")

        row = dict(template)
        row["observed_local_at"] = bucket
        row["observed_date"] = bucket.date()
        row["observed_hour"] = bucket.hour
        row["observed_at"] = bucket.replace(tzinfo=self.open_meteo.location.timezone_info())
        row["raw_payload"] = {
            **(template.get("raw_payload") or {}),
            "synthetic_local_hour": True,
            "filled_from_previous": previous_bucket.isoformat() if previous_bucket else None,
            "filled_from_next": next_bucket.isoformat() if next_bucket else None,
        }

        if previous and following:
            for column in WEATHER_COMPARISON_COLUMNS:
                row[column] = self._interpolated_weather_value(previous.get(column), following.get(column), template.get(column))
        return row

    def _interpolated_weather_value(self, previous, following, default):
        if previous is None or following is None:
            return default
        if isinstance(previous, bool) and isinstance(following, bool):
            return previous or following
        if isinstance(previous, Decimal) or isinstance(following, Decimal):
            return (Decimal(str(previous)) + Decimal(str(following))) / Decimal("2")
        if isinstance(previous, (int, float)) and isinstance(following, (int, float)):
            return (previous + following) / 2
        return default
