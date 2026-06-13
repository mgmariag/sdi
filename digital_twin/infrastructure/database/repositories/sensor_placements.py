from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_twin.domain.valves import VALVE_COUNT, VALVE_ZONE_DESIGN, VALVE_ZONE_ORDER
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.repositories._utils import _json_ready


class SensorPlacementRepository:
    """Persistence for the current sensor placement recommendation set."""

    def active_pots(self) -> list[dict[str, Any]]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    p.id,
                    p.pot_code,
                    p.label,
                    p.size_class,
                    p.small_subtype,
                    p.plant_type_code,
                    pt.label AS plant_type_label,
                    pt.water_need_level,
                    pt.heat_sensitive,
                    pt.allows_second_watering,
                    p.balcony_zone,
                    p.rain_exposure,
                    p.sun_exposure,
                    p.wind_exposure,
                    p.container_material,
                    p.soil_profile,
                    p.drip_flow_ml_min,
                    p.moisture_min_pct,
                    p.moisture_target_pct,
                    p.moisture_max_pct,
                    ps.volume_l,
                    ps.evaporation_factor,
                    ps.retention_factor
                FROM pots p
                JOIN plant_types pt ON pt.code = p.plant_type_code
                JOIN pot_size_profiles ps
                  ON ps.code = CASE
                        WHEN p.size_class = 'small' THEN 'small_' || p.small_subtype
                        ELSE p.size_class
                     END
                WHERE p.active = true
                ORDER BY p.id
                """
            ).fetchall()
            return [_json_ready(row) for row in rows]

    def current(self) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    r.id,
                    r.requested_sensor_count,
                    r.rank,
                    r.pot_id,
                    r.score,
                    r.reason,
                    r.criteria,
                    r.created_at,
                    p.pot_code,
                    p.label AS pot_label,
                    p.size_class,
                    p.small_subtype,
                    p.plant_type_code,
                    pt.label AS plant_type_label,
                    p.balcony_zone,
                    p.rain_exposure,
                    p.sun_exposure,
                    p.wind_exposure,
                    p.container_material,
                    p.drip_flow_ml_min,
                    p.moisture_target_pct
                FROM sensor_location_recommendations r
                JOIN pots p ON p.id = r.pot_id
                JOIN plant_types pt ON pt.code = p.plant_type_code
                ORDER BY r.rank
                """
            ).fetchall()
            counts = conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM pots WHERE active = true) AS active_pot_count,
                    (
                        SELECT count(DISTINCT sr.sensor_id)
                        FROM sensor_readings sr
                        JOIN pots p ON p.id = sr.sensor_id
                        WHERE p.active = true
                    ) AS sensor_reading_pot_count
                """
            ).fetchone()
        items = [_with_valve_fields(_json_ready(row)) for row in rows]
        stored_sensor_count = items[0]["requested_sensor_count"] if items else 0
        sensor_reading_pot_count = int(counts["sensor_reading_pot_count"] or 0)
        active_pot_count = int(counts["active_pot_count"] or 0)
        default_sensor_count = min(active_pot_count, VALVE_COUNT) if active_pot_count > 0 else 0
        selected_zones = {str(item.get("balcony_zone") or "") for item in items}
        required_zones = {item["zone"] for item in VALVE_ZONE_DESIGN}
        return {
            "sensor_count": stored_sensor_count or default_sensor_count,
            "minimum_sensor_count": VALVE_COUNT,
            "valve_count": VALVE_COUNT,
            "has_all_valve_zones": bool(items) and required_zones.issubset(selected_zones),
            "stored_sensor_count": stored_sensor_count,
            "sensor_reading_pot_count": sensor_reading_pot_count,
            "items": items,
            "updated_at": items[0]["created_at"] if items else None,
            "active_pot_count": active_pot_count,
        }

    def replace(self, requested_sensor_count: int, recommendations: list[dict[str, Any]]) -> dict[str, Any]:
        with get_connection() as conn:
            conn.execute("DELETE FROM sensor_location_recommendations")
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO sensor_location_recommendations (
                        sensor_id, requested_sensor_count, rank, pot_id, score, reason, criteria
                    )
                    VALUES (
                        %(sensor_id)s, %(requested_sensor_count)s, %(rank)s, %(pot_id)s, %(score)s, %(reason)s, %(criteria)s
                    )
                    """,
                    [
                        {
                            "sensor_id": item.get("sensor_id") or item["rank"],
                            "requested_sensor_count": requested_sensor_count,
                            "rank": item["rank"],
                            "pot_id": item["pot_id"],
                            "score": item["score"],
                            "reason": item["reason"],
                            "criteria": Jsonb(item["criteria"]),
                        }
                        for item in recommendations
                    ],
                )
            conn.commit()
        return self.current()

    def selected_pot_ids(self, candidate_pot_ids: list[int] | None = None) -> list[int]:
        filters = []
        params: dict[str, Any] = {}
        if candidate_pot_ids:
            filters.append("r.pot_id = ANY(%(candidate_pot_ids)s)")
            params["candidate_pot_ids"] = candidate_pot_ids
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT r.pot_id
                FROM sensor_location_recommendations r
                JOIN pots p ON p.id = r.pot_id
                {where_clause}
                ORDER BY r.rank
                """,
                params,
            ).fetchall()
        return [int(row[0]) for row in rows]


def _with_valve_fields(item: dict[str, Any]) -> dict[str, Any]:
    valve_number = VALVE_ZONE_ORDER.get(str(item.get("balcony_zone") or ""))
    item["valve_number"] = valve_number
    item["valve_label"] = f"V{valve_number}" if valve_number else "Unmapped"
    return item
