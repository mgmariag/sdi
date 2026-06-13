from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.repositories.anfis_model_repository import DEFAULT_MODEL_KEY

ANFIS_MODEL_KEY_BY_START_YEAR = {
    2023: "anfis-2023-simulated",
}


def baseline_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("baseline-db-v17-seasonal-rain-dose-valve-details", start, end, persist, sensor_placement_cache_token())


def sampling_cache_key(
    start: date,
    end: date,
    sample_interval_days: int,
    sample_interval_hours: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    effective_sample_interval_hours = sample_interval_hours or sample_interval_days * 24
    return (
        "sampling-db-sensor-weather-v14-hourly-raw-weather-popover-valve-details",
        start,
        end,
        sample_interval_days,
        effective_sample_interval_hours,
        persist,
        sensor_placement_cache_token(),
    )


def anfis_cache_key(
    start: date,
    end: date,
    seed: int | None,
    persist: bool = True,
) -> tuple[Any, ...]:
    return (
        "anfis-db-size-flow-pots-v17-weighted-zone-calibrated-valve-details",
        start,
        end,
        seed,
        resolve_anfis_model_key(start),
        persist,
        sensor_placement_cache_token(),
    )


def fuzzy_dt_cache_key(start: date, end: date, persist: bool = True) -> tuple[Any, ...]:
    return ("fuzzy-dt-db-v10-volume-score-valve-details", start, end, persist, sensor_placement_cache_token())


def snapshot_cache_key(start: date, end: date) -> tuple[Any, ...]:
    return ("db-snapshot-v7-baseline-startup", start, end, sensor_placement_cache_token())


def sensor_placement_cache_token() -> tuple[Any, ...]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                count(*) AS location_count,
                coalesce(max(requested_sensor_count), 0) AS requested_sensor_count,
                coalesce(string_agg(pot_id::text, ',' ORDER BY rank), '') AS pot_ids
            FROM sensor_location_recommendations
            """
        ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0), row[2] or "")


def resolve_anfis_model_key(start: date) -> str:
    """Select the persisted ANFIS model from the range lower bound."""
    return ANFIS_MODEL_KEY_BY_START_YEAR.get(start.year, DEFAULT_MODEL_KEY)
