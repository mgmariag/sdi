from __future__ import annotations

from datetime import date, datetime
from typing import Any

from digital_twin.application.sensors.generation import (
    SensorReadingGenerator,
)
from digital_twin.application.sensors.ingestion import SensorReadingIngestionService
from digital_twin.application.sensors.maintenance import (
    SensorReadingMaintenanceService,
)
from digital_twin.application.sensors.availability import SensorAvailabilityService
from digital_twin.application.exceptions import InvalidDateRange
from digital_twin.domain.sensor import SensorSource
from digital_twin.infrastructure.database.repositories.sensors import SensorRepository


class SensorService:
    """Coordinates simulated sensor generation and sensor cache reads."""

    def __init__(
        self,
        repository: SensorRepository | None = None,
        availability_service: SensorAvailabilityService | None = None,
        maintenance_service: SensorReadingMaintenanceService | None = None,
        reading_generator: SensorReadingGenerator | None = None,
        ingestion_service: SensorReadingIngestionService | None = None,
    ) -> None:
        self.repository = repository or SensorRepository()
        cadence = (
            getattr(availability_service, "cadence", None)
            or getattr(maintenance_service, "cadence", None)
            or getattr(reading_generator, "cadence", None)
            or getattr(ingestion_service, "cadence", None)
        )
        self.availability_service = (
            availability_service
            or getattr(maintenance_service, "availability_service", None)
            or SensorAvailabilityService(cadence=cadence)
        )
        self.reading_generator = (
            reading_generator
            or getattr(maintenance_service, "generator", None)
            or SensorReadingGenerator(cadence=self.availability_service.cadence)
        )
        self.maintenance_service = maintenance_service or SensorReadingMaintenanceService(
            self.availability_service,
            generator=self.reading_generator,
        )
        self.ingestion_service = ingestion_service or SensorReadingIngestionService(
            cadence=self.availability_service.cadence
        )

    def summary(self, source: str | None = None) -> dict[str, Any]:
        summary = self.repository.summary(source=source)
        summary["retention"] = self.availability_service.cadence.retention_summary()
        return summary

    def seed_history(self, start_date: date, end_date: date, source: str) -> dict[str, Any]:
        if end_date < start_date:
            raise InvalidDateRange("end_date must not be before start_date")
        return self.reading_generator.seed_historical_sensor_readings(
            start_date=start_date,
            end_date=end_date,
            source=source,
        )

    def ensure_tiered_history(self, source: str, cleanup: bool = True) -> dict[str, Any]:
        return self.maintenance_service.ensure_tiered_sensor_readings(source=source, cleanup=cleanup)

    def seed_tiered_history(
        self,
        source: str,
        start_date: date | None = None,
        end_at: datetime | None = None,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        return self.reading_generator.seed_tiered_sensor_readings(
            end_at=end_at,
            start_date=start_date,
            source=source,
            replace_existing=replace_existing,
        )

    def ingest_actual(self, readings: list[dict[str, Any]], recorded_at: datetime | None = None) -> dict[str, Any]:
        return self.ingestion_service.ingest_actual_sensor_readings(
            readings=readings,
            recorded_at=recorded_at,
            source=SensorSource.ACTUAL.value,
        )

    def generate_due(self, source: str) -> list[dict[str, Any]]:
        return self.reading_generator.generate_due_sensor_readings(source=source)

    def generate_at(self, recorded_at: datetime, source: str) -> dict[str, Any]:
        return self.reading_generator.generate_sensor_readings_at(recorded_at=recorded_at, source=source)

    def cleanup(self, source: str | None = None) -> dict[str, Any]:
        return self.maintenance_service.aggregate_and_cleanup_sensor_readings(source=source)

    def has_data(self, source: str, pot_ids: list[int] | None = None) -> bool:
        return self.availability_service.get_sensor_availability(source=source, sensor_ids=pot_ids) is not None

