from __future__ import annotations

from datetime import datetime
from typing import Any

from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.repositories.overview._common import freshness_percent as _freshness_percent


def sensor_coverage(conn, now: datetime, today, active_pots: int) -> dict[str, Any]:
    sensor_nodes = int(
        conn.execute(
            """
            SELECT count(DISTINCT pot_id) AS count
            FROM sensor_location_recommendations
            """
        ).fetchone()["count"]
        or 0
    )
    if sensor_nodes == 0:
        sensor_nodes = int(
            conn.execute("SELECT count(DISTINCT sensor_id) AS count FROM sensor_readings").fetchone()["count"]
            or 0
        )

    measured_today = int(
        conn.execute(
            """
            WITH recommended_sensor_pots AS (
                SELECT DISTINCT pot_id
                FROM sensor_location_recommendations
            ),
            current_readings AS (
                SELECT DISTINCT sr.sensor_id
                FROM sensor_readings sr
                JOIN pots p ON p.id = sr.sensor_id
                WHERE p.active = true
                  AND sr.recorded_at::date = %(today)s
                  AND (sr.source = 'actual_sensor' OR sr.source = %(sensor_source)s)
            )
            SELECT
                CASE
                    WHEN EXISTS (SELECT 1 FROM recommended_sensor_pots)
                        THEN (
                            SELECT count(*)
                            FROM current_readings cr
                            WHERE cr.sensor_id IN (SELECT pot_id FROM recommended_sensor_pots)
                        )
                    ELSE (SELECT count(*) FROM current_readings)
                END AS count
            """,
            {"today": today, "sensor_source": get_settings().sensor_source},
        ).fetchone()["count"]
        or 0
    )
    measured_pots = min(active_pots, measured_today)
    estimated_pots = max(0, active_pots - measured_pots)

    latest_at = conn.execute("SELECT max(recorded_at) AS latest_at FROM sensor_readings").fetchone()["latest_at"]
    freshness = _freshness_percent(latest_at, now)
    return {
        "total_pots": active_pots,
        "sensor_nodes": sensor_nodes,
        "data_freshness_pct": freshness,
        "segments": [
            {"key": "measured", "label": "Measured pots", "count": measured_pots},
            {"key": "estimated", "label": "Estimated pots", "count": estimated_pots},
        ],
    }

