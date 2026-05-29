from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


LOCATION_NAME = "Cluj-Napoca"
LOCAL_TZ = ZoneInfo("Europe/Bucharest")
ANFIS_DECISION_THRESHOLD = 0.6
HOURLY_CHART_MAX_RANGE_DAYS = 7


@dataclass
class PotState:
    moisture: float
    too_wet_hours: int = 0


@dataclass
class ExperimentSnapshot:
    start_date: date
    end_date: date
    pot_count: int
    pots: list[dict[str, Any]]
    weather_rows: list[dict[str, Any]]
    selected_weather_rows: list[dict[str, Any]]
    weather_by_day: dict[date, list[dict[str, Any]]]
    day_profiles: dict[date, dict[str, Any]]
    sensor_context: dict[str, Any]
    initial_pot_states: dict[int, PotState]
    estimated_weather_rows: int
    loaded_at: datetime
