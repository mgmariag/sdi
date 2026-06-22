from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WeatherLocation:
    name: str
    latitude: float
    longitude: float
    timezone: str

    def as_dict(self) -> dict[str, float | str]:
        return {
            "location_name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
        }

    def open_meteo_params(self) -> dict[str, float | str]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
        }

    def timezone_info(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

DEFAULT_WEATHER_LOCATION = WeatherLocation(
    name="Cluj-Napoca",
    latitude=46.7712,
    longitude=23.6236,
    timezone="Europe/Bucharest",
)
_LOCAL_TZ = DEFAULT_WEATHER_LOCATION.timezone_info()


def local_observed_at(weather: dict[str, Any]) -> datetime:
    observed_local_at = weather.get("observed_local_at")
    if observed_local_at is not None:
        if observed_local_at.tzinfo is None:
            return observed_local_at.replace(tzinfo=_LOCAL_TZ)
        return observed_local_at.astimezone(_LOCAL_TZ)
    observed_at = weather["observed_at"]
    if observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=_LOCAL_TZ)
    return observed_at.astimezone(_LOCAL_TZ)
