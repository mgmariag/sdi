from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from digital_twin.api.routes import control_loop, experiments, sensors, weather
from digital_twin.application.experiments.experiment_service import (
    warm_default_baseline_cache,
)
from digital_twin.application.sensor_history.sensor_history_service import SensorService
from digital_twin.application.weather_refresh.weather_refresh_service import (
    WeatherService,
)
from digital_twin.core.config import get_settings
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.infrastructure.schedulers.actuation import ActuationScheduler
from digital_twin.infrastructure.schedulers.prescriptions import PrescriptionScheduler

logger = logging.getLogger("digital_twin.api")
_prescription_scheduler: PrescriptionScheduler | None = None
_actuation_scheduler: ActuationScheduler | None = None


def initialize_api() -> None:
    try:
        initialize_database()
        logger.info("Database schema initialized and pot inventory seeded")
    except Exception as exc:
        logger.warning("Database initialization skipped: %s", exc)

    settings = get_settings()
    if settings.weather_refresh_on_startup:
        _refresh_weather_on_startup()
    _prepare_sensors_on_startup()
    if settings.prescription_scheduler_enabled:
        _start_prescription_scheduler()
    if settings.actuation_scheduler_enabled:
        _start_actuation_scheduler()

    try:
        baseline = warm_default_baseline_cache()
        logger.info(
            "Baseline experiment cache warm-up %s for %s to %s",
            baseline["status"],
            baseline["start"],
            baseline["end"],
        )
    except Exception as exc:
        logger.warning("Baseline experiment cache warm-up skipped: %s", exc)


def _prepare_sensors_on_startup() -> None:
    settings = get_settings()
    service = SensorService()
    if settings.sensor_seed_history_on_startup:
        try:
            result = service.ensure_tiered_history(
                source=settings.sensor_source,
                cleanup=settings.sensor_cleanup_enabled,
            )
            logger.info(
                "Sensor tiered history ready: coverage=%s seed=%s cleanup=%s",
                result.get("coverage"),
                bool(result.get("seed")),
                bool(result.get("cleanup")),
            )
        except Exception as exc:
            logger.warning("Sensor tiered history seeding skipped: %s", exc)
        return

    if settings.sensor_cleanup_enabled:
        try:
            service.cleanup(source=settings.sensor_source)
            logger.info("Sensor aggregate cleanup completed")
        except Exception as exc:
            logger.warning("Sensor aggregate cleanup skipped: %s", exc)


def _refresh_weather_on_startup() -> None:
    try:
        result = WeatherService().refresh_forecast()
        if result.get("already_refreshed"):
            logger.info("Weather refresh skipped; forecast already refreshed for today")
            return
        logger.info(
            "Weather refresh completed from %s to %s: inserted=%s updated=%s unchanged=%s",
            result.get("refresh_start"),
            result.get("forecast_end"),
            result.get("inserted_count"),
            result.get("updated_count"),
            result.get("unchanged_count"),
        )
    except Exception as exc:
        logger.warning("Weather refresh on startup skipped: %s", exc)


def _start_prescription_scheduler() -> None:
    global _prescription_scheduler
    if _prescription_scheduler is None:
        _prescription_scheduler = PrescriptionScheduler()
    _prescription_scheduler.start()
    logger.info("Prescription scheduler started")


def _start_actuation_scheduler() -> None:
    global _actuation_scheduler
    if _actuation_scheduler is None:
        _actuation_scheduler = ActuationScheduler()
    _actuation_scheduler.start()
    logger.info("Actuation scheduler started")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_api()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Smart Irrigation", lifespan=lifespan)
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
    app.include_router(control_loop.router)
    return app


app = create_app()

