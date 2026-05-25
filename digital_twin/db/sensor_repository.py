from __future__ import annotations

from typing import Any

from tools.sensor_readings import get_sensor_reading_summary


class SensorRepository:
    """Sensor reading read access."""

    def summary(self, source: str | None = None) -> dict[str, Any]:
        return get_sensor_reading_summary(source=source)

