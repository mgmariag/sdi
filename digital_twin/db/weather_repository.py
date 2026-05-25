from __future__ import annotations

from datetime import date
from typing import Any

from weather_ingestion import get_weather_cache_summary, get_weather_hourly


class WeatherRepository:
    """Weather cache read access."""

    def summary(self) -> dict[str, Any]:
        return get_weather_cache_summary()

    def hourly(self, start: date, end: date, limit: int = 1000) -> list[dict[str, Any]]:
        return get_weather_hourly(start=start, end=end, limit=limit)

