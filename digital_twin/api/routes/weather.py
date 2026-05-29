from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from digital_twin.api.errors import http_error
from digital_twin.services.weather_service import WeatherService


api_router = APIRouter(prefix="/api/weather/cluj-napoca")
service_router = APIRouter(prefix="/weather/cluj-napoca")
service = WeatherService()


@api_router.get("/summary")
def api_cluj_weather_summary():
    try:
        return service.summary()
    except Exception as exc:
        raise http_error(exc, 503, "Weather cache unavailable") from exc


@api_router.get("/hourly")
def api_cluj_weather_hourly(
    start: date = Query(...),
    end: date = Query(...),
    limit: int = Query(1000, ge=1, le=10000),
):
    try:
        return {"items": service.hourly(start=start, end=end, limit=limit)}
    except Exception as exc:
        raise http_error(exc, 503, "Weather cache unavailable") from exc


@service_router.post("/cache")
def cache_cluj_weather(
    start_year: int = Query(1940, ge=1940, le=2050),
    end_year: int = Query(2050, ge=1940, le=2050),
    include_climate: bool = Query(True),
):
    if end_year < start_year:
        raise http_error(ValueError("end_year must not be before start_year"), 400)
    try:
        return service.cache_cluj_range(
            start=date(start_year, 1, 1),
            end=date(end_year, 12, 31),
            include_climate=include_climate,
        )
    except Exception as exc:
        raise http_error(exc, 502, "Weather cache failed") from exc


@service_router.post("/cache-range")
def cache_cluj_weather_by_date(
    start: date = Query(...),
    end: date = Query(...),
    include_climate: bool = Query(True),
):
    try:
        return service.cache_cluj_range(start=start, end=end, include_climate=include_climate)
    except Exception as exc:
        raise http_error(exc, 502, "Weather cache failed") from exc


@service_router.get("/summary")
def service_cluj_weather_summary():
    try:
        return service.summary()
    except Exception as exc:
        raise http_error(exc, 503, "Weather cache unavailable") from exc


@service_router.get("/hourly")
def service_cluj_weather_hourly(
    start: date = Query(...),
    end: date = Query(...),
    limit: int = Query(1000, ge=1, le=10000),
):
    try:
        return {"items": service.hourly(start=start, end=end, limit=limit)}
    except Exception as exc:
        raise http_error(exc, 503, "Weather cache unavailable") from exc


@service_router.post("/refresh-forecast")
def refresh_cluj_forecast(force: bool = Query(False)):
    try:
        return service.refresh_forecast(force=force)
    except Exception as exc:
        raise http_error(exc, 502, "Forecast refresh failed") from exc


