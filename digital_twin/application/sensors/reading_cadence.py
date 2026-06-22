"""Shared sensor reading cadence and timestamp helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from digital_twin.infrastructure.config import get_settings
from digital_twin.domain.weather import DEFAULT_WEATHER_LOCATION

LOCATION_NAME = DEFAULT_WEATHER_LOCATION.name
LOCAL_TZ = DEFAULT_WEATHER_LOCATION.timezone_info()
DEFAULT_HISTORY_START = date(2025, 5, 22)
ACTUAL_READING_INTERVAL_MINUTES = 15
RAW_RETENTION_HOURS = 24
HOURLY_RETENTION_DAYS = 7
DAILY_RETENTION_DAYS = 366
DAILY_READING_TIMES = (time(1, 0), time(5, 30), time(14, 0), time(17, 30))
DAILY_READING_SLOTS_PER_DAY = len(DAILY_READING_TIMES)


class SensorReadingCadence:
    """Owns sensor reading cadence, local-time alignment, and retention windows."""

    def __init__(
        self,
        local_tz=LOCAL_TZ,
        daily_reading_times: tuple[time, ...] = DAILY_READING_TIMES,
    ) -> None:
        self.local_tz = local_tz
        self.daily_reading_times = daily_reading_times

    @property
    def daily_reading_slots_per_day(self) -> int:
        return len(self.daily_reading_times)

    def tiered_periods(self, end_at: datetime) -> dict[str, datetime]:
        end_at = self.align_to_reading_interval(self.as_local(end_at))
        raw_start = datetime.combine(end_at.date(), time(0, 0), tzinfo=self.local_tz)
        hourly_start = raw_start - timedelta(days=HOURLY_RETENTION_DAYS)
        daily_start = raw_start - timedelta(days=DAILY_RETENTION_DAYS)
        return {
            "daily_start": daily_start,
            "hourly_start": hourly_start,
            "raw_start": raw_start,
            "end_at": end_at,
        }

    def slot_count(self, start_at: datetime, end_at: datetime, minutes: int) -> int:
        if end_at < start_at:
            return 0
        return int((end_at - start_at).total_seconds() // (minutes * 60)) + 1

    def scheduled_datetimes(self, day: date) -> list[datetime]:
        return [datetime.combine(day, slot, tzinfo=self.local_tz) for slot in self.daily_reading_times]

    def next_scheduled_datetime(self, now: datetime) -> datetime:
        now = self.as_local(now)
        for candidate in self.scheduled_datetimes(now.date()):
            if candidate > now:
                return candidate
        return self.scheduled_datetimes(now.date() + timedelta(days=1))[0]

    def reading_interval_minutes(self) -> int:
        return max(1, min(24 * 60, get_settings().sensor_reading_interval_minutes))

    def retention_summary(self) -> dict[str, object]:
        return {
            "raw_hours": RAW_RETENTION_HOURS,
            "hourly_days": HOURLY_RETENTION_DAYS,
            "daily_days": DAILY_RETENTION_DAYS,
            "daily_reading_times": [slot.strftime("%H:%M") for slot in self.daily_reading_times],
            "reading_interval_minutes": self.reading_interval_minutes(),
        }

    def align_to_reading_interval(self, value: datetime) -> datetime:
        return self.align_to_interval(value, self.reading_interval_minutes())

    def align_to_interval(self, value: datetime, interval: int) -> datetime:
        value = self.as_local(value).replace(second=0, microsecond=0)
        minutes_since_midnight = value.hour * 60 + value.minute
        aligned_minutes = (minutes_since_midnight // interval) * interval
        return datetime.combine(value.date(), time(0, 0), tzinfo=self.local_tz) + timedelta(minutes=aligned_minutes)

    def ceil_to_interval(self, value: datetime, interval: int) -> datetime:
        value = self.as_local(value).replace(second=0, microsecond=0)
        minutes_since_midnight = value.hour * 60 + value.minute
        aligned_minutes = ((minutes_since_midnight + interval - 1) // interval) * interval
        return datetime.combine(value.date(), time(0, 0), tzinfo=self.local_tz) + timedelta(minutes=aligned_minutes)

    def today_local(self) -> date:
        return datetime.now(self.local_tz).date()

    def as_local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self.local_tz)
        return value.astimezone(self.local_tz)

    def db_timestamp(self, value: datetime) -> datetime:
        return self.as_local(value).replace(tzinfo=None)

    @staticmethod
    def same_month_day(year: int, source_date: date) -> date:
        try:
            return date(year, source_date.month, source_date.day)
        except ValueError:
            return date(year, 2, 28)


DEFAULT_SENSOR_READING_CADENCE = SensorReadingCadence()
