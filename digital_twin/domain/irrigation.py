from __future__ import annotations

from enum import Enum


class IrrigationSlot(str, Enum):
    MORNING = "morning"
    EVENING = "evening"
    WINTER_CHECK = "winter_check"
    DAILY_PRESCRIPTION = "daily_prescription"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(item.value for item in cls)


class IrrigationStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @classmethod
    def event_values(cls) -> tuple[str, ...]:
        return (
            cls.PLANNED.value,
            cls.RUNNING.value,
            cls.COMPLETED.value,
            cls.SKIPPED.value,
            cls.CANCELLED.value,
        )

    @classmethod
    def actuation_values(cls) -> tuple[str, ...]:
        return (
            *cls.event_values(),
            cls.FAILED.value,
        )
