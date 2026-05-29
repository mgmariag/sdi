from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from digital_twin.api.errors import http_error
from digital_twin.core.config import get_settings
from digital_twin.core.time import today_local
from digital_twin.db.repositories.sensor_repository import OverviewRepository, PotRepository
from digital_twin.services.sensor_placements import DEFAULT_SENSOR_COUNT, SensorPlacementService
from digital_twin.services.sensor_service import SensorService


system_router = APIRouter()
api_router = APIRouter(prefix="/api/sensors")
service_router = APIRouter(prefix="/sensors")
service = SensorService()
placement_service = SensorPlacementService()
overview_repository = OverviewRepository()
pot_repository = PotRepository()


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


def _seed_sensor_history_if_placement_changed(result: dict) -> dict:
    settings = get_settings()
    if result.get("changed"):
        try:
            result["sensor_seed"] = service.ensure_tiered_history(
                source=settings.sensor_source,
                cleanup=settings.sensor_cleanup_enabled,
            )
        except Exception as exc:
            result["sensor_seed"] = {
                "status": "skipped",
                "reason": str(exc),
            }
    return result


@system_router.get("/")
def root() -> dict[str, str]:
    return {"message": "Digital Twin Irrigation API running"}


@system_router.get("/api/hello")
def hello() -> dict[str, str]:
    return {"message": "Select an experiment to begin"}


@system_router.get("/api/db/health")
def database_health():
    try:
        return pot_repository.health()
    except Exception as exc:
        raise http_error(exc, 503, "Database unavailable") from exc


@system_router.get("/api/overview")
def overview():
    try:
        return overview_repository.current()
    except Exception as exc:
        raise http_error(exc, 503, "Overview unavailable") from exc


@system_router.get("/api/pots/summary")
def pots_summary():
    try:
        return pot_repository.summary()
    except Exception as exc:
        raise http_error(exc, 503, "Database unavailable") from exc


@system_router.get("/api/pots")
def pots(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    size_class: str | None = Query(None),
    plant_type: str | None = Query(None),
):
    try:
        return {
            "items": pot_repository.list(
                limit=limit,
                offset=offset,
                size_class=size_class,
                plant_type=plant_type,
            )
        }
    except Exception as exc:
        raise http_error(exc, 503, "Database unavailable") from exc


@api_router.get("/summary")
def api_sensors_summary(source: str | None = Query(get_settings().sensor_source)):
    try:
        return service.summary(source=source)
    except Exception as exc:
        raise http_error(exc, 503, "Sensor readings unavailable") from exc


@api_router.post("/cleanup")
def api_cleanup_sensors(source: str | None = Query(get_settings().sensor_source)):
    try:
        return service.cleanup(source=source)
    except Exception as exc:
        raise http_error(exc, 500, "Sensor cleanup failed") from exc


@api_router.post("/ingest")
def api_ingest_actual_sensor_readings(payload: SensorReadingIngestRequest):
    try:
        return service.ingest_actual(payload.as_tool_readings(), recorded_at=payload.recorded_at)
    except Exception as exc:
        raise http_error(exc, 400, "Sensor ingestion failed") from exc


@api_router.get("/placements")
def api_sensor_placements():
    try:
        return placement_service.current()
    except Exception as exc:
        raise http_error(exc, 503, "Sensor placements unavailable") from exc


@api_router.post("/placements/recommend")
def api_recommend_sensor_placements(count: int = Query(DEFAULT_SENSOR_COUNT, ge=1, le=500)):
    try:
        return _seed_sensor_history_if_placement_changed(placement_service.recommend(sensor_count=count))
    except Exception as exc:
        raise http_error(exc, 500, "Sensor placement recommendation failed") from exc


@api_router.post("/placements/ensure")
def api_ensure_sensor_placements(count: int = Query(DEFAULT_SENSOR_COUNT, ge=1, le=500)):
    try:
        return _seed_sensor_history_if_placement_changed(placement_service.ensure(sensor_count=count))
    except Exception as exc:
        raise http_error(exc, 500, "Sensor placement synchronization failed") from exc


@service_router.get("/summary")
def service_sensors_summary(source: str | None = Query(get_settings().sensor_source)):
    try:
        return service.summary(source=source)
    except Exception as exc:
        raise http_error(exc, 503, "Sensor readings unavailable") from exc


@service_router.post("/cleanup")
def service_cleanup_sensors(source: str | None = Query(get_settings().sensor_source)):
    try:
        return service.cleanup(source=source)
    except Exception as exc:
        raise http_error(exc, 500, "Sensor cleanup failed") from exc


@service_router.post("/ingest")
def service_ingest_actual_sensor_readings(payload: SensorReadingIngestRequest):
    try:
        return service.ingest_actual(payload.as_tool_readings(), recorded_at=payload.recorded_at)
    except Exception as exc:
        raise http_error(exc, 400, "Sensor ingestion failed") from exc


@service_router.get("/placements")
def service_sensor_placements():
    try:
        return placement_service.current()
    except Exception as exc:
        raise http_error(exc, 503, "Sensor placements unavailable") from exc


@service_router.post("/placements/recommend")
def service_recommend_sensor_placements(count: int = Query(DEFAULT_SENSOR_COUNT, ge=1, le=500)):
    try:
        return _seed_sensor_history_if_placement_changed(placement_service.recommend(sensor_count=count))
    except Exception as exc:
        raise http_error(exc, 500, "Sensor placement recommendation failed") from exc


@service_router.post("/placements/ensure")
def service_ensure_sensor_placements(count: int = Query(DEFAULT_SENSOR_COUNT, ge=1, le=500)):
    try:
        return _seed_sensor_history_if_placement_changed(placement_service.ensure(sensor_count=count))
    except Exception as exc:
        raise http_error(exc, 500, "Sensor placement synchronization failed") from exc


@service_router.post("/seed")
def seed_sensors(
    start: date = Query(get_settings().sensor_history_start),
    end: date | None = Query(None),
    source: str = Query(get_settings().sensor_source),
):
    try:
        result = service.seed_history(start_date=start, end_date=end or today_local(), source=source)
        if get_settings().sensor_cleanup_enabled:
            result["sensor_cleanup"] = service.cleanup(source=source)
        return result
    except Exception as exc:
        raise http_error(exc, 500, "Sensor seeding failed") from exc


@service_router.post("/run-due")
def run_due_sensor_readings(source: str = Query(get_settings().sensor_source)):
    try:
        return {"items": service.generate_due(source=source)}
    except Exception as exc:
        raise http_error(exc, 500, "Sensor generation failed") from exc


@service_router.post("/run-at")
def run_sensor_readings_at(
    recorded_at: datetime = Query(...),
    source: str = Query(get_settings().sensor_source),
):
    try:
        return service.generate_at(recorded_at, source=source)
    except Exception as exc:
        raise http_error(exc, 500, "Sensor generation failed") from exc

