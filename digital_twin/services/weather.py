from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from digital_twin.core.exceptions import InvalidDateRange
from digital_twin.db.weather_repository import WeatherRepository
from weather_ingestion import (
    cache_cluj_weather_range,
    import_open_meteo_csv,
    refresh_forecast_once_per_day,
)


class WeatherService:
    """Coordinates weather ingestion and weather cache reads."""

    def __init__(self, repository: WeatherRepository | None = None) -> None:
        self.repository = repository or WeatherRepository()

    def cache_cluj_range(self, start: date, end: date, include_climate: bool = True) -> dict[str, Any]:
        if end < start:
            raise InvalidDateRange("end date must not be before start date")
        return cache_cluj_weather_range(start=start, end=end, include_climate=include_climate)

    def refresh_forecast(self, force: bool = False) -> dict[str, Any]:
        return refresh_forecast_once_per_day(force=force)

    def import_csv(self, csv_path: str | Path, skip_existing_observed: bool = True) -> dict[str, Any]:
        return import_open_meteo_csv(csv_path=csv_path, skip_existing_observed=skip_existing_observed)

    def summary(self) -> dict[str, Any]:
        return self.repository.summary()

    def hourly(self, start: date, end: date, limit: int = 1000) -> list[dict[str, Any]]:
        if end < start:
            raise InvalidDateRange("end date must not be before start date")
        return self.repository.hourly(start=start, end=end, limit=limit)

