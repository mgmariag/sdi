from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

from digital_twin.application.sensors.reading_cadence import (
    ACTUAL_READING_INTERVAL_MINUTES,
    DAILY_READING_TIMES,
    DEFAULT_SENSOR_READING_CADENCE,
    DEFAULT_HISTORY_START,
    LOCAL_TZ,
    LOCATION_NAME,
)
from digital_twin.application.sensors.placement import SensorPlacementService
from digital_twin.domain.pot import Pot
from digital_twin.domain.sensor import (
    SensorReadingResolution,
    SensorSource,
)
from digital_twin.domain.valve import DEFAULT_VALVE_LAYOUT
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil


def _load_pots() -> list[dict[str, Any]]:
    sensor_pot_ids = SensorPlacementService().selected_pot_ids()
    if not sensor_pot_ids:
        return []
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT
                p.*,
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
              AND p.id = ANY(%(sensor_pot_ids)s)
            ORDER BY p.id
            """,
            {"sensor_pot_ids": sensor_pot_ids},
        ).fetchall()


def _load_pots_by_ids(pot_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not pot_ids:
        return {}
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT
                p.*,
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
              AND p.id = ANY(%(pot_ids)s)
            ORDER BY p.id
            """,
            {"pot_ids": pot_ids},
        ).fetchall()
    return {int(row["id"]): row for row in rows}


def _load_weather(start_date: date, end_date: date) -> list[dict[str, Any]]:
    start_ts = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_ts = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            WITH ranked_weather AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY observed_local_at
                        ORDER BY
                            CASE
                                WHEN source = 'open-meteo-archive' THEN 0
                                WHEN source = 'open-meteo-forecast' THEN 1
                                ELSE 2
                            END,
                            id DESC
                    ) AS source_rank
                FROM weather_hourly
                WHERE location_name = %(location)s
                  AND observed_local_at >= %(start_ts)s
                  AND observed_local_at < %(end_ts)s
            )
            SELECT *
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_local_at
            """,
            {"location": LOCATION_NAME, "start_ts": start_ts.replace(tzinfo=None), "end_ts": end_ts.replace(tzinfo=None)},
        ).fetchall()


def _load_latest_weather_at(recorded_at: datetime) -> dict[str, Any] | None:
    with get_connection(row_factory=dict_row) as conn:
        return conn.execute(
            """
            WITH ranked_weather AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY observed_local_at
                        ORDER BY
                            CASE
                                WHEN source = 'open-meteo-archive' THEN 0
                                WHEN source = 'open-meteo-forecast' THEN 1
                                ELSE 2
                            END,
                            id DESC
                    ) AS source_rank
                FROM weather_hourly
                WHERE location_name = %(location)s
                  AND observed_local_at <= %(recorded_at)s
            )
            SELECT *
            FROM ranked_weather
            WHERE source_rank = 1
            ORDER BY observed_local_at DESC
            LIMIT 1
            """,
            {
                "location": LOCATION_NAME,
                "recorded_at": DEFAULT_SENSOR_READING_CADENCE.db_timestamp(recorded_at),
            },
        ).fetchone()


def _load_latest_sensor_states(recorded_at: datetime, source: str) -> dict[int, dict[str, Any]]:
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (sensor_id)
                sensor_id,
                recorded_at,
                soil_moisture_pct
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND reading_resolution = ANY(%(resolutions)s)
              AND recorded_at < %(recorded_at)s
            ORDER BY sensor_id, recorded_at DESC
            """,
            {
                "sources": SensorSource.query_values(source),
                "recorded_at": DEFAULT_SENSOR_READING_CADENCE.db_timestamp(recorded_at),
                "resolutions": SensorReadingResolution.query_values(),
            },
        ).fetchall()
    return {row["sensor_id"]: row for row in rows}


def _closest_actual_recorded_at(conn, sensor_id: int, recorded_at: datetime) -> datetime:
    aligned_recorded_at = DEFAULT_SENSOR_READING_CADENCE.db_timestamp(
        DEFAULT_SENSOR_READING_CADENCE.align_to_interval(
            recorded_at,
            ACTUAL_READING_INTERVAL_MINUTES,
        )
    )
    requested_at = DEFAULT_SENSOR_READING_CADENCE.db_timestamp(recorded_at)
    row = conn.execute(
        """
        SELECT recorded_at
        FROM sensor_readings
        WHERE sensor_id = %(sensor_id)s
          AND reading_resolution = %(raw_resolution)s
          AND recorded_at BETWEEN %(start_at)s AND %(end_at)s
        ORDER BY abs(extract(epoch FROM (recorded_at - %(requested_at)s::timestamp)))
        LIMIT 1
        """,
        {
            "sensor_id": sensor_id,
            "raw_resolution": SensorReadingResolution.RAW.value,
            "requested_at": requested_at,
            "start_at": requested_at - timedelta(minutes=ACTUAL_READING_INTERVAL_MINUTES / 2),
            "end_at": requested_at + timedelta(minutes=ACTUAL_READING_INTERVAL_MINUTES / 2),
        },
    ).fetchone()
    return row[0] if row else aligned_recorded_at


def _upsert_sensor_rows(conn, rows: list[dict[str, Any]], update_changed_at: bool = False) -> int:
    changed_at_value = "now() AT TIME ZONE 'Europe/Bucharest'" if update_changed_at else "NULL"
    conflict_filter = "" if update_changed_at else f"WHERE sensor_readings.source <> '{SensorSource.ACTUAL.value}'"
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO sensor_readings (
                sensor_id,
                recorded_at,
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                reading_resolution,
                sample_count,
                changed_at
            )
            VALUES (
                %(sensor_id)s,
                %(recorded_at)s,
                %(soil_moisture_pct)s,
                %(air_temperature_c)s,
                %(air_humidity_pct)s,
                %(substrate_temperature_c)s,
                %(source)s,
                %(reading_resolution)s,
                %(sample_count)s,
                {changed_at_value}
            )
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = {changed_at_value}
            {conflict_filter}
            """,
            rows,
        )
        rowcount = cur.rowcount
    return rowcount if rowcount else len(rows)


def _initial_state_for_pot(pot: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(2026 + int(pot["id"]))
    target = float(pot["moisture_target_pct"])
    return {
        "moisture": soil.clamp(target + rng.uniform(-6.0, 4.0), 5.0, 95.0),
        "last_recorded_at": datetime.combine(DEFAULT_HISTORY_START, time(0, 0), tzinfo=LOCAL_TZ),
    }


def _apply_hourly_environment(
    state: dict[str, Any],
    pot: dict[str, Any],
    weather: dict[str, Any],
    local_day: date,
    hours: float = 1.0,
) -> None:
    state["moisture"] = soil.apply_hourly_environment_moisture(
        state["moisture"],
        pot,
        weather,
        local_day,
        hours=hours,
        outdoor=Pot.from_mapping(pot).is_outdoor(local_day),
    )


def _apply_virtual_irrigation_if_due(
    state: dict[str, Any],
    pot: dict[str, Any],
    weather: dict[str, Any],
    recorded_at: datetime,
) -> None:
    hour = recorded_at.hour
    if hour not in {7, 18}:
        return

    temp = soil.number(weather.get("temperature_c"), 20.0)
    precipitation = soil.number(weather.get("precipitation_mm"), 0.0)
    threshold = float(pot["moisture_min_pct"])
    target = float(pot["moisture_target_pct"])

    if recorded_at.month in {12, 1, 2}:
        threshold = 10.0
        target = float(pot["winter_moisture_target_pct"])
        if temp <= 10.0:
            return

    if temp <= 0.0:
        return
    if precipitation >= 2.0 and state["moisture"] > threshold * 0.85:
        return
    if hour == 18 and pot["plant_type_code"] not in {"vegetables", "herbs"} and pot["size_class"] != "small":
        return
    if state["moisture"] >= threshold:
        return

    domain_pot = Pot.from_mapping(pot)
    max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[domain_pot.size_class]
    planned_volume_ml = domain_pot.volume_for_moisture_deficit(state["moisture"], target, max_minutes)
    state["moisture"] = domain_pot.moisture_after_volume(state["moisture"], planned_volume_ml)


def _sensor_row(
    pot: dict[str, Any],
    state: dict[str, Any],
    weather: dict[str, Any],
    recorded_at: datetime,
    source: str,
) -> dict[str, Any]:
    rng = random.Random(f"{pot['id']}|{recorded_at.isoformat()}|{source}")
    air_temperature = _microclimate_temperature(pot, weather, recorded_at)
    air_humidity = soil.clamp(soil.number(weather.get("relative_humidity_pct"), 60.0) + rng.uniform(-4.0, 4.0), 20.0, 100.0)
    substrate_temperature = air_temperature + _substrate_delta(pot, recorded_at)
    moisture = soil.clamp(
        state["moisture"] + rng.uniform(-1.2, 1.2),
        soil.minimum_realistic_moisture(pot, recorded_at.date()),
        100.0,
    )
    return {
        "sensor_id": pot["id"],
        "recorded_at": DEFAULT_SENSOR_READING_CADENCE.db_timestamp(recorded_at),
        "soil_moisture_pct": round(moisture, 2),
        "air_temperature_c": round(air_temperature, 2),
        "air_humidity_pct": round(air_humidity, 2),
        "substrate_temperature_c": round(substrate_temperature, 2),
        "source": source,
        "reading_resolution": SensorReadingResolution.RAW.value,
        "sample_count": 1,
    }


def _microclimate_temperature(pot: dict[str, Any], weather: dict[str, Any], recorded_at: datetime) -> float:
    base = soil.number(weather.get("temperature_c"), 20.0)
    hour = recorded_at.hour
    sun_delta = {
        "shade": -0.4,
        "partial": 0.5,
        "full": 1.4,
        "reflected_heat": 2.6,
    }[pot["sun_exposure"]]
    if hour < 8 or hour > 19:
        sun_delta *= 0.2
    elif 11 <= hour <= 16:
        sun_delta *= 1.25
    wind_delta = -0.3 if pot["wind_exposure"] == "gusty" else 0.0
    indoor_delta = 1.8 if not Pot.from_mapping(pot).is_outdoor(recorded_at.date()) else 0.0
    return base + sun_delta + wind_delta + indoor_delta


def _substrate_delta(pot: dict[str, Any], recorded_at: datetime) -> float:
    material_delta = {
        "terracotta": 0.6,
        "plastic": 1.1,
        "ceramic": 0.4,
        "fabric": -0.2,
    }.get(pot["container_material"], 0.0)
    if 11 <= recorded_at.hour <= 16 and pot["sun_exposure"] in {"full", "reflected_heat"}:
        material_delta += 1.5
    return material_delta


def _fallback_weather(observed_at: datetime) -> dict[str, Any]:
    month = observed_at.month
    if month in {12, 1, 2}:
        temp = 3.0
        humidity = 78.0
    elif month in {6, 7, 8}:
        temp = 25.0
        humidity = 55.0
    elif month in {3, 4, 5}:
        temp = 16.0
        humidity = 65.0
    else:
        temp = 13.0
        humidity = 70.0
    return {
        "observed_at": observed_at,
        "observed_local_at": observed_at.replace(tzinfo=None) if observed_at.tzinfo else observed_at,
        "observed_date": observed_at.date(),
        "observed_hour": observed_at.hour,
        "temperature_c": temp,
        "relative_humidity_pct": humidity,
        "precipitation_mm": 0.0,
        "wind_speed_kmh": 8.0,
        "wind_gust_kmh": 14.0,
        "evapotranspiration_mm": None,
        "source": "fallback",
    }


def _reading_exists(recorded_at: datetime, source: str) -> bool:
    sensor_ids = SensorPlacementService().selected_pot_ids()
    if not sensor_ids:
        return False
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT sensor_id)
            FROM sensor_readings
            WHERE source = ANY(%(sources)s)
              AND reading_resolution = %(raw_resolution)s
              AND recorded_at = %(recorded_at)s
              AND sensor_id = ANY(%(sensor_ids)s)
            """,
            {
                "sources": SensorSource.query_values(source),
                "raw_resolution": SensorReadingResolution.RAW.value,
                "recorded_at": DEFAULT_SENSOR_READING_CADENCE.db_timestamp(recorded_at),
                "sensor_ids": sensor_ids,
            },
        ).fetchone()
    return bool(row and row[0] >= len(sensor_ids))


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "obj") and isinstance(value.obj, dict):
        return value.obj
    return {}


def _valve_number_for_zone(zone: str) -> int | None:
    return DEFAULT_VALVE_LAYOUT.valve_number_for_zone(zone)


def _is_hot_irrigation_day(conn, day: date) -> bool:
    start_at = datetime.combine(day, time.min)
    end_at = start_at + timedelta(days=1)
    row = conn.execute(
        """
        SELECT max(temperature_c) AS max_temperature_c
        FROM weather_hourly
        WHERE location_name = %(location)s
          AND observed_local_at >= %(start_at)s
          AND observed_local_at < %(end_at)s
        """,
        {"location": LOCATION_NAME, "start_at": start_at, "end_at": end_at},
    ).fetchone()
    return bool(row and soil.number(row["max_temperature_c"], 0.0) >= 32.0)


class SensorStateRepository:
    """Database access used by generated sensor state workflows."""

    def load_pots(self) -> list[dict[str, Any]]:
        return _load_pots()

    def load_pots_by_ids(self, pot_ids: list[int]) -> dict[int, dict[str, Any]]:
        return _load_pots_by_ids(pot_ids)

    def load_weather(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return _load_weather(start_date, end_date)

    def load_latest_weather_at(self, recorded_at: datetime) -> dict[str, Any] | None:
        return _load_latest_weather_at(recorded_at)

    def load_latest_sensor_states(self, recorded_at: datetime, source: str) -> dict[int, dict[str, Any]]:
        return _load_latest_sensor_states(recorded_at, source)

    def closest_actual_recorded_at(self, conn, sensor_id: int, recorded_at: datetime) -> datetime:
        return _closest_actual_recorded_at(conn, sensor_id, recorded_at)

    def upsert_sensor_rows(self, conn, rows: list[dict[str, Any]], update_changed_at: bool = False) -> int:
        return _upsert_sensor_rows(conn, rows, update_changed_at=update_changed_at)

    def reading_exists(self, recorded_at: datetime, source: str) -> bool:
        return _reading_exists(recorded_at, source)

    def is_hot_irrigation_day(self, conn, day: date) -> bool:
        return _is_hot_irrigation_day(conn, day)


class SensorStateProjector:
    """Builds simulated sensor states and database rows from pot/weather state."""

    def initial_sensor_states(self, pots: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        return {pot["id"]: self.initial_state_for_pot(pot) for pot in pots}

    def initial_state_for_pot(self, pot: dict[str, Any]) -> dict[str, Any]:
        return _initial_state_for_pot(pot)

    def apply_hourly_environment(
        self,
        state: dict[str, Any],
        pot: dict[str, Any],
        weather: dict[str, Any],
        local_day: date,
        hours: float = 1.0,
    ) -> None:
        _apply_hourly_environment(state, pot, weather, local_day, hours=hours)

    def apply_virtual_irrigation_if_due(
        self,
        state: dict[str, Any],
        pot: dict[str, Any],
        weather: dict[str, Any],
        recorded_at: datetime,
    ) -> None:
        _apply_virtual_irrigation_if_due(state, pot, weather, recorded_at)

    def sensor_row(
        self,
        pot: dict[str, Any],
        state: dict[str, Any],
        weather: dict[str, Any],
        recorded_at: datetime,
        source: str,
    ) -> dict[str, Any]:
        return _sensor_row(pot, state, weather, recorded_at, source)

    def microclimate_temperature(self, pot: dict[str, Any], weather: dict[str, Any], recorded_at: datetime) -> float:
        return _microclimate_temperature(pot, weather, recorded_at)

    def substrate_delta(self, pot: dict[str, Any], recorded_at: datetime) -> float:
        return _substrate_delta(pot, recorded_at)

    def fallback_weather(self, observed_at: datetime) -> dict[str, Any]:
        return _fallback_weather(observed_at)

    def payload_dict(self, value: Any) -> dict[str, Any]:
        return _payload_dict(value)

    def valve_number_for_zone(self, zone: str) -> int | None:
        return _valve_number_for_zone(zone)
