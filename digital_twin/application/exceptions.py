from __future__ import annotations


class DigitalTwinError(Exception):
    """Base class for expected application errors."""


class InvalidDateRange(DigitalTwinError, ValueError):
    """Raised when an end date is before a start date."""


class NoWeatherData(DigitalTwinError):
    """Raised when required weather rows are unavailable."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {"message": message}


class ExperimentConfigurationError(DigitalTwinError, ValueError):
    """Raised when an experiment request is invalid."""
