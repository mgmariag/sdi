from __future__ import annotations

from collections.abc import Callable
from datetime import date

from psycopg.rows import dict_row

from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_IRRIGATION_POLICY,
)


def state_simulation_start(
    start_date: date,
    end_date: date,
    anchor_resolver: Callable[[date], date | None] | None = None,
) -> date:
    season_start = active_season_start_date(end_date)
    if season_start is None:
        return start_date

    resolver = anchor_resolver or historical_state_anchor_date
    anchor_date = resolver(end_date)
    warmup_start = season_start
    if anchor_date is not None:
        warmup_start = max(season_start, anchor_date)
    return min(start_date, warmup_start)


def active_season_start_date(day: date) -> date | None:
    if DEFAULT_IRRIGATION_POLICY.dormant_period(day):
        return None
    return date(day.year, 4, 1)


def historical_state_anchor_date(end_date: date) -> date | None:
    try:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT
                    (
                        SELECT min(recorded_at::date)
                        FROM sensor_readings
                        WHERE recorded_at::date <= %(end_date)s
                    ) AS sensor_start,
                    (
                        SELECT min(observed_local_at::date)
                        FROM weather_hourly
                        WHERE observed_local_at::date <= %(end_date)s
                    ) AS weather_start
                """,
                {"end_date": end_date},
            ).fetchone()
    except Exception:
        return None

    if not row:
        return None

    sensor_start = row.get("sensor_start")
    weather_start = row.get("weather_start")
    if sensor_start and weather_start:
        return max(sensor_start, weather_start)
    return sensor_start or weather_start
