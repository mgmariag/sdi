from __future__ import annotations

from datetime import datetime
from typing import Any

from digital_twin.application.sensors.state_rows import SensorStateRepository
from digital_twin.application.sensors.reading_cadence import (
    ACTUAL_READING_INTERVAL_MINUTES,
    DEFAULT_SENSOR_READING_CADENCE,
    LOCAL_TZ,
    SensorReadingCadence,
)
from digital_twin.domain.sensor import SensorReadingResolution, SensorSource
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database


class SensorReadingIngestionService:
    """Stores actual raw sensor readings in local sensor slots."""

    def __init__(
        self,
        cadence: SensorReadingCadence | None = None,
        state_repository: SensorStateRepository | None = None,
    ) -> None:
        self.cadence = cadence or DEFAULT_SENSOR_READING_CADENCE
        self.state_repository = state_repository or SensorStateRepository()

    def ingest_actual_sensor_readings(
        self,
        readings: list[dict[str, Any]],
        recorded_at: datetime | None = None,
        source: str = SensorSource.ACTUAL.value,
    ) -> dict[str, Any]:
        """Store actual raw sensor readings in 15-minute local slots."""
        initialize_database()
        if source != SensorSource.ACTUAL.value:
            raise ValueError(f"actual sensor ingestion must use source={SensorSource.ACTUAL.value!r}")

        default_recorded_at = self.cadence.as_local(recorded_at or datetime.now(LOCAL_TZ)).replace(
            second=0,
            microsecond=0,
        )
        rows: list[dict[str, Any]] = []
        with get_connection() as conn:
            for item in readings:
                sensor_id = item.get("sensor_id", item.get("pot_id"))
                if sensor_id is None:
                    raise ValueError("Each reading must include sensor_id or pot_id")
                item_recorded_at = self.cadence.as_local(item.get("recorded_at") or default_recorded_at).replace(
                    second=0,
                    microsecond=0,
                )
                rows.append(
                    {
                        "sensor_id": int(sensor_id),
                        "recorded_at": self.state_repository.closest_actual_recorded_at(
                            conn,
                            int(sensor_id),
                            item_recorded_at,
                        ),
                        "soil_moisture_pct": _required_number(item, "soil_moisture_pct"),
                        "air_temperature_c": _optional_number(item.get("air_temperature_c")),
                        "air_humidity_pct": _optional_number(item.get("air_humidity_pct")),
                        "substrate_temperature_c": _optional_number(item.get("substrate_temperature_c")),
                        "source": SensorSource.ACTUAL.value,
                        "reading_resolution": SensorReadingResolution.RAW.value,
                        "sample_count": 1,
                    }
                )
            upserted = self.state_repository.upsert_sensor_rows(conn, rows, update_changed_at=True)
            conn.commit()

        slots = sorted({row["recorded_at"].isoformat() for row in rows})
        return {
            "source": source,
            "reading_resolution": SensorReadingResolution.RAW.value,
            "reading_interval_minutes": ACTUAL_READING_INTERVAL_MINUTES,
            "received_readings": len(readings),
            "upserted_readings": upserted,
            "stored_slots": slots,
        }


def _required_number(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if value is None:
        raise ValueError(f"Each reading must include {key}")
    return float(value)


def _optional_number(value) -> float | None:
    return None if value is None else float(value)
