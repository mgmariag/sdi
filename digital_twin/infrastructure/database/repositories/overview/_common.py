from __future__ import annotations

from datetime import datetime
from typing import Any

from digital_twin.domain.valves import VALVE_COUNT

SAFE_TAP_FLOW_L_MIN = 2.0
VALVE_SWITCH_PAUSE_MIN = 1.0
PHYSICAL_ACTUATION_EXPERIMENT_TYPE = "baseline"
NO_IRRIGATION_PLANNED_LABEL = "No irrigation planned"
NO_IRRIGATION_RECORDED_LABEL = "No irrigation recorded"


def window_minutes(next_window: dict[str, Any] | None) -> float | None:
    if not next_window or not next_window.get("start_at") or not next_window.get("end_at"):
        return None
    try:
        start = datetime.fromisoformat(str(next_window["start_at"]))
        end = datetime.fromisoformat(str(next_window["end_at"]))
    except ValueError:
        return None
    return max(0.0, (end - start).total_seconds() / 60.0)


def freshness_percent(latest_at: datetime | None, now: datetime) -> int:
    if latest_at is None:
        return 0
    age_minutes = max(0.0, (now - latest_at).total_seconds() / 60.0)
    if age_minutes <= 30:
        return 98
    if age_minutes <= 120:
        return 95
    if age_minutes <= 24 * 60:
        return 82
    if age_minutes <= 7 * 24 * 60:
        return 68
    return 45


def confidence_score(freshness_percent: int, sensor_count: int, weather_rows: int) -> float:
    sensor_factor = min(1.0, sensor_count / max(VALVE_COUNT, 1))
    weather_factor = min(1.0, weather_rows / 72.0)
    freshness_factor = max(0.0, min(1.0, freshness_percent / 100.0))
    return round(0.2 + 0.45 * freshness_factor + 0.25 * sensor_factor + 0.1 * weather_factor, 2)


def rain_level(rain_mm: float) -> str:
    if rain_mm >= 30.0:
        return "High"
    if rain_mm >= 12.0:
        return "Moderate"
    return "Low"


def number(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)
