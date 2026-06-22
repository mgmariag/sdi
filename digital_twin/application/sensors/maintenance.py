from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from digital_twin.application.sensors.availability import SensorAvailabilityService
from digital_twin.application.sensors.generation import SensorReadingGenerator
from digital_twin.application.sensors.placement import SensorPlacementService
from digital_twin.application.sensors.reading_cadence import (
    DAILY_READING_TIMES,
    DEFAULT_SENSOR_READING_CADENCE,
    LOCAL_TZ,
    SensorReadingCadence,
)
from digital_twin.domain.sensor import SensorReadingResolution, SensorSource
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database


class SensorReadingMaintenanceService:
    """Coordinates sensor reading coverage checks, reseeding, and cleanup."""

    def __init__(
        self,
        availability_service: SensorAvailabilityService | None = None,
        generator: SensorReadingGenerator | None = None,
        cadence: SensorReadingCadence | None = None,
    ) -> None:
        self.cadence = (
            cadence
            or getattr(availability_service, "cadence", None)
            or getattr(generator, "cadence", DEFAULT_SENSOR_READING_CADENCE)
        )
        self.generator = generator or SensorReadingGenerator(cadence=self.cadence)
        self.availability_service = availability_service or SensorAvailabilityService(
            cadence=self.cadence
        )

    def ensure_tiered_sensor_readings(
        self,
        end_at: datetime | None = None,
        source: str = SensorSource.DEFAULT.value,
        cleanup: bool = True,
    ) -> dict[str, Any]:
        """Ensure simulated tiered readings cover the current local calendar periods."""
        initialize_database()
        end_at = self.cadence.align_to_reading_interval(
            self.cadence.as_local(end_at or datetime.now(LOCAL_TZ))
        )
        placement = SensorPlacementService().ensure_default_if_missing()
        sensor_ids = [int(item["pot_id"]) for item in placement.get("items", [])]
        coverage = self.availability_service.get_tiered_sensor_coverage(
            end_at=end_at,
            source=source,
            sensor_ids=sensor_ids,
        )
        seed = None
        if placement.get("changed") or not coverage["complete"]:
            seed = self.generator.seed_tiered_sensor_readings(
                end_at=end_at,
                start_date=coverage["daily_start"],
                source=source,
                replace_existing=True,
            )
            coverage = self.availability_service.get_tiered_sensor_coverage(
                end_at=end_at,
                source=source,
                sensor_ids=sensor_ids,
            )

        cleanup_summary = self.aggregate_and_cleanup_sensor_readings(now=end_at, source=source) if cleanup else None
        return {
            "source": source,
            "end_at": end_at.isoformat(),
            "placement_sensor_count": placement.get("sensor_count", 0),
            "coverage": coverage,
            "seed": seed,
            "cleanup": cleanup_summary,
        }

    def ensure_sensor_readings_for_experiment_range(
        self,
        start_date: date,
        end_date: date,
        source: str = SensorSource.DEFAULT.value,
    ) -> dict[str, Any]:
        """Ensure simulated readings exist for the non-future part of an experiment range."""
        initialize_database()
        placement = SensorPlacementService().ensure_default_if_missing()
        sensor_ids = [int(item["pot_id"]) for item in placement.get("items", [])]
        now = self.cadence.align_to_reading_interval(datetime.now(LOCAL_TZ))
        generation_end = min(end_date, now.date())

        if not sensor_ids or generation_end < start_date:
            return {
                "source": source,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "generated": False,
                "missing_dates": [],
                "placement_sensor_count": len(sensor_ids),
            }

        requested_dates = [
            start_date + timedelta(days=offset)
            for offset in range((generation_end - start_date).days + 1)
        ]
        complete_dates = self.availability_service.complete_sensor_dates_for_range(
            start_date,
            generation_end,
            source,
            sensor_ids,
        )
        missing_dates = [day for day in requested_dates if day not in complete_dates]
        seed = None
        if missing_dates:
            if generation_end == now.date():
                end_at = now
            else:
                end_at = datetime.combine(generation_end + timedelta(days=1), time.min, tzinfo=LOCAL_TZ) - timedelta(minutes=15)
            seed = self.generator.seed_tiered_sensor_readings(
                end_at=end_at,
                start_date=start_date,
                source=source,
                replace_existing=True,
            )

        return {
            "source": source,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "generated": seed is not None,
            "missing_dates": [day.isoformat() for day in missing_dates],
            "placement_sensor_count": len(sensor_ids),
            "seed": seed,
        }

    def aggregate_and_cleanup_sensor_readings(
        self,
        now: datetime | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        return _aggregate_and_cleanup_sensor_readings(
            now=now,
            source=source,
            cadence=self.cadence,
        )


def _aggregate_and_cleanup_sensor_readings(
    now: datetime | None,
    source: str | None,
    cadence: SensorReadingCadence,
) -> dict[str, Any]:
    initialize_database()
    now = cadence.as_local(now or datetime.now(LOCAL_TZ))
    period = cadence.tiered_periods(now)
    params: dict[str, Any] = {
        "timezone": LOCAL_TZ.key,
        "raw_start": cadence.db_timestamp(period["raw_start"]),
        "hourly_start": cadence.db_timestamp(period["hourly_start"]),
        "daily_start_date": period["daily_start"].date(),
        "raw_resolution": SensorReadingResolution.RAW.value,
        "hourly_resolution": SensorReadingResolution.HOURLY.value,
        "daily_resolution": SensorReadingResolution.DAILY.value,
        "actual_source": SensorSource.ACTUAL.value,
        "default_source": SensorSource.DEFAULT.value,
        "output_source": source or SensorSource.DEFAULT.value,
        "daily_slot_offsets_minutes": _daily_slot_offsets_minutes(),
    }
    source_filter = ""
    if source:
        params["sources"] = SensorSource.query_values(source)
        source_filter = "AND source = ANY(%(sources)s)"

    with get_connection() as conn:
        hourly_upserted = conn.execute(
            f"""
            WITH hourly AS (
                SELECT
                    sensor_id,
                    date_trunc('hour', recorded_at) AS bucket_recorded_at,
                    round(avg(soil_moisture_pct), 2) AS soil_moisture_pct,
                    round(avg(air_temperature_c), 2) AS air_temperature_c,
                    round(avg(air_humidity_pct), 2) AS air_humidity_pct,
                    round(avg(substrate_temperature_c), 2) AS substrate_temperature_c,
                    count(*)::int AS sample_count,
                    CASE
                        WHEN bool_or(source = %(actual_source)s) THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source
                FROM sensor_readings
                WHERE reading_resolution = %(raw_resolution)s
                  AND recorded_at < %(raw_start)s
                  AND recorded_at >= %(hourly_start)s
                  {source_filter}
                GROUP BY sensor_id, bucket_recorded_at
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id, bucket_recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, %(hourly_resolution)s,
                sample_count
            FROM hourly
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = NULL
            """,
            params,
        ).rowcount
        daily_from_raw = conn.execute(
            f"""
            WITH daily AS (
                SELECT
                    sensor_id,
                    recorded_at::date AS bucket_date,
                    (EXTRACT(HOUR FROM recorded_at)::int * 60 + EXTRACT(MINUTE FROM recorded_at)::int) AS bucket_slot_offset_minutes,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    sample_count,
                    CASE
                        WHEN source = %(actual_source)s THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source
                FROM sensor_readings
                WHERE reading_resolution = %(raw_resolution)s
                  AND recorded_at < %(hourly_start)s
                  AND recorded_at::date >= %(daily_start_date)s
                  AND (EXTRACT(HOUR FROM recorded_at)::int * 60 + EXTRACT(MINUTE FROM recorded_at)::int)
                        = ANY(%(daily_slot_offsets_minutes)s::int[])
                  {source_filter}
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id,
                bucket_date + make_interval(mins => bucket_slot_offset_minutes),
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                %(daily_resolution)s,
                sample_count
            FROM daily
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = NULL
            """,
            params,
        ).rowcount
        daily_from_hourly = conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    sr.sensor_id,
                    sr.recorded_at::date AS bucket_date,
                    slot.slot_offset_minutes AS bucket_slot_offset_minutes,
                    sr.soil_moisture_pct,
                    sr.air_temperature_c,
                    sr.air_humidity_pct,
                    sr.substrate_temperature_c,
                    sr.sample_count,
                    CASE
                        WHEN sr.source = %(actual_source)s THEN %(actual_source)s
                        ELSE %(output_source)s
                    END AS source,
                    row_number() OVER (
                        PARTITION BY sr.sensor_id, sr.recorded_at::date, slot.slot_offset_minutes
                        ORDER BY
                            abs(EXTRACT(EPOCH FROM (
                                sr.recorded_at - (sr.recorded_at::date + make_interval(mins => slot.slot_offset_minutes))
                            ))),
                            sr.recorded_at DESC
                    ) AS slot_rank
                FROM sensor_readings sr
                CROSS JOIN unnest(%(daily_slot_offsets_minutes)s::int[]) AS slot(slot_offset_minutes)
                WHERE reading_resolution = %(hourly_resolution)s
                  AND recorded_at < %(hourly_start)s
                  AND recorded_at::date >= %(daily_start_date)s
                  {source_filter}
            ),
            daily AS (
                SELECT *
                FROM ranked
                WHERE slot_rank = 1
            )
            INSERT INTO sensor_readings (
                sensor_id, recorded_at, soil_moisture_pct, air_temperature_c,
                air_humidity_pct, substrate_temperature_c, source, reading_resolution,
                sample_count
            )
            SELECT
                sensor_id,
                bucket_date + make_interval(mins => bucket_slot_offset_minutes),
                soil_moisture_pct,
                air_temperature_c,
                air_humidity_pct,
                substrate_temperature_c,
                source,
                %(daily_resolution)s,
                sample_count
            FROM daily
            ON CONFLICT (sensor_id, recorded_at) DO UPDATE SET
                soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                air_temperature_c = EXCLUDED.air_temperature_c,
                air_humidity_pct = EXCLUDED.air_humidity_pct,
                substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                source = EXCLUDED.source,
                reading_resolution = EXCLUDED.reading_resolution,
                sample_count = EXCLUDED.sample_count,
                changed_at = NULL
            """,
            params,
        ).rowcount
        raw_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(raw_resolution)s
              AND recorded_at < %(raw_start)s
              {source_filter}
            """,
            params,
        ).rowcount
        hourly_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(hourly_resolution)s
              AND recorded_at < %(hourly_start)s
              {source_filter}
            """,
            params,
        ).rowcount
        daily_deleted = conn.execute(
            f"""
            DELETE FROM sensor_readings
            WHERE reading_resolution = %(daily_resolution)s
              AND recorded_at::date < %(daily_start_date)s
              {source_filter}
            """,
            params,
        ).rowcount
        conn.commit()

    return {
        "source": source,
        "raw_start": period["raw_start"].isoformat(),
        "hourly_start": period["hourly_start"].isoformat(),
        "daily_start_date": period["daily_start"].date().isoformat(),
        "hourly_upserted": hourly_upserted,
        "daily_from_raw_upserted": daily_from_raw,
        "daily_from_hourly_upserted": daily_from_hourly,
        "raw_deleted": raw_deleted,
        "hourly_deleted": hourly_deleted,
        "daily_deleted": daily_deleted,
    }


def _daily_slot_offsets_minutes() -> list[int]:
    return [slot.hour * 60 + slot.minute for slot in DAILY_READING_TIMES]
