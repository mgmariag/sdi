from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Query

from digital_twin.api.errors import http_error
from digital_twin.core.config import get_settings
from digital_twin.core.time import today_local
from digital_twin.services.sensor_placements import DEFAULT_SENSOR_COUNT, SensorPlacementService
from digital_twin.services.sensors import SensorService


api_router = APIRouter(prefix="/api/sensors")
service_router = APIRouter(prefix="/sensors")
service = SensorService()
placement_service = SensorPlacementService()


def _seed_sensor_history_if_placement_changed(result: dict) -> dict:
    settings = get_settings()
    sensor_pot_ids = [int(item["pot_id"]) for item in result.get("items", [])]
    has_sensor_data = service.has_data(source=settings.sensor_source, pot_ids=sensor_pot_ids)
    if not result.get("changed") and has_sensor_data:
        return result
    result["sensor_seed"] = service.seed_history(
        start_date=settings.sensor_history_start,
        end_date=settings.sensor_history_end or today_local(),
        source=settings.sensor_source,
    )
    if settings.sensor_cleanup_enabled:
        result["sensor_cleanup"] = service.cleanup(source=settings.sensor_source)
    return result


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
