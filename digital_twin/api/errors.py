from __future__ import annotations

from fastapi import HTTPException

from digital_twin.application.exceptions import (
    DigitalTwinError,
    InvalidDateRange,
    NoWeatherData,
)
from digital_twin.infrastructure.exceptions import DatabaseUnavailable


class ApiErrorMapper:
    """Maps application exceptions to HTTP responses."""

    def to_http_error(
        self,
        exc: Exception,
        default_status: int = 500,
        prefix: str | None = None,
    ) -> HTTPException:
        if isinstance(exc, NoWeatherData):
            return HTTPException(status_code=400, detail=exc.detail)
        if isinstance(exc, (InvalidDateRange, ValueError)):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, DatabaseUnavailable):
            return HTTPException(status_code=503, detail=str(exc))
        if isinstance(exc, DigitalTwinError):
            return HTTPException(status_code=default_status, detail=str(exc))
        detail = f"{prefix}: {exc}" if prefix else str(exc)
        return HTTPException(status_code=default_status, detail=detail)
