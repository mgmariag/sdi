from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from digital_twin.domain.weather import DEFAULT_WEATHER_LOCATION, WeatherLocation

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

ARCHIVE_SOURCE = "open-meteo-archive"
FORECAST_SOURCE = "open-meteo-forecast"

ARCHIVE_START = date(1940, 1, 1)
ARCHIVE_DELAY_DAYS = 5
# Open-Meteo forecast supports 16 future calendar days including today and up to 92 past days.
FORECAST_DAYS = 16
FORECAST_MAX_DAYS = FORECAST_DAYS - 1
FORECAST_PAST_DAYS_MAX = 92

HOURLY_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "cloud_cover",
    "et0_fao_evapotranspiration",
    "vapour_pressure_deficit",
]


class OpenMeteoClient:
    """Fetches and normalizes Open-Meteo weather data for the configured location."""

    archive_url = OPEN_METEO_ARCHIVE_URL
    forecast_url = OPEN_METEO_FORECAST_URL
    archive_source = ARCHIVE_SOURCE
    forecast_source = FORECAST_SOURCE
    archive_start = ARCHIVE_START
    archive_delay_days = ARCHIVE_DELAY_DAYS
    forecast_days = FORECAST_DAYS
    forecast_max_days = FORECAST_MAX_DAYS
    forecast_past_days_max = FORECAST_PAST_DAYS_MAX
    hourly_variables = tuple(HOURLY_VARIABLES)

    def __init__(self, location: WeatherLocation | None = None) -> None:
        self.location = location if location is not None else DEFAULT_WEATHER_LOCATION

    def hourly_request_params(self, start: date, end: date) -> dict[str, Any]:
        return {
            **self.location.open_meteo_params(),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(self.hourly_variables),
        }

    def forecast_request_params(self, start: date, end: date, today: date | None = None) -> dict[str, Any]:
        local_today = today or self.today_local()
        past_days = max(0, min((local_today - start).days, self.forecast_past_days_max))
        forecast_days = max(1, min((end - local_today).days + 1, self.forecast_days))
        return {
            **self.location.open_meteo_params(),
            "past_days": past_days,
            "forecast_days": forecast_days,
            "hourly": ",".join(self.hourly_variables),
        }

    def fetch_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        request_url = f"{url}?{urlencode(params)}"
        with urlopen(request_url, timeout=60) as response:
            body = response.read().decode("utf-8")
        payload = json.loads(body)
        if "error" in payload:
            raise RuntimeError(payload.get("reason", "Open-Meteo returned an error"))
        return payload

    def weather_rows_from_payload(
        self,
        payload: dict[str, Any],
        source: str,
        is_forecast: bool,
    ) -> list[dict[str, Any]]:
        _ = is_forecast
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        rows = []
        for index, timestamp in enumerate(times):
            raw_values = {name: self._series_value(hourly, name, index) for name in self.hourly_variables}
            raw_payload = {name: self.json_ready(value) for name, value in raw_values.items()}
            observed_local_at = self.local_bucket_from_open_meteo(timestamp)
            rows.append(
                {
                    "location_name": self.location.name,
                    "latitude": Decimal(str(self.location.latitude)),
                    "longitude": Decimal(str(self.location.longitude)),
                    "observed_at": self.local_datetime_from_open_meteo(timestamp),
                    "observed_local_at": observed_local_at,
                    "observed_date": observed_local_at.date(),
                    "observed_hour": observed_local_at.hour,
                    "source": source,
                    "temperature_c": raw_values["temperature_2m"],
                    "relative_humidity_pct": raw_values["relative_humidity_2m"],
                    "precipitation_mm": raw_values["precipitation"],
                    "wind_speed_kmh": raw_values["wind_speed_10m"],
                    "wind_gust_kmh": raw_values["wind_gusts_10m"],
                    "cloud_cover_pct": raw_values["cloud_cover"],
                    "apparent_temperature_c": None,
                    "is_day": None,
                    "precipitation_probability_pct": None,
                    "evapotranspiration_mm": raw_values["et0_fao_evapotranspiration"],
                    "rain_mm": None,
                    "showers_mm": None,
                    "snowfall_cm": None,
                    "weather_code": None,
                    "pressure_msl_hpa": None,
                    "surface_pressure_hpa": None,
                    "wind_direction_10m_deg": None,
                    "soil_temperature_0cm_c": None,
                    "soil_temperature_6cm_c": None,
                    "soil_moisture_0_to_1cm": None,
                    "soil_moisture_1_to_3cm": None,
                    "shortwave_radiation_w_m2": None,
                    "raw_payload": raw_payload,
                }
            )
        return rows

    @staticmethod
    def filter_weather_rows_by_date(rows: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
        return [row for row in rows if start <= row["observed_date"] <= end]

    def parse_csv(self, csv_path: str | Path) -> dict[str, Any]:
        path = Path(csv_path)
        with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            csv_rows = list(csv.reader(csv_file))

        metadata = self._parse_csv_metadata(csv_rows)
        header_index = self._find_hourly_header_index(csv_rows)
        if header_index is None:
            raise ValueError("Could not find an hourly forecast table in the Open-Meteo CSV")

        header = csv_rows[header_index]
        rows = []
        for raw_row in csv_rows[header_index + 1:]:
            if not raw_row or not raw_row[0].strip():
                break
            if len(raw_row) < len(header):
                continue
            rows.append(self._csv_hourly_row(header, raw_row, metadata))

        return {
            "rows": rows,
            "skipped_current_conditions_rows": self._count_current_condition_rows(csv_rows, header_index),
        }

    @staticmethod
    def year_chunks(start: date, end: date):
        cursor = start
        while cursor <= end:
            chunk_end = min(date(cursor.year, 12, 31), end)
            yield cursor, chunk_end
            cursor = chunk_end + timedelta(days=1)

    def local_datetime(self, day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=self.location.timezone_info())

    @staticmethod
    def local_bucket(day: date) -> datetime:
        return datetime(day.year, day.month, day.day)

    def today_local(self) -> date:
        return datetime.now(self.location.timezone_info()).date()

    def local_bucket_from_observed_at(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(self.location.timezone_info()).replace(tzinfo=None)

    def local_bucket_from_open_meteo(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo:
            return parsed.astimezone(self.location.timezone_info()).replace(tzinfo=None)
        return parsed

    def local_datetime_from_open_meteo(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo:
            return parsed
        return parsed.replace(tzinfo=self.location.timezone_info())

    def json_ready(self, value):
        if isinstance(value, list):
            return [self.json_ready(item) for item in value]
        if isinstance(value, dict):
            return {key: self.json_ready(item) for key, item in value.items()}
        if isinstance(value, Decimal):
            return float(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    @staticmethod
    def _series_value(series: dict[str, Any], name: str, index: int):
        values = series.get(name) or []
        if index >= len(values):
            return None
        value = values[index]
        if value is None:
            return None
        return Decimal(str(value))

    @staticmethod
    def _parse_csv_metadata(csv_rows: list[list[str]]) -> dict[str, Any]:
        if len(csv_rows) < 2:
            return {}
        keys = csv_rows[0]
        values = csv_rows[1]
        return {key: values[index] for index, key in enumerate(keys) if index < len(values)}

    @staticmethod
    def _find_hourly_header_index(csv_rows: list[list[str]]) -> int | None:
        candidates = [
            index
            for index, row in enumerate(csv_rows)
            if row and row[0] == "time" and len(row) > 10
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda index: len(csv_rows[index]))

    @staticmethod
    def _count_current_condition_rows(csv_rows: list[list[str]], hourly_header_index: int) -> int:
        count = 0
        for row in csv_rows[:hourly_header_index]:
            if row and row[0].startswith("20"):
                count += 1
        return count

    def _csv_hourly_row(self, header: list[str], raw_row: list[str], metadata: dict[str, Any]) -> dict[str, Any]:
        values = {header[index]: raw_row[index] for index in range(min(len(header), len(raw_row)))}
        by_name = {
            self._normalize_csv_column(column): self._csv_decimal(value)
            for column, value in values.items()
            if column != "time"
        }
        raw_payload = {
            self._normalize_csv_column(column): self.json_ready(self._csv_decimal(value))
            for column, value in values.items()
            if column != "time"
        }
        observed_local_at = self._csv_local_datetime(values["time"])
        return {
            "location_name": self.location.name,
            "latitude": Decimal(str(metadata.get("latitude") or self.location.latitude)),
            "longitude": Decimal(str(metadata.get("longitude") or self.location.longitude)),
            "observed_at": self._csv_datetime(values["time"], metadata),
            "observed_local_at": observed_local_at,
            "observed_date": observed_local_at.date(),
            "observed_hour": observed_local_at.hour,
            "source": self.forecast_source,
            "temperature_c": by_name.get("temperature_2m"),
            "relative_humidity_pct": by_name.get("relative_humidity_2m"),
            "precipitation_mm": by_name.get("precipitation"),
            "wind_speed_kmh": by_name.get("wind_speed_10m"),
            "wind_gust_kmh": by_name.get("wind_gusts_10m"),
            "cloud_cover_pct": by_name.get("cloud_cover"),
            "apparent_temperature_c": by_name.get("apparent_temperature"),
            "is_day": self._csv_bool(values.get(self._original_column(header, "is_day"))),
            "precipitation_probability_pct": by_name.get("precipitation_probability"),
            "evapotranspiration_mm": by_name.get("evapotranspiration"),
            "rain_mm": by_name.get("rain"),
            "showers_mm": by_name.get("showers"),
            "snowfall_cm": by_name.get("snowfall"),
            "weather_code": self._csv_int(values.get(self._original_column(header, "weather_code"))),
            "pressure_msl_hpa": by_name.get("pressure_msl"),
            "surface_pressure_hpa": by_name.get("surface_pressure"),
            "wind_direction_10m_deg": by_name.get("wind_direction_10m"),
            "soil_temperature_0cm_c": by_name.get("soil_temperature_0cm"),
            "soil_temperature_6cm_c": by_name.get("soil_temperature_6cm"),
            "soil_moisture_0_to_1cm": by_name.get("soil_moisture_0_to_1cm"),
            "soil_moisture_1_to_3cm": by_name.get("soil_moisture_1_to_3cm"),
            "shortwave_radiation_w_m2": by_name.get("shortwave_radiation"),
            "raw_payload": raw_payload,
        }

    @staticmethod
    def _normalize_csv_column(column: str) -> str:
        name = column.split(" (", 1)[0].strip()
        return name.lower().replace(" ", "_").replace("-", "_")

    def _original_column(self, header: list[str], normalized_name: str) -> str | None:
        for column in header:
            if self._normalize_csv_column(column) == normalized_name:
                return column
        return None

    @staticmethod
    def _csv_decimal(value: str | None) -> Decimal | None:
        if value is None or value == "":
            return None
        decimal_value = Decimal(value)
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return None
        return decimal_value

    @staticmethod
    def _csv_int(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        return int(Decimal(value))

    @staticmethod
    def _csv_bool(value: str | None) -> bool | None:
        if value is None or value == "":
            return None
        return bool(int(Decimal(value)))

    @staticmethod
    def _csv_datetime(value: str, metadata: dict[str, Any]) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo:
            return parsed
        offset_seconds = int(float(metadata.get("utc_offset_seconds") or 0))
        if offset_seconds == 0:
            return parsed.replace(tzinfo=datetime_timezone.utc)
        return parsed.replace(tzinfo=datetime_timezone(timedelta(seconds=offset_seconds)))

    def _csv_local_datetime(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo:
            return parsed.astimezone(self.location.timezone_info()).replace(tzinfo=None)
        return parsed
