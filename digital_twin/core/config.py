from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from functools import lru_cache

from digital_twin.domain.sensors import DEFAULT_SENSOR_SOURCE
from digital_twin.domain.weather import (
    DEFAULT_LOCAL_TIMEZONE,
    DEFAULT_WEATHER_LATITUDE,
    DEFAULT_WEATHER_LOCATION_NAME,
    DEFAULT_WEATHER_LONGITUDE,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _default_worker_count() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def _env_date(name: str, default: date) -> date:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return date.fromisoformat(value)


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_time(name: str, default: time) -> time:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return time.fromisoformat(value.strip())


@dataclass(frozen=True)
class WeatherLocation:
    name: str = DEFAULT_WEATHER_LOCATION_NAME
    latitude: float = DEFAULT_WEATHER_LATITUDE
    longitude: float = DEFAULT_WEATHER_LONGITUDE
    timezone: str = DEFAULT_LOCAL_TIMEZONE


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://dt_user:dt_password@localhost:5432/digital_twin"
    cors_origins: tuple[str, ...] = field(default_factory=lambda: ("http://localhost:8080", "http://localhost:8081"))
    default_pot_count: int = 200
    default_seed: int = 2026
    local_timezone: str = DEFAULT_LOCAL_TIMEZONE
    weather_location: WeatherLocation = field(default_factory=WeatherLocation)
    weather_refresh_on_startup: bool = True
    sensor_source: str = DEFAULT_SENSOR_SOURCE
    sensor_history_start: date = field(default_factory=lambda: date.today() - timedelta(days=29))
    sensor_history_end: date | None = None
    sensor_reading_interval_minutes: int = 15
    sensor_seed_history_on_startup: bool = True
    sensor_scheduler_enabled: bool = True
    sensor_cleanup_enabled: bool = True
    sensor_cleanup_time: time = time(3, 15)
    prescription_scheduler_enabled: bool = True
    prescription_dispatch_time: time = time(21, 0)
    actuation_scheduler_enabled: bool = True
    actuation_poll_seconds: int = 60
    experiment_snapshot_cache_ttl_seconds: int = 15 * 60
    experiment_precompute_related: bool = True
    experiment_precompute_anfis: bool = True
    default_scenario_seed: int = 2026
    default_anfis_parallel_workers: int = field(default_factory=_default_worker_count)
    default_anfis_parallel_backend: str = "process"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", Settings.database_url),
        cors_origins=_env_csv("CORS_ORIGINS", ("http://localhost:8080", "http://localhost:8081")),
        weather_refresh_on_startup=_env_bool("WEATHER_REFRESH_ON_STARTUP", True),
        sensor_source=os.getenv("SENSOR_SOURCE", DEFAULT_SENSOR_SOURCE),
        sensor_history_start=_env_date("SENSOR_HISTORY_START", date.today() - timedelta(days=29)),
        sensor_history_end=_env_date("SENSOR_HISTORY_END", date.today()) if os.getenv("SENSOR_HISTORY_END") else None,
        sensor_reading_interval_minutes=_env_int("SENSOR_READING_INTERVAL_MINUTES", 15),
        sensor_seed_history_on_startup=_env_bool("SENSOR_SEED_HISTORY_ON_STARTUP", True),
        sensor_scheduler_enabled=_env_bool("SENSOR_SCHEDULER_ENABLED", True),
        sensor_cleanup_enabled=_env_bool("SENSOR_CLEANUP_ENABLED", True),
        sensor_cleanup_time=_env_time("SENSOR_CLEANUP_TIME", time(3, 15)),
        prescription_scheduler_enabled=_env_bool("PRESCRIPTION_SCHEDULER_ENABLED", True),
        prescription_dispatch_time=_env_time("PRESCRIPTION_DISPATCH_TIME", time(21, 0)),
        actuation_scheduler_enabled=_env_bool("ACTUATION_SCHEDULER_ENABLED", True),
        actuation_poll_seconds=_env_int("ACTUATION_POLL_SECONDS", 60),
        experiment_snapshot_cache_ttl_seconds=_env_int("EXPERIMENT_SNAPSHOT_CACHE_TTL_SECONDS", 15 * 60),
        experiment_precompute_related=_env_bool("EXPERIMENT_PRECOMPUTE_RELATED", True),
        experiment_precompute_anfis=_env_bool("EXPERIMENT_PRECOMPUTE_ANFIS", True),
        default_anfis_parallel_workers=_env_int("DEFAULT_ANFIS_PARALLEL_WORKERS", _default_worker_count()),
        default_anfis_parallel_backend=os.getenv("DEFAULT_ANFIS_PARALLEL_BACKEND", "process"),
    )
