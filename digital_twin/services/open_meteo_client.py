from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.core.exceptions import WeatherProviderError
from weather_ingestion import (
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_FORECAST_URL,
    _fetch_json,
    _hourly_request_params,
)


class OpenMeteoClient:
    """HTTP client for Open-Meteo hourly archive and forecast payloads."""

    def fetch_archive_hourly(self, start: date, end: date) -> dict[str, Any]:
        return self._fetch(OPEN_METEO_ARCHIVE_URL, start, end)

    def fetch_forecast_hourly(self, start: date, end: date) -> dict[str, Any]:
        return self._fetch(OPEN_METEO_FORECAST_URL, start, end)

    @staticmethod
    def request_params(start: date, end: date) -> dict[str, Any]:
        return _hourly_request_params(start, end)

    def _fetch(self, url: str, start: date, end: date) -> dict[str, Any]:
        try:
            return _fetch_json(url, self.request_params(start, end))
        except Exception as exc:
            raise WeatherProviderError(str(exc)) from exc

