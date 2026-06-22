from __future__ import annotations

from enum import Enum


class SensorSource(str, Enum):
    DEFAULT = "simulated_sensor"
    ACTUAL = "actual_sensor"
    ACTUATOR_FEEDBACK = "actuator_feedback"
    SPARSE_FORECAST = "forecast_simulated_sensor"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(item.value for item in cls)

    @classmethod
    def query_values(cls, source: str | None) -> list[str]:
        if not source or source == cls.DEFAULT.value:
            return [
                cls.DEFAULT.value,
                cls.ACTUAL.value,
                cls.ACTUATOR_FEEDBACK.value,
            ]
        return [source]


class SensorReadingResolution(str, Enum):
    RAW = "raw_15min"
    HOURLY = "hourly"
    DAILY = "daily"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(item.value for item in cls)

    @classmethod
    def query_values(cls) -> list[str]:
        return list(cls.values())
