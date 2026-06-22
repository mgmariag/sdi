from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from digital_twin.application.exceptions import InvalidDateRange
from digital_twin.application.weather_refresh.cache_reader import WeatherCacheReader
from digital_twin.application.weather_refresh.ingestion import WeatherIngestion


class WeatherRefreshService:
    """Coordinates weather refresh commands and cache reads."""

    def __init__(
        self,
        ingestion: WeatherIngestion | None = None,
        cache_reader: WeatherCacheReader | None = None,
    ) -> None:
        self.ingestion = ingestion or WeatherIngestion()
        self.cache_reader = cache_reader or self.ingestion.cache_reader

    def cache_cluj_range(self, start: date, end: date, include_climate: bool = True) -> dict[str, Any]:
        if end < start:
            raise InvalidDateRange("end date must not be before start date")
        return self.ingestion.cache_cluj_weather_range(start=start, end=end, include_climate=include_climate)

    def refresh_forecast(self, force: bool = False) -> dict[str, Any]:
        return self.ingestion.refresh_forecast_once_per_day(force=force)

    def summary(self) -> dict[str, Any]:
        return self.cache_reader.summary()

    def hourly(self, start: date, end: date, limit: int = 1000) -> list[dict[str, Any]]:
        if end < start:
            raise InvalidDateRange("end date must not be before start date")
        return self.cache_reader.hourly(start=start, end=end, limit=limit)

    def import_csv(self, csv_path: str | Path, skip_existing_observed: bool = True) -> dict[str, Any]:
        return self.ingestion.import_weather_csv(
            csv_path,
            skip_existing_observed=skip_existing_observed,
        )
