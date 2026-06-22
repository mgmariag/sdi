from __future__ import annotations

import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

from digital_twin.infrastructure.config import get_settings


class ApplicationClock:
    """Application-facing source of local time."""

    def __init__(self, timezone_name: str | None = None) -> None:
        self.timezone_name = timezone_name

    def local_timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name or get_settings().local_timezone)

    def now(self) -> datetime:
        return datetime.now(self.local_timezone())

    def today(self) -> date:
        return self.now().date()

    @staticmethod
    def add_months(value: date, months: int) -> date:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
