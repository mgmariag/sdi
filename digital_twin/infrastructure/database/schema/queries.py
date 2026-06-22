from __future__ import annotations

from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from digital_twin.infrastructure.database.connection import get_connection


def _json_ready(value):
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def get_database_health() -> dict[str, Any]:
    with get_connection(row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT
                current_database() AS database_name,
                current_user AS user_name,
                version() AS version
            """
        ).fetchone()
        row["pot_count"] = conn.execute("SELECT count(*) AS count FROM pots").fetchone()["count"]
        return _json_ready(row)

def get_pot_summary() -> dict[str, Any]:
    with get_connection(row_factory=dict_row) as conn:
        totals = conn.execute("SELECT count(*) AS total FROM pots").fetchone()
        by_size = conn.execute(
            """
            SELECT size_class, count(*) AS count
            FROM pots
            GROUP BY size_class
            ORDER BY size_class
            """
        ).fetchall()
        by_plant = conn.execute(
            """
            SELECT p.plant_type_code, pt.label, count(*) AS count
            FROM pots p
            JOIN plant_types pt ON pt.code = p.plant_type_code
            GROUP BY p.plant_type_code, pt.label
            ORDER BY p.plant_type_code
            """
        ).fetchall()
        return _json_ready(
            {
                "total": totals["total"],
                "by_size": by_size,
                "by_plant_type": by_plant,
            }
        )

def list_pots(limit: int = 50, offset: int = 0, size_class: str | None = None, plant_type: str | None = None) -> list[dict[str, Any]]:
    filters = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if size_class:
        filters.append("p.size_class = %(size_class)s")
        params["size_class"] = size_class
    if plant_type:
        filters.append("p.plant_type_code = %(plant_type)s")
        params["plant_type"] = plant_type

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    query = f"""
        SELECT
            p.id,
            p.pot_code,
            p.label,
            p.size_class,
            p.small_subtype,
            p.plant_type_code,
            pt.label AS plant_type_label,
            p.balcony_zone,
            p.rain_exposure,
            p.sun_exposure,
            p.wind_exposure,
            p.container_material,
            p.soil_profile,
            p.drip_flow_ml_min,
            p.cycle_soak_enabled,
            p.morning_window_start,
            p.morning_window_end,
            p.evening_window_start,
            p.evening_window_end,
            p.moisture_min_pct,
            p.moisture_target_pct,
            p.moisture_max_pct,
            p.winter_moisture_target_pct
        FROM pots p
        JOIN plant_types pt ON pt.code = p.plant_type_code
        {where_clause}
        ORDER BY p.id
        LIMIT %(limit)s OFFSET %(offset)s
    """
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(query, params).fetchall()
        return _json_ready(rows)
