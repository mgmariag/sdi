from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database


class SensorRepository:
    """Sensor reading read access."""

    def summary(self, source: str | None = None) -> dict[str, Any]:
        initialize_database()
        params: dict[str, Any] = {}
        where = ""
        if source:
            where = "WHERE source = %(source)s"
            params["source"] = source

        with get_connection(row_factory=dict_row) as conn:
            by_source = conn.execute(
                f"""
                SELECT
                    source,
                    count(*) AS row_count,
                    count(DISTINCT sensor_id) AS sensor_count,
                    min(recorded_at) AS first_recorded_at,
                    max(recorded_at) AS last_recorded_at
                FROM sensor_readings
                {where}
                GROUP BY source
                ORDER BY source
                """,
                params,
            ).fetchall()
            by_resolution = conn.execute(
                f"""
                SELECT
                    source,
                    reading_resolution,
                    count(*) AS row_count,
                    count(DISTINCT sensor_id) AS sensor_count,
                    min(recorded_at) AS first_recorded_at,
                    max(recorded_at) AS last_recorded_at,
                    sum(sample_count)::int AS sample_count
                FROM sensor_readings
                {where}
                GROUP BY source, reading_resolution
                ORDER BY source, reading_resolution
                """,
                params,
            ).fetchall()
            recent = conn.execute(
                f"""
                SELECT
                    sensor_id,
                    recorded_at,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    source,
                    reading_resolution,
                    sample_count
                FROM sensor_readings
                {where}
                ORDER BY recorded_at DESC, sensor_id
                LIMIT 20
                """,
                params,
            ).fetchall()

        return _json_ready(
            {
                "sources": by_source,
                "resolutions": by_resolution,
                "recent": recent,
            }
        )


def _json_ready(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
