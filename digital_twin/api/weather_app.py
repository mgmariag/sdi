from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException

from digital_twin.api.routes.weather import service_router
from digital_twin.core.config import get_settings
from digital_twin.db.pot_repository import PotRepository
from digital_twin.db.schema import initialize_database
from digital_twin.services.weather import WeatherService


logger = logging.getLogger("digital_twin.weather_service")


def initialize_weather_service() -> None:
    initialize_database()
    if not get_settings().weather_refresh_on_startup:
        return
    try:
        result = WeatherService().refresh_forecast()
        logger.info("Weather forecast refresh result: %s", result)
    except Exception as exc:
        logger.warning("Weather forecast refresh failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_weather_service()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Open-Meteo Ingestion Service", lifespan=lifespan)
    app.include_router(service_router)

    @app.get("/health")
    def health():
        try:
            return PotRepository().health()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    return app


app = create_app()

