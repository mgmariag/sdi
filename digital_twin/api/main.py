from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from digital_twin.api.routes import experiments, sensors, weather
from digital_twin.core.config import get_settings
from digital_twin.db.schema import initialize_database
from digital_twin.services.sensor_service import SensorService


logger = logging.getLogger("digital_twin.api")


def initialize_api() -> None:
    try:
        initialize_database()
        logger.info("Database schema initialized and pot inventory seeded")
    except Exception as exc:
        logger.warning("Database initialization skipped: %s", exc)

    settings = get_settings()
    if settings.sensor_cleanup_enabled:
        try:
            SensorService().cleanup(source=settings.sensor_source)
            logger.info("Sensor aggregate cleanup completed")
        except Exception as exc:
            logger.warning("Sensor aggregate cleanup skipped: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_api()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Digital Twin Irrigation API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(sensors.system_router)
    app.include_router(sensors.api_router)
    app.include_router(weather.api_router)
    app.include_router(experiments.router)
    return app


app = create_app()

