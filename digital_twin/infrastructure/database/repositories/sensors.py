from __future__ import annotations

from typing import Any


class SensorRepository:
    """Sensor reading read access."""

    def summary(self, source: str | None = None) -> dict[str, Any]:
        from digital_twin.application.sensor_history.readings.core import (
            get_sensor_reading_summary,
        )

        return get_sensor_reading_summary(source=source)
