from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from digital_twin.api.errors import ApiErrorMapper
from digital_twin.application.clock import ApplicationClock
from digital_twin.application.sensors.service import SensorService
from digital_twin.application.sensors.placement import (
    DEFAULT_SENSOR_COUNT,
    MIN_SENSOR_COUNT,
    SensorPlacementService,
)
from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.repositories.overview.current import (
    OverviewRepository,
)
from digital_twin.infrastructure.database.repositories.pots import PotRepository


class SensorReadingIngestItem(BaseModel):
    sensor_id: int | None = Field(default=None, ge=1)
    pot_id: int | None = Field(default=None, ge=1)
    recorded_at: datetime | None = None
    soil_moisture_pct: float = Field(ge=0, le=100)
    air_temperature_c: float | None = None
    air_humidity_pct: float | None = Field(default=None, ge=0, le=100)
    substrate_temperature_c: float | None = None


class SensorReadingIngestRequest(BaseModel):
    recorded_at: datetime | None = None
    readings: list[SensorReadingIngestItem]

    def as_tool_readings(self) -> list[dict[str, Any]]:
        return [item.dict(exclude_none=True) for item in self.readings]


class SystemRoutes:
    def __init__(
        self,
        overview_repository: OverviewRepository | None = None,
        pot_repository: PotRepository | None = None,
        clock: ApplicationClock | None = None,
        error_mapper: ApiErrorMapper | None = None,
    ) -> None:
        self.router = APIRouter()
        self.clock = clock or ApplicationClock()
        self.overview_repository = overview_repository or OverviewRepository(clock=self.clock)
        self.pot_repository = pot_repository or PotRepository()
        self.error_mapper = error_mapper or ApiErrorMapper()
        self.router.add_api_route("/", self.root, methods=["GET"])
        self.router.add_api_route("/api/hello", self.hello, methods=["GET"])
        self.router.add_api_route("/api/db/health", self.database_health, methods=["GET"])
        self.router.add_api_route("/api/overview", self.overview, methods=["GET"])
        self.router.add_api_route("/api/pots/summary", self.pots_summary, methods=["GET"])
        self.router.add_api_route("/api/pots", self.pots, methods=["GET"])

    def root(self) -> dict[str, str]:
        return {"message": "Smart Irrigation API running"}

    def hello(self) -> dict[str, str]:
        return {"message": "Select an experiment to begin"}

    def database_health(self):
        try:
            return self.pot_repository.health()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Database unavailable") from exc

    def overview(self):
        try:
            return self.overview_repository.current()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Overview unavailable") from exc

    def pots_summary(self):
        try:
            return self.pot_repository.summary()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Database unavailable") from exc

    def pots(
        self,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        size_class: str | None = Query(None),
        plant_type: str | None = Query(None),
    ):
        try:
            return {
                "items": self.pot_repository.list(
                    limit=limit,
                    offset=offset,
                    size_class=size_class,
                    plant_type=plant_type,
                )
            }
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Database unavailable") from exc


class SensorRoutes:
    def __init__(
        self,
        service: SensorService | None = None,
        placement_service: SensorPlacementService | None = None,
        clock: ApplicationClock | None = None,
        error_mapper: ApiErrorMapper | None = None,
    ) -> None:
        self.router = APIRouter(prefix="/api/sensors")
        self.service = service or SensorService()
        self.placement_service = placement_service or SensorPlacementService()
        self.clock = clock or ApplicationClock()
        self.error_mapper = error_mapper or ApiErrorMapper()
        self.router.add_api_route("/summary", self.summary, methods=["GET"])
        self.router.add_api_route("/cleanup", self.cleanup, methods=["POST"])
        self.router.add_api_route("/ingest", self.ingest_actual_sensor_readings, methods=["POST"])
        self.router.add_api_route("/placements", self.sensor_placements, methods=["GET"])
        self.router.add_api_route("/placements/recommend", self.recommend_sensor_placements, methods=["POST"])
        self.router.add_api_route("/placements/ensure", self.ensure_sensor_placements, methods=["POST"])
        self.router.add_api_route("/seed", self.seed_sensors, methods=["POST"])
        self.router.add_api_route("/run-due", self.run_due_sensor_readings, methods=["POST"])
        self.router.add_api_route("/run-at", self.run_sensor_readings_at, methods=["POST"])

    def summary(self, source: str | None = Query(get_settings().sensor_source)):
        try:
            return self.service.summary(source=source)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Sensor readings unavailable") from exc

    def cleanup(self, source: str | None = Query(get_settings().sensor_source)):
        try:
            return self.service.cleanup(source=source)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sensor cleanup failed") from exc

    def ingest_actual_sensor_readings(self, payload: SensorReadingIngestRequest):
        try:
            return self.service.ingest_actual(payload.as_tool_readings(), recorded_at=payload.recorded_at)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 400, "Sensor ingestion failed") from exc

    def sensor_placements(self):
        try:
            return self.placement_service.current()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Sensor placements unavailable") from exc

    def recommend_sensor_placements(self, count: int = Query(DEFAULT_SENSOR_COUNT, ge=MIN_SENSOR_COUNT, le=500)):
        try:
            return self._seed_sensors_if_placement_changed(self.placement_service.recommend(sensor_count=count))
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sensor placement recommendation failed") from exc

    def ensure_sensor_placements(self, count: int = Query(DEFAULT_SENSOR_COUNT, ge=MIN_SENSOR_COUNT, le=500)):
        try:
            return self._seed_sensors_if_placement_changed(self.placement_service.ensure(sensor_count=count))
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sensor placement synchronization failed") from exc

    def seed_sensors(
        self,
        start: date = Query(get_settings().sensor_history_start),
        end: date | None = Query(None),
        source: str = Query(get_settings().sensor_source),
    ):
        try:
            result = self.service.seed_history(start_date=start, end_date=end or self.clock.today(), source=source)
            if get_settings().sensor_cleanup_enabled:
                result["sensor_cleanup"] = self.service.cleanup(source=source)
            return result
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sensor seeding failed") from exc

    def run_due_sensor_readings(self, source: str = Query(get_settings().sensor_source)):
        try:
            return {"items": self.service.generate_due(source=source)}
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sensor generation failed") from exc

    def run_sensor_readings_at(
        self,
        recorded_at: datetime = Query(...),
        source: str = Query(get_settings().sensor_source),
    ):
        try:
            return self.service.generate_at(recorded_at, source=source)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Sensor generation failed") from exc

    def _seed_sensors_if_placement_changed(self, result: dict) -> dict:
        settings = get_settings()
        if result.get("changed"):
            try:
                result["sensor_seed"] = self.service.ensure_tiered_history(
                    source=settings.sensor_source,
                    cleanup=settings.sensor_cleanup_enabled,
                )
            except Exception as exc:
                result["sensor_seed"] = {
                    "status": "skipped",
                    "reason": str(exc),
                }
        return result

