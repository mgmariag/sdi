from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from digital_twin.api.errors import ApiErrorMapper
from digital_twin.application.weather_refresh.service import (
    WeatherRefreshService,
)

class WeatherRoutes:
    def __init__(
        self,
        service: WeatherRefreshService | None = None,
        error_mapper: ApiErrorMapper | None = None,
    ) -> None:
        self.router = APIRouter(prefix="/api/weather/cluj-napoca")
        self.service = service or WeatherRefreshService()
        self.error_mapper = error_mapper or ApiErrorMapper()
        self.router.add_api_route("/summary", self.summary, methods=["GET"])
        self.router.add_api_route("/hourly", self.hourly, methods=["GET"])
        self.router.add_api_route("/cache", self.cache, methods=["POST"])
        self.router.add_api_route("/cache-range", self.cache_range, methods=["POST"])
        self.router.add_api_route("/refresh-forecast", self.refresh_forecast, methods=["POST"])
        self.router.add_api_route("/import-csv", self.import_csv, methods=["POST"])

    def summary(self):
        try:
            return self.service.summary()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Weather cache unavailable") from exc

    def hourly(
        self,
        start: date = Query(...),
        end: date = Query(...),
        limit: int = Query(1000, ge=1, le=10000),
    ):
        try:
            return {"items": self.service.hourly(start=start, end=end, limit=limit)}
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 503, "Weather cache unavailable") from exc

    def cache(
        self,
        start_year: int = Query(1940, ge=1940, le=2050),
        end_year: int = Query(2050, ge=1940, le=2050),
        include_climate: bool = Query(True),
    ):
        if end_year < start_year:
            raise self.error_mapper.to_http_error(ValueError("end_year must not be before start_year"), 400)
        try:
            return self.service.cache_cluj_range(
                start=date(start_year, 1, 1),
                end=date(end_year, 12, 31),
                include_climate=include_climate,
            )
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 502, "Weather cache failed") from exc

    def cache_range(
        self,
        start: date = Query(...),
        end: date = Query(...),
        include_climate: bool = Query(True),
    ):
        if end < start:
            raise self.error_mapper.to_http_error(ValueError("end date must not be before start date"), 400)
        try:
            return self.service.cache_cluj_range(start=start, end=end, include_climate=include_climate)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 502, "Weather cache failed") from exc

    def refresh_forecast(self, force: bool = Query(False)):
        try:
            return self.service.refresh_forecast(force=force)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 502, "Forecast refresh failed") from exc

    def import_csv(
        self,
        csv_path: str = Query(...),
        skip_existing_observed: bool = Query(True),
    ):
        try:
            return self.service.import_csv(
                csv_path,
                skip_existing_observed=skip_existing_observed,
            )
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 502, "Weather CSV import failed") from exc

