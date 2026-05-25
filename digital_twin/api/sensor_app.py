from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException

from digital_twin.api.routes.sensors import service_router
from digital_twin.core.config import get_settings
from digital_twin.core.time import today_local
from digital_twin.db.pot_repository import PotRepository
from digital_twin.db.schema import initialize_database
from digital_twin.services.sensor_placements import SensorPlacementService
from digital_twin.services.sensors import SensorService
from digital_twin.workers.sensor_scheduler import SensorScheduler


logger = logging.getLogger("digital_twin.sensor_service")
_scheduler = SensorScheduler()


def initialize_sensor_service() -> None:
    settings = get_settings()
    initialize_database()
    sensor_service = SensorService()

    if settings.sensor_seed_history_on_startup:
        placement = SensorPlacementService().ensure_default_if_missing()
        logger.info("Sensor placement ready: %s sensors", placement.get("sensor_count", 0))
        sensor_pot_ids = [int(item["pot_id"]) for item in placement.get("items", [])]
        should_seed = bool(placement.get("changed")) or not sensor_service.has_data(
            source=settings.sensor_source,
            pot_ids=sensor_pot_ids,
        )
        if should_seed:
            end_date = settings.sensor_history_end or today_local()
            summary = sensor_service.seed_history(
                start_date=settings.sensor_history_start,
                end_date=end_date,
                source=settings.sensor_source,
            )
            logger.info("Historical sensor seed completed: %s", summary)
        else:
            logger.info("Existing sensor readings found; startup seed skipped")

    due = sensor_service.generate_due(source=settings.sensor_source)
    if due:
        logger.info("Generated due sensor readings: %s", due)

    if settings.sensor_cleanup_enabled:
        cleanup = sensor_service.cleanup(source=settings.sensor_source)
        logger.info("Sensor aggregate cleanup completed: %s", cleanup)

    if settings.sensor_scheduler_enabled:
        _scheduler.start(settings.sensor_source)
    elif settings.sensor_cleanup_enabled:
        _scheduler.start_cleanup(settings.sensor_source)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_sensor_service()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Sensor Ingestion Service", lifespan=lifespan)
    app.include_router(service_router)

    @app.get("/health")
    def health():
        try:
            return PotRepository().health()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    return app


app = create_app()
