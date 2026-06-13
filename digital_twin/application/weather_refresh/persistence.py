from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_twin.application.weather_refresh.rows import (
    WEATHER_COMPARISON_COLUMNS,
    WEATHER_INSERT_COLUMNS,
    local_bucket_from_observed_at,
)
from digital_twin.infrastructure.database.connection import get_connection


def upsert_weather_hourly(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_weather_upsert_query(), [_weather_db_row(row) for row in rows])
        conn.commit()
    return len(rows)


def upsert_weather_hourly_with_stats(rows: list[dict[str, Any]], skip_existing_observed: bool) -> dict[str, int]:
    stats = empty_import_stats()
    if not rows:
        return stats

    with get_connection(row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            query = _weather_upsert_query()
            for row in rows:
                if skip_existing_observed and _has_existing_observed_row(cur, row):
                    stats["skipped_existing_observed"] += 1
                    continue

                existing = cur.execute(
                    """
                    SELECT *
                    FROM weather_hourly
                    WHERE location_name = %(location_name)s
                      AND source = %(source)s
                      AND observed_local_at = %(observed_local_at)s
                    """,
                    row,
                ).fetchone()

                if existing is None:
                    cur.execute(query, _weather_db_row(row))
                    stats["inserted"] += 1
                elif _weather_row_changed(existing, row):
                    cur.execute(query, _weather_db_row(row))
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
        conn.commit()
    return stats


def empty_import_stats() -> dict[str, int]:
    return {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_existing_observed": 0,
    }


def merge_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _has_existing_observed_row(cur, row: dict[str, Any]) -> bool:
    existing = cur.execute(
        """
        SELECT 1
        FROM weather_hourly
        WHERE location_name = %(location_name)s
          AND observed_at = %(observed_at)s
          AND observed_local_at = %(observed_local_at)s
          AND source <> %(source)s
          AND observed_at <= now()
        LIMIT 1
        """,
        row,
    ).fetchone()
    return existing is not None


def _weather_upsert_query() -> str:
    columns = ", ".join(WEATHER_INSERT_COLUMNS)
    values = ", ".join(f"%({column})s" for column in WEATHER_INSERT_COLUMNS)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in WEATHER_INSERT_COLUMNS
        if column not in {"location_name", "source", "observed_local_at"}
    )
    changed_condition = " OR ".join(
        f"weather_hourly.{column} IS DISTINCT FROM EXCLUDED.{column}"
        for column in WEATHER_COMPARISON_COLUMNS
    )
    return f"""
        INSERT INTO weather_hourly ({columns})
        VALUES ({values})
        ON CONFLICT (location_name, source, observed_local_at) DO UPDATE SET
            {updates},
            changed_at = now()
        WHERE {changed_condition}
    """


def _weather_db_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {column: row.get(column) for column in WEATHER_INSERT_COLUMNS}
    observed_local_at = normalized.get("observed_local_at")
    if observed_local_at is None:
        observed_local_at = local_bucket_from_observed_at(normalized["observed_at"])
        normalized["observed_local_at"] = observed_local_at
    normalized["observed_date"] = normalized.get("observed_date") or observed_local_at.date()
    if normalized.get("observed_hour") is None:
        normalized["observed_hour"] = observed_local_at.hour
    raw_payload = normalized.get("raw_payload") or {}
    normalized["raw_payload"] = raw_payload if isinstance(raw_payload, Jsonb) else Jsonb(raw_payload)
    return normalized


def _weather_row_changed(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    for column in WEATHER_COMPARISON_COLUMNS:
        if _comparable(existing.get(column)) != _comparable(incoming.get(column)):
            return True
    return False


def _comparable(value):
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, datetime):
        return value
    return value
