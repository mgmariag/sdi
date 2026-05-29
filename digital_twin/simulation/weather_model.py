from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.rows import dict_row

from digital_twin.core.exceptions import NoWeatherData
from digital_twin.db.connection import get_connection
from digital_twin.simulation.dto import LOCAL_TZ, LOCATION_NAME
from digital_twin.simulation.soil_model import clamp as _clamp, local_observed_at as _local_observed_at, number as _number

def _load_weather(start_date: date, end_date: date) -> list[dict[str, Any]]:
    start_ts = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_ts = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
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
        return rows


def _raise_if_missing_historical_weather(weather_rows: list[dict[str, Any]], start_date: date, end_date: date) -> None:
    missing = _missing_historical_weather_hours(weather_rows, start_date, end_date)
    if not missing:
        return

    requested_range = {"start": start_date.isoformat(), "end": end_date.isoformat()}
    first_missing = min(missing)
    last_missing = max(missing)
    availability = _weather_availability_ranges()
    message = (
        f"Stored historical weather data is incomplete for {requested_range['start']} to {requested_range['end']}."
    )
    raise NoWeatherData(
        message,
        {
            "code": "weather_data_unavailable",
            "message": message,
            "requestedRange": requested_range,
            "missingHistoricalRange": {
                "start": first_missing.date().isoformat(),
                "end": last_missing.date().isoformat(),
                "missingHours": len(missing),
            },
            "closestLowerRange": _closest_lower_weather_range(availability, start_date),
            "closestHigherRange": _closest_higher_weather_range(availability, end_date),
            "availableRanges": availability,
        },
    )


def _missing_historical_weather_hours(weather_rows: list[dict[str, Any]], start_date: date, end_date: date) -> list[datetime]:
    today = datetime.now(LOCAL_TZ).date()
    historical_end = min(end_date, today - timedelta(days=1))
    if start_date > historical_end:
        return []

    existing_hours = {
        _local_weather_bucket(row)
        for row in weather_rows
    }
    missing = []
    current = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_dt = datetime.combine(historical_end, time(23, 0), tzinfo=LOCAL_TZ)
    while current <= end_dt:
        current_hour = current.replace(minute=0, second=0, microsecond=0)
        if current_hour not in existing_hours:
            missing.append(current_hour)
        current += timedelta(hours=1)
    return missing


def _weather_availability_ranges() -> list[dict[str, Any]]:
    with get_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            WITH weather_dates AS (
                SELECT DISTINCT
                    observed_date AS weather_date
                FROM weather_hourly
                WHERE location_name = %(location)s
            ),
            numbered_dates AS (
                SELECT
                    weather_date,
                    weather_date - (row_number() OVER (ORDER BY weather_date))::int AS range_group
                FROM weather_dates
            )
            SELECT
                min(weather_date) AS start_date,
                max(weather_date) AS end_date,
                count(*) AS day_count
            FROM numbered_dates
            GROUP BY range_group
            ORDER BY start_date
            """,
            {"location": LOCATION_NAME},
        ).fetchall()

    ranges = []
    for row in rows:
        ranges.append(
            {
                "start": row["start_date"].isoformat(),
                "end": row["end_date"].isoformat(),
                "dayCount": int(row["day_count"]),
            }
        )
    return ranges


def _closest_lower_weather_range(availability: list[dict[str, Any]], start_date: date) -> dict[str, Any] | None:
    candidates = [item for item in availability if date.fromisoformat(item["end"]) < start_date]
    if not candidates:
        return None
    return max(candidates, key=lambda item: date.fromisoformat(item["end"]))


def _closest_higher_weather_range(availability: list[dict[str, Any]], end_date: date) -> dict[str, Any] | None:
    candidates = [item for item in availability if date.fromisoformat(item["start"]) > end_date]
    if not candidates:
        return None
    return min(candidates, key=lambda item: date.fromisoformat(item["start"]))


def _with_estimated_future_weather(
    weather_rows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> tuple[list[dict[str, Any]], int]:
    rows = list(weather_rows)
    existing_hours = {
        _local_weather_bucket(row)
        for row in rows
    }
    today = datetime.now(LOCAL_TZ).date()
    current = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_dt = datetime.combine(end_date, time(23, 0), tzinfo=LOCAL_TZ)
    estimated_count = 0

    while current <= end_dt:
        current_hour = current.replace(minute=0, second=0, microsecond=0)
        if current.date() >= today and current_hour not in existing_hours:
            rows.append(_estimated_weather_row(current_hour))
            existing_hours.add(current_hour)
            estimated_count += 1
        current += timedelta(hours=1)

    rows.sort(key=lambda row: _local_observed_at(row))
    return rows, estimated_count


def _local_weather_bucket(row: dict[str, Any]) -> datetime:
    observed_local_at = row.get("observed_local_at")
    if observed_local_at is not None:
        if observed_local_at.tzinfo is None:
            return observed_local_at.replace(tzinfo=LOCAL_TZ)
        return observed_local_at.astimezone(LOCAL_TZ)
    return _local_observed_at(row).replace(minute=0, second=0, microsecond=0)


def _estimated_weather_row(observed_at: datetime) -> dict[str, Any]:
    rng = random.Random(f"estimated-weather|{observed_at.isoformat()}")
    profile = _estimated_month_profile(observed_at.month)
    hour = observed_at.hour
    diurnal = math.cos(((hour - 14) / 24.0) * 2.0 * math.pi)
    temperature = profile["avg_temp"] + profile["diurnal_amp"] * diurnal + rng.uniform(-1.8, 1.8)
    humidity = _clamp(profile["humidity"] - (temperature - profile["avg_temp"]) * 1.6 + rng.uniform(-7.0, 7.0), 30.0, 98.0)
    wind_speed = max(1.0, profile["wind"] + rng.uniform(-3.0, 5.0))
    wind_gust = wind_speed + rng.uniform(4.0, 12.0)
    precipitation = 0.0
    if rng.random() < profile["precip_chance"]:
        precipitation = round(rng.uniform(profile["precip_min"], profile["precip_max"]), 2)
    evapotranspiration = max(
        0.0,
        profile["evap_base"] * (1.0 + max(temperature, 0.0) / 28.0) * ((100.0 - humidity) / 100.0) * (1.0 + wind_speed / 35.0),
    )

    return {
        "id": None,
        "location_name": LOCATION_NAME,
        "latitude": 46.7712,
        "longitude": 23.6236,
        "observed_at": observed_at,
        "observed_local_at": observed_at.replace(tzinfo=None) if observed_at.tzinfo else observed_at,
        "observed_date": observed_at.date(),
        "observed_hour": observed_at.hour,
        "source": "estimated-weather",
        "is_forecast": observed_at > datetime.now(LOCAL_TZ),
        "temperature_c": round(temperature, 2),
        "relative_humidity_pct": round(humidity, 2),
        "precipitation_mm": precipitation,
        "wind_speed_kmh": round(wind_speed, 2),
        "wind_gust_kmh": round(wind_gust, 2),
        "cloud_cover_pct": round(_clamp(profile["cloud"] + rng.uniform(-18.0, 18.0), 0.0, 100.0), 2),
        "apparent_temperature_c": round(temperature - max(0.0, wind_speed - 15.0) * 0.05, 2),
        "is_day": 7 <= hour <= 18,
        "precipitation_probability_pct": round(profile["precip_chance"] * 100.0, 2),
        "evapotranspiration_mm": round(evapotranspiration, 3),
        "rain_mm": precipitation,
        "showers_mm": 0.0,
        "snowfall_cm": round(precipitation * 0.7, 2) if temperature <= 1.0 else 0.0,
        "weather_code": None,
        "pressure_msl_hpa": None,
        "surface_pressure_hpa": None,
        "wind_direction_10m_deg": None,
        "soil_temperature_0cm_c": round(temperature - 0.8, 2),
        "soil_temperature_6cm_c": round(temperature - 0.4, 2),
        "soil_moisture_0_to_1cm": None,
        "soil_moisture_1_to_3cm": None,
        "shortwave_radiation_w_m2": None,
        "raw_payload": {"estimated": True},
    }


def _estimated_month_profile(month: int) -> dict[str, float]:
    profiles = {
        1: {"avg_temp": -2.0, "diurnal_amp": 3.0, "humidity": 84.0, "wind": 10.0, "cloud": 76.0, "precip_chance": 0.07, "precip_min": 0.2, "precip_max": 2.5, "evap_base": 0.012},
        2: {"avg_temp": 0.5, "diurnal_amp": 4.0, "humidity": 80.0, "wind": 10.0, "cloud": 70.0, "precip_chance": 0.06, "precip_min": 0.2, "precip_max": 2.2, "evap_base": 0.014},
        3: {"avg_temp": 6.0, "diurnal_amp": 6.0, "humidity": 72.0, "wind": 11.0, "cloud": 62.0, "precip_chance": 0.06, "precip_min": 0.3, "precip_max": 3.0, "evap_base": 0.020},
        4: {"avg_temp": 12.0, "diurnal_amp": 7.0, "humidity": 67.0, "wind": 10.0, "cloud": 55.0, "precip_chance": 0.07, "precip_min": 0.4, "precip_max": 4.0, "evap_base": 0.030},
        5: {"avg_temp": 17.0, "diurnal_amp": 8.0, "humidity": 64.0, "wind": 9.0, "cloud": 50.0, "precip_chance": 0.08, "precip_min": 0.5, "precip_max": 6.0, "evap_base": 0.044},
        6: {"avg_temp": 21.0, "diurnal_amp": 9.0, "humidity": 61.0, "wind": 8.0, "cloud": 44.0, "precip_chance": 0.08, "precip_min": 0.5, "precip_max": 6.5, "evap_base": 0.060},
        7: {"avg_temp": 23.5, "diurnal_amp": 10.0, "humidity": 56.0, "wind": 8.0, "cloud": 35.0, "precip_chance": 0.06, "precip_min": 0.3, "precip_max": 5.0, "evap_base": 0.072},
        8: {"avg_temp": 23.0, "diurnal_amp": 10.0, "humidity": 55.0, "wind": 8.0, "cloud": 34.0, "precip_chance": 0.05, "precip_min": 0.2, "precip_max": 4.5, "evap_base": 0.070},
        9: {"avg_temp": 17.5, "diurnal_amp": 8.0, "humidity": 63.0, "wind": 8.0, "cloud": 42.0, "precip_chance": 0.05, "precip_min": 0.3, "precip_max": 3.8, "evap_base": 0.044},
        10: {"avg_temp": 10.5, "diurnal_amp": 6.0, "humidity": 72.0, "wind": 9.0, "cloud": 58.0, "precip_chance": 0.06, "precip_min": 0.3, "precip_max": 3.2, "evap_base": 0.026},
        11: {"avg_temp": 5.0, "diurnal_amp": 4.5, "humidity": 80.0, "wind": 10.0, "cloud": 70.0, "precip_chance": 0.07, "precip_min": 0.2, "precip_max": 2.8, "evap_base": 0.016},
        12: {"avg_temp": -0.8, "diurnal_amp": 3.2, "humidity": 85.0, "wind": 10.0, "cloud": 78.0, "precip_chance": 0.07, "precip_min": 0.2, "precip_max": 2.4, "evap_base": 0.012},
    }
    return profiles[month]



