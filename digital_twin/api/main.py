from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from digital_twin.api import actuation_route, experiment_route, sensor_route, weather_route
from digital_twin.api.errors import ApiErrorMapper
from digital_twin.application.clock import ApplicationClock
from digital_twin.application.experiments.service import ExperimentService
from digital_twin.application.sensors.service import SensorService
from digital_twin.application.weather_refresh.service import (
    WeatherRefreshService,
)
from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.infrastructure.schedulers.actuation import ActuationScheduler
from digital_twin.infrastructure.schedulers.prescriptions import PrescriptionScheduler

logger = logging.getLogger("digital_twin.api")


class WebApiApplication:
    """Composes the FastAPI app and owns startup lifecycle state."""

    def __init__(self) -> None:
        self.clock = ApplicationClock()
        self.error_mapper = ApiErrorMapper()
        self.experiment_service = ExperimentService(clock=self.clock)
        self.prescription_scheduler: PrescriptionScheduler | None = None
        self.actuation_scheduler: ActuationScheduler | None = None

    def create_app(self) -> FastAPI:
        settings = get_settings()
        app = FastAPI(title="Smart Irrigation", lifespan=self.lifespan)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.register_routes(app)
        return app

    def register_routes(self, app: FastAPI) -> None:
        app.include_router(sensor_route.SystemRoutes(clock=self.clock, error_mapper=self.error_mapper).router)
        app.include_router(sensor_route.SensorRoutes(clock=self.clock, error_mapper=self.error_mapper).router)
        app.include_router(weather_route.WeatherRoutes(error_mapper=self.error_mapper).router)
        app.include_router(
            experiment_route.ExperimentRoutes(
                service=self.experiment_service,
                error_mapper=self.error_mapper,
            ).router
        )
        app.include_router(actuation_route.ActuationRoutes(error_mapper=self.error_mapper).router)

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        self.initialize()
        yield

    def initialize(self) -> None:
        try:
            initialize_database()
            logger.info("Database schema initialized and pot inventory seeded")
        except Exception as exc:
            logger.warning("Database initialization skipped: %s", exc)

        settings = get_settings()
        if settings.weather_refresh_on_startup:
            self.refresh_weather_on_startup()
        self.prepare_sensors_on_startup()
        if settings.prescription_scheduler_enabled:
            self.start_prescription_scheduler()
        if settings.actuation_scheduler_enabled:
            self.start_actuation_scheduler()
        self.warm_baseline_cache()

    def prepare_sensors_on_startup(self) -> None:
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

    def refresh_weather_on_startup(self) -> None:
        try:
            result = WeatherRefreshService().refresh_forecast()
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

    def start_prescription_scheduler(self) -> None:
        if self.prescription_scheduler is None:
            self.prescription_scheduler = PrescriptionScheduler(clock=self.clock)
        self.prescription_scheduler.start()
        logger.info("Prescription scheduler started")

    def start_actuation_scheduler(self) -> None:
        if self.actuation_scheduler is None:
            self.actuation_scheduler = ActuationScheduler(clock=self.clock)
        self.actuation_scheduler.start()
        logger.info("Actuation scheduler started")

    def warm_baseline_cache(self) -> None:
        try:
            baseline = self.experiment_service.warm_default_baseline_cache()
            logger.info(
                "Baseline experiment cache warm-up %s for %s to %s",
                baseline["status"],
                baseline["start"],
                baseline["end"],
            )
        except Exception as exc:
            logger.warning("Baseline experiment cache warm-up skipped: %s", exc)


web_api_application = WebApiApplication()


def create_app() -> FastAPI:
    return web_api_application.create_app()


app = create_app()
