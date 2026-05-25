from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from digital_twin.core.config import get_settings


def local_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().local_timezone)


def now_local() -> datetime:
    return datetime.now(local_timezone())


def today_local() -> date:
    return now_local().date()

