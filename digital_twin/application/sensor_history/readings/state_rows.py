from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

from digital_twin.application.sensor_history.readings.shared import (
    DAILY_READING_TIMES,
    DEFAULT_HISTORY_START,
    LOCAL_TZ,
    LOCATION_NAME,
    align_to_interval,
    db_timestamp,
    query_sources,
    sensor_equipped_pot_ids,
)
from digital_twin.domain.sensors import (
    ACTUAL_READING_INTERVAL_MINUTES,
    ACTUAL_SENSOR_SOURCE,
    DAILY_RESOLUTION,
    HOURLY_RESOLUTION,
    RAW_RESOLUTION,
)
from digital_twin.domain.valves import VALVE_ZONE_DESIGN
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.simulation.soil_model import (
    apply_hourly_environment_moisture,
    clamp,
    minimum_realistic_moisture,
    number,
)


def load_pots() -> list[dict[str, Any]]:
    sensor_pot_ids = sensor_equipped_pot_ids()
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


def load_pots_by_ids(pot_ids: list[int]) -> dict[int, dict[str, Any]]:
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


def load_weather(start_date: date, end_date: date) -> list[dict[str, Any]]:
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


def load_latest_weather_at(recorded_at: datetime) -> dict[str, Any] | None:
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
            {"location": LOCATION_NAME, "recorded_at": db_timestamp(recorded_at)},
        ).fetchone()


def load_latest_sensor_states(recorded_at: datetime, source: str) -> dict[int, dict[str, Any]]:
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
                "sources": query_sources(source),
                "recorded_at": db_timestamp(recorded_at),
                "resolutions": [RAW_RESOLUTION, HOURLY_RESOLUTION, DAILY_RESOLUTION],
            },
        ).fetchall()
    return {row["sensor_id"]: row for row in rows}


def closest_actual_recorded_at(conn, sensor_id: int, recorded_at: datetime) -> datetime:
    aligned_recorded_at = db_timestamp(align_to_interval(recorded_at, ACTUAL_READING_INTERVAL_MINUTES))
    requested_at = db_timestamp(recorded_at)
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
            "raw_resolution": RAW_RESOLUTION,
            "requested_at": requested_at,
            "start_at": requested_at - timedelta(minutes=ACTUAL_READING_INTERVAL_MINUTES / 2),
            "end_at": requested_at + timedelta(minutes=ACTUAL_READING_INTERVAL_MINUTES / 2),
        },
    ).fetchone()
    return row[0] if row else aligned_recorded_at


def upsert_sensor_rows(conn, rows: list[dict[str, Any]], update_changed_at: bool = False) -> int:
    changed_at_value = "now() AT TIME ZONE 'Europe/Bucharest'" if update_changed_at else "NULL"
    conflict_filter = "" if update_changed_at else f"WHERE sensor_readings.source <> '{ACTUAL_SENSOR_SOURCE}'"
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


def initial_sensor_states(pots: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {pot["id"]: initial_state_for_pot(pot) for pot in pots}


def initial_state_for_pot(pot: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(2026 + int(pot["id"]))
    target = float(pot["moisture_target_pct"])
    return {
        "moisture": clamp(target + rng.uniform(-6.0, 4.0), 5.0, 95.0),
        "last_recorded_at": datetime.combine(DEFAULT_HISTORY_START, time(0, 0), tzinfo=LOCAL_TZ),
    }


def apply_hourly_environment(
    state: dict[str, Any],
    pot: dict[str, Any],
    weather: dict[str, Any],
    local_day: date,
    hours: float = 1.0,
) -> None:
    state["moisture"] = apply_hourly_environment_moisture(
        state["moisture"],
        pot,
        weather,
        local_day,
        hours=hours,
        outdoor=is_outdoor(pot, local_day),
    )


def apply_virtual_irrigation_if_due(
    state: dict[str, Any],
    pot: dict[str, Any],
    weather: dict[str, Any],
    recorded_at: datetime,
) -> None:
    hour = recorded_at.hour
    if hour not in {7, 18}:
        return

    temp = number(weather.get("temperature_c"), 20.0)
    precipitation = number(weather.get("precipitation_mm"), 0.0)
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

    volume_l = float(pot["volume_l"])
    retention = max(float(pot["retention_factor"]), 0.1)
    flow_rate = max(float(pot["drip_flow_ml_min"]), 1.0)
    need_pct = max(0.0, target - state["moisture"])
    planned_volume_ml = need_pct * volume_l * 10.0 / retention
    max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[pot["size_class"]]
    planned_volume_ml = min(planned_volume_ml, flow_rate * max_minutes)
    moisture_gain = planned_volume_ml * retention / max(volume_l * 10.0, 1.0)
    state["moisture"] = clamp(state["moisture"] + moisture_gain, 0.0, 100.0)


def sensor_row(
    pot: dict[str, Any],
    state: dict[str, Any],
    weather: dict[str, Any],
    recorded_at: datetime,
    source: str,
) -> dict[str, Any]:
    rng = random.Random(f"{pot['id']}|{recorded_at.isoformat()}|{source}")
    air_temperature = microclimate_temperature(pot, weather, recorded_at)
    air_humidity = clamp(number(weather.get("relative_humidity_pct"), 60.0) + rng.uniform(-4.0, 4.0), 20.0, 100.0)
    substrate_temperature = air_temperature + substrate_delta(pot, recorded_at)
    moisture = clamp(
        state["moisture"] + rng.uniform(-1.2, 1.2),
        minimum_realistic_moisture(pot, recorded_at.date()),
        100.0,
    )
    return {
        "sensor_id": pot["id"],
        "recorded_at": db_timestamp(recorded_at),
        "soil_moisture_pct": round(moisture, 2),
        "air_temperature_c": round(air_temperature, 2),
        "air_humidity_pct": round(air_humidity, 2),
        "substrate_temperature_c": round(substrate_temperature, 2),
        "source": source,
        "reading_resolution": RAW_RESOLUTION,
        "sample_count": 1,
    }


def microclimate_temperature(pot: dict[str, Any], weather: dict[str, Any], recorded_at: datetime) -> float:
    base = number(weather.get("temperature_c"), 20.0)
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
    indoor_delta = 1.8 if not is_outdoor(pot, recorded_at.date()) else 0.0
    return base + sun_delta + wind_delta + indoor_delta


def substrate_delta(pot: dict[str, Any], recorded_at: datetime) -> float:
    material_delta = {
        "terracotta": 0.6,
        "plastic": 1.1,
        "ceramic": 0.4,
        "fabric": -0.2,
    }.get(pot["container_material"], 0.0)
    if 11 <= recorded_at.hour <= 16 and pot["sun_exposure"] in {"full", "reflected_heat"}:
        material_delta += 1.5
    return material_delta


def fallback_weather(observed_at: datetime) -> dict[str, Any]:
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


def reading_exists(recorded_at: datetime, source: str) -> bool:
    sensor_ids = sensor_equipped_pot_ids()
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
                "sources": query_sources(source),
                "raw_resolution": RAW_RESOLUTION,
                "recorded_at": db_timestamp(recorded_at),
                "sensor_ids": sensor_ids,
            },
        ).fetchone()
    return bool(row and row[0] >= len(sensor_ids))


def payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "obj") and isinstance(value.obj, dict):
        return value.obj
    return {}


def valve_number_for_zone(zone: str) -> int | None:
    for design in VALVE_ZONE_DESIGN:
        if design["zone"] == zone:
            return int(design["valve_number"])
    return None


def is_hot_irrigation_day(conn, day: date) -> bool:
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
    return bool(row and number(row["max_temperature_c"], 0.0) >= 32.0)


def is_outdoor(pot: dict[str, Any], day: date) -> bool:
    return True
