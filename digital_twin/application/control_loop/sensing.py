from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.rows import dict_row

from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.simulation.soil_model import sun_factor, wind_factor
from digital_twin.simulation.sensors.context import load_sensor_context
from digital_twin.simulation.weather_model import load_weather


class SensingStage:
    """Loads stored pot, sensor, and weather inputs for a control-loop window."""

    def initialize_storage(self) -> None:
        initialize_database()

    def load_active_pots(self) -> list[dict[str, Any]]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    p.*,
                    pt.label AS plant_type_label,
                    pt.water_need_level,
                    pt.heat_sensitive,
                    pt.allows_second_watering,
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
            return [_prepare_pot_row(row) for row in rows]

    def load_sensor_context(self, start_date: date, end_date: date, pots: list[dict[str, Any]]) -> dict[str, Any]:
        return load_sensor_context(start_date, end_date, pots)

    def load_weather(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return load_weather(start_date, end_date)


def _prepare_pot_row(row: dict[str, Any]) -> dict[str, Any]:
    pot = dict(row)
    for field in (
        "drip_flow_ml_min",
        "moisture_min_pct",
        "moisture_target_pct",
        "moisture_max_pct",
        "winter_moisture_target_pct",
        "volume_l",
        "evaporation_factor",
        "retention_factor",
    ):
        if pot.get(field) is not None:
            pot[field] = float(pot[field])
    pot["_sun_factor"] = sun_factor(pot)
    pot["_wind_factor"] = wind_factor(pot)
    return pot
