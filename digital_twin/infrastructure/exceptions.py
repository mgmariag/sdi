from __future__ import annotations

from digital_twin.application.exceptions import DigitalTwinError


class DatabaseUnavailable(DigitalTwinError):
    """Raised when database access fails."""


class WeatherProviderError(DigitalTwinError):
    """Raised when an external weather provider fails."""
