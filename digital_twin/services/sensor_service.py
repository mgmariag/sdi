from __future__ import annotations

from datetime import date, datetime
from typing import Any

from digital_twin.core.exceptions import InvalidDateRange
from digital_twin.db.repositories.sensor_repository import SensorRepository
from digital_twin.services.sensor_readings import (
    ACTUAL_SENSOR_SOURCE,
    aggregate_and_cleanup_sensor_readings,
    generate_due_sensor_readings,
    generate_sensor_readings_at,
    get_sensor_availability,
    ensure_tiered_sensor_readings,
    ingest_actual_sensor_readings,
    seed_historical_sensor_readings,
)


class SensorService:
    """Coordinates simulated sensor generation and sensor cache reads."""

    def __init__(self, repository: SensorRepository | None = None) -> None:
        self.repository = repository or SensorRepository()

    def summary(self, source: str | None = None) -> dict[str, Any]:
        return self.repository.summary(source=source)

    def seed_history(self, start_date: date, end_date: date, source: str) -> dict[str, Any]:
        if end_date < start_date:
            raise InvalidDateRange("end_date must not be before start_date")
        return seed_historical_sensor_readings(start_date=start_date, end_date=end_date, source=source)

    def ensure_tiered_history(self, source: str, cleanup: bool = True) -> dict[str, Any]:
        return ensure_tiered_sensor_readings(source=source, cleanup=cleanup)

    def ingest_actual(self, readings: list[dict[str, Any]], recorded_at: datetime | None = None) -> dict[str, Any]:
        return ingest_actual_sensor_readings(readings=readings, recorded_at=recorded_at, source=ACTUAL_SENSOR_SOURCE)

    def generate_due(self, source: str) -> list[dict[str, Any]]:
        return generate_due_sensor_readings(source=source)

    def generate_at(self, recorded_at: datetime, source: str) -> dict[str, Any]:
        return generate_sensor_readings_at(recorded_at=recorded_at, source=source)

    def cleanup(self, source: str | None = None) -> dict[str, Any]:
        return aggregate_and_cleanup_sensor_readings(source=source)

    def has_data(self, source: str, pot_ids: list[int] | None = None) -> bool:
        return get_sensor_availability(source=source, sensor_ids=pot_ids) is not None

