from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

from digital_twin.core.time import now_local
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.repositories._utils import _json_ready
from digital_twin.infrastructure.database.repositories.overview._common import (
    NO_IRRIGATION_PLANNED_LABEL,
    NO_IRRIGATION_RECORDED_LABEL,
    confidence_score as _confidence_score,
    freshness_percent as _freshness_percent,
    number as _number,
    rain_level as _rain_level,
)
from digital_twin.infrastructure.database.repositories.overview.activity import irrigation_activity
from digital_twin.infrastructure.database.repositories.overview.irrigation_windows import (
    most_recent_irrigation,
    next_irrigation_window,
    next_planned_irrigation,
    next_prescription_irrigation,
    next_recommendation_ready_at,
)
from digital_twin.infrastructure.database.repositories.overview.sensor_coverage import sensor_coverage
from digital_twin.infrastructure.database.repositories.overview.valve_plan import valve_plan


class OverviewRepository:
    """Read model for the dashboard shown before an experiment is selected."""

    def current(self) -> dict[str, Any]:
        now = now_local().replace(tzinfo=None)
        today = now.date()
        with get_connection(row_factory=dict_row) as conn:
            active_pots = int(
                conn.execute("SELECT count(*) AS count FROM pots WHERE active = true").fetchone()["count"]
                or 0
            )
            irrigation_window = next_irrigation_window(conn, now)
            valve_plan_result = valve_plan(conn, now, irrigation_window)
            current_state = self._current_state(conn, now, today, valve_plan_result)
            sensor_coverage_result = sensor_coverage(conn, now, today, active_pots)
            plant_overview = self._plant_overview(conn)

        return _json_ready(
            {
                "generated_at": now.isoformat(),
                "state": current_state,
                "sensor_coverage": sensor_coverage_result,
                "valve_plan": valve_plan_result,
                "plant_overview": plant_overview,
            }
        )

    def _current_state(
        self,
        conn,
        now: datetime,
        today,
        valve_plan_result: dict[str, Any],
    ) -> dict[str, Any]:
        latest = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sensor_id)
                    sensor_id, recorded_at, soil_moisture_pct, source
                FROM sensor_readings
                ORDER BY
                    sensor_id,
                    recorded_at DESC,
                    CASE reading_resolution
                        WHEN 'raw_15min' THEN 1
                        WHEN 'hourly' THEN 2
                        ELSE 3
                    END
            )
            SELECT
                round(avg(soil_moisture_pct), 2) AS avg_moisture,
                max(recorded_at) AS latest_recorded_at,
                count(*) AS sensor_count
            FROM latest
            """
        ).fetchone()
        fallback = conn.execute(
            "SELECT round(avg(moisture_target_pct), 2) AS avg_target FROM pots WHERE active = true"
        ).fetchone()["avg_target"]
        moisture = _number(latest["avg_moisture"], _number(fallback, 0.0))
        latest_at = latest["latest_recorded_at"]

        rain = conn.execute(
            """
            SELECT
                coalesce(sum(coalesce(precipitation_mm, rain_mm, 0)), 0) AS rain_mm,
                max(temperature_c) AS max_temperature_c,
                count(*) AS weather_rows
            FROM weather_hourly
            WHERE location_name = 'Cluj-Napoca'
              AND observed_local_at >= %(start_at)s
              AND observed_local_at < %(end_at)s
            """,
            {
                "start_at": datetime.combine(today, time.min),
                "end_at": datetime.combine(today + timedelta(days=3), time.min),
            },
        ).fetchone()
        rain_mm = _number(rain["rain_mm"], 0.0)
        max_temperature = rain["max_temperature_c"]

        dry = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sensor_id)
                    sensor_id, soil_moisture_pct
                FROM sensor_readings
                ORDER BY sensor_id, recorded_at DESC
            )
            SELECT count(*) AS dry_sensors
            FROM latest l
            JOIN pots p ON p.id = l.sensor_id
            WHERE p.active = true
              AND l.soil_moisture_pct < p.moisture_min_pct
            """
        ).fetchone()
        dry_sensors = int(dry["dry_sensors"] or 0)
        recommendation_on = dry_sensors > 0 or int(valve_plan_result.get("valve_starts") or 0) > 0
        planned_window = next_planned_irrigation(conn, now) or next_prescription_irrigation(conn, now)
        recent_window = most_recent_irrigation(conn, now)
        irrigation_activity_result = irrigation_activity(planned_window, recent_window)
        recommendation_ready_at = next_recommendation_ready_at(now)

        confidence = _confidence_score(
            freshness_percent=_freshness_percent(latest_at, now),
            sensor_count=int(latest["sensor_count"] or 0),
            weather_rows=int(rain["weather_rows"] or 0),
        )

        return {
            "current_soil_moisture_pct": round(moisture, 2),
            "forecast_rain_next_3_days_mm": round(rain_mm, 2),
            "forecast_rain_level": _rain_level(rain_mm),
            "forecast_max_temperature_c": round(float(max_temperature), 2) if max_temperature is not None else None,
            "irrigation_recommendation": "ON" if recommendation_on else "OFF",
            "next_recommendation_ready_at": recommendation_ready_at.isoformat(),
            "dry_sensor_count": dry_sensors,
            "confidence": confidence,
            "next_irrigation_window": planned_window
            or {"label": NO_IRRIGATION_PLANNED_LABEL, "start_at": None, "end_at": None},
            "recent_irrigation_window": recent_window
            or {"label": NO_IRRIGATION_RECORDED_LABEL, "start_at": None, "end_at": None},
            "irrigation_activity": irrigation_activity_result,
            "latest_sensor_recorded_at": latest_at,
        }

    def _plant_overview(self, conn) -> dict[str, Any]:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sensor_id)
                    sensor_id, soil_moisture_pct
                FROM sensor_readings
                ORDER BY
                    sensor_id,
                    recorded_at DESC,
                    CASE reading_resolution
                        WHEN 'raw_15min' THEN 1
                        WHEN 'hourly' THEN 2
                        ELSE 3
                    END
            )
            SELECT
                p.plant_type_code,
                pt.label AS plant_type_label,
                count(*)::int AS pot_count,
                round(avg(coalesce(l.soil_moisture_pct, p.moisture_target_pct)), 2) AS avg_moisture_pct
            FROM pots p
            JOIN plant_types pt ON pt.code = p.plant_type_code
            LEFT JOIN latest l ON l.sensor_id = p.id
            WHERE p.active = true
            GROUP BY p.plant_type_code, pt.label
            ORDER BY pot_count DESC, pt.label
            """
        ).fetchall()
        total = sum(int(row["pot_count"]) for row in rows)
        top = rows[:4]
        other = rows[4:]
        items = [
            {
                "key": row["plant_type_code"],
                "label": row["plant_type_label"],
                "count": int(row["pot_count"]),
                "avg_moisture_pct": _number(row["avg_moisture_pct"], 0.0),
            }
            for row in top
        ]
        if other:
            other_count = sum(int(row["pot_count"]) for row in other)
            weighted = sum(_number(row["avg_moisture_pct"], 0.0) * int(row["pot_count"]) for row in other)
            items.append(
                {
                    "key": "other",
                    "label": "Other",
                    "count": other_count,
                    "avg_moisture_pct": round(weighted / max(other_count, 1), 2),
                }
            )
        return {"total_pots": total, "items": items}
