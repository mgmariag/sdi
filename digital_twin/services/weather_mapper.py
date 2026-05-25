from __future__ import annotations

from typing import Any

from weather_ingestion import _hourly_rows, _parse_open_meteo_csv


class WeatherMapper:
    """Maps provider/file payloads into weather-hourly row dictionaries."""

    def hourly_rows(self, payload: dict[str, Any], source: str, is_forecast: bool) -> list[dict[str, Any]]:
        return _hourly_rows(payload, source=source, is_forecast=is_forecast)

    def csv_rows(self, csv_path: str) -> dict[str, Any]:
        return _parse_open_meteo_csv(csv_path)

