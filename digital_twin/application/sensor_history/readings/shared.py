from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from digital_twin.application.sensor_placement.sensor_placement_service import (
    SensorPlacementService,
)
from digital_twin.core.config import get_settings
from digital_twin.domain.sensors import (
    ACTUAL_SENSOR_SOURCE,
    ACTUATOR_FEEDBACK_SOURCE,
    DEFAULT_SENSOR_SOURCE,
)
from digital_twin.domain.weather import (
    DEFAULT_LOCAL_TIMEZONE,
    DEFAULT_WEATHER_LOCATION_NAME,
)

LOCATION_NAME = DEFAULT_WEATHER_LOCATION_NAME
LOCAL_TZ = ZoneInfo(DEFAULT_LOCAL_TIMEZONE)
DEFAULT_HISTORY_START = date(2025, 5, 22)
RAW_RETENTION_HOURS = 24
HOURLY_RETENTION_DAYS = 7
DAILY_RETENTION_DAYS = 366
DAILY_READING_TIMES = (time(1, 0), time(5, 30), time(14, 0), time(17, 30))
DAILY_READING_SLOTS_PER_DAY = len(DAILY_READING_TIMES)


def query_sources(source: str | None) -> list[str]:
    if source == DEFAULT_SENSOR_SOURCE:
        return [DEFAULT_SENSOR_SOURCE, ACTUAL_SENSOR_SOURCE, ACTUATOR_FEEDBACK_SOURCE]
    if source:
        return [source]
    return [DEFAULT_SENSOR_SOURCE, ACTUAL_SENSOR_SOURCE, ACTUATOR_FEEDBACK_SOURCE]


def sensor_equipped_pot_ids(candidate_pot_ids: list[int] | None = None) -> list[int]:
    return SensorPlacementService().selected_pot_ids(candidate_pot_ids)


def tiered_periods(end_at: datetime) -> dict[str, datetime]:
    end_at = align_to_reading_interval(as_local(end_at))
    raw_start = datetime.combine(end_at.date(), time(0, 0), tzinfo=LOCAL_TZ)
    hourly_start = raw_start - timedelta(days=HOURLY_RETENTION_DAYS)
    daily_start = raw_start - timedelta(days=DAILY_RETENTION_DAYS)
    return {
        "daily_start": daily_start,
        "hourly_start": hourly_start,
        "raw_start": raw_start,
        "end_at": end_at,
    }


def slot_count(start_at: datetime, end_at: datetime, minutes: int) -> int:
    if end_at < start_at:
        return 0
    return int((end_at - start_at).total_seconds() // (minutes * 60)) + 1


def scheduled_datetimes(day: date) -> list[datetime]:
    return [datetime.combine(day, slot, tzinfo=LOCAL_TZ) for slot in DAILY_READING_TIMES]


def next_scheduled_datetime(now: datetime) -> datetime:
    now = as_local(now)
    for candidate in scheduled_datetimes(now.date()):
        if candidate > now:
            return candidate
    return scheduled_datetimes(now.date() + timedelta(days=1))[0]


def reading_interval_minutes() -> int:
    return max(1, min(24 * 60, get_settings().sensor_reading_interval_minutes))


def align_to_reading_interval(value: datetime) -> datetime:
    return align_to_interval(value, reading_interval_minutes())


def align_to_interval(value: datetime, interval: int) -> datetime:
    value = as_local(value).replace(second=0, microsecond=0)
    minutes_since_midnight = value.hour * 60 + value.minute
    aligned_minutes = (minutes_since_midnight // interval) * interval
    return datetime.combine(value.date(), time(0, 0), tzinfo=LOCAL_TZ) + timedelta(minutes=aligned_minutes)


def ceil_to_interval(value: datetime, interval: int) -> datetime:
    value = as_local(value).replace(second=0, microsecond=0)
    minutes_since_midnight = value.hour * 60 + value.minute
    aligned_minutes = ((minutes_since_midnight + interval - 1) // interval) * interval
    return datetime.combine(value.date(), time(0, 0), tzinfo=LOCAL_TZ) + timedelta(minutes=aligned_minutes)


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def db_timestamp(value: datetime) -> datetime:
    return as_local(value).replace(tzinfo=None)


def same_month_day(year: int, source_date: date) -> date:
    try:
        return date(year, source_date.month, source_date.day)
    except ValueError:
        return date(year, 2, 28)
