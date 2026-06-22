from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from digital_twin.application.sensors.state_rows import (
    SensorStateProjector,
    SensorStateRepository,
)
from digital_twin.application.sensors.placement import SensorPlacementService
from digital_twin.application.sensors.reading_cadence import (
    DAILY_READING_TIMES,
    DAILY_RETENTION_DAYS,
    DEFAULT_HISTORY_START,
    DEFAULT_SENSOR_READING_CADENCE,
    LOCAL_TZ,
    SensorReadingCadence,
)
from digital_twin.domain.sensor import SensorReadingResolution, SensorSource
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database


class SensorReadingGenerator:
    """Generates simulated sensor readings for selected sensor locations."""

    def __init__(
        self,
        cadence: SensorReadingCadence | None = None,
        placement_service: SensorPlacementService | None = None,
        state_repository: SensorStateRepository | None = None,
        state_projector: SensorStateProjector | None = None,
    ) -> None:
        self.cadence = cadence or DEFAULT_SENSOR_READING_CADENCE
        self.placement_service = placement_service or SensorPlacementService()
        self.state_repository = state_repository or SensorStateRepository()
        self.state_projector = state_projector or SensorStateProjector()

    def seed_historical_sensor_readings(
        self,
        start_date: date = DEFAULT_HISTORY_START,
        end_date: date | None = None,
        source: str = SensorSource.DEFAULT.value,
        batch_size: int = 5000,
    ) -> dict[str, Any]:
        """Generate scheduled readings for pots selected as sensor locations."""
        if end_date is None:
            end_date = self.cadence.today_local()
        start_date = max(start_date, end_date - timedelta(days=DAILY_RETENTION_DAYS - 1))
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")

        initialize_database()
        pots = self.state_repository.load_pots()
        weather_rows = self.state_repository.load_weather(start_date, end_date)
        weather_index = 0
        latest_weather: dict[str, Any] | None = None

        states = self.state_projector.initial_sensor_states(pots)
        pending_rows: list[dict[str, Any]] = []
        inserted_or_updated = 0
        total_readings = 0

        current = datetime.combine(start_date, time(0, 0), tzinfo=LOCAL_TZ)
        interval_minutes = self.cadence.reading_interval_minutes()
        interval_hours = interval_minutes / 60.0
        end_dt = (
            datetime.combine(end_date + timedelta(days=1), time(0, 0), tzinfo=LOCAL_TZ)
            - timedelta(minutes=interval_minutes)
        )

        with get_connection() as conn:
            while current <= end_dt:
                while weather_index < len(weather_rows) and local_observed_at(weather_rows[weather_index]) <= current:
                    latest_weather = weather_rows[weather_index]
                    weather_index += 1
                weather = latest_weather or self.state_projector.fallback_weather(current)
                for pot in pots:
                    self.state_projector.apply_hourly_environment(
                        states[pot["id"]],
                        pot,
                        weather,
                        current.date(),
                        hours=interval_hours,
                    )

                for pot in pots:
                    row = self.state_projector.sensor_row(pot, states[pot["id"]], weather, current, source)
                    pending_rows.append(row)
                    self.state_projector.apply_virtual_irrigation_if_due(states[pot["id"]], pot, weather, current)
                    total_readings += 1

                if len(pending_rows) >= batch_size:
                    inserted_or_updated += self.state_repository.upsert_sensor_rows(conn, pending_rows)
                    pending_rows.clear()

                current += timedelta(minutes=interval_minutes)

            if pending_rows:
                inserted_or_updated += self.state_repository.upsert_sensor_rows(conn, pending_rows)
                pending_rows.clear()

            conn.commit()

        return {
            "source": source,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "pot_count": len(pots),
            "reading_interval_minutes": interval_minutes,
            "expected_readings": total_readings,
            "upserted_readings": inserted_or_updated,
            "weather_rows": len(weather_rows),
        }

    def seed_tiered_sensor_readings(
        self,
        end_at: datetime | None = None,
        start_date: date | None = None,
        source: str = SensorSource.DEFAULT.value,
        batch_size: int = 5000,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        """Generate one year of tiered readings for the selected sensor locations."""
        initialize_database()
        placement = self.placement_service.ensure_default_if_missing()
        end_at = self.cadence.align_to_reading_interval(
            self.cadence.as_local(end_at or datetime.now(LOCAL_TZ))
        )
        start_date = start_date or (end_at.date() - timedelta(days=DAILY_RETENTION_DAYS))
        start_at = datetime.combine(start_date, time(0, 0), tzinfo=LOCAL_TZ)
        if end_at < start_at:
            raise ValueError("end_at must not be before start_date")

        pots = self.state_repository.load_pots()
        weather_rows = self.state_repository.load_weather(start_date, end_at.date())
        weather_index = 0
        latest_weather: dict[str, Any] | None = None
        states = self.state_projector.initial_sensor_states(pots)
        for state in states.values():
            state["last_recorded_at"] = start_at

        period = self.cadence.tiered_periods(end_at)
        pending_rows: list[dict[str, Any]] = []
        inserted_or_updated = 0
        deleted_existing = 0
        by_resolution = {SensorReadingResolution.RAW.value: 0, SensorReadingResolution.HOURLY.value: 0, SensorReadingResolution.DAILY.value: 0}

        with get_connection() as conn:
            if replace_existing and pots:
                deleted_existing = conn.execute(
                    """
                    DELETE FROM sensor_readings
                    WHERE source = %(source)s
                      AND recorded_at >= %(start_at)s
                      AND recorded_at <= %(end_at)s
                    """,
                    {
                        "source": source,
                        "start_at": self.cadence.db_timestamp(start_at),
                        "end_at": self.cadence.db_timestamp(end_at),
                    },
                ).rowcount

            current = start_at
            while current <= end_at:
                while weather_index < len(weather_rows) and local_observed_at(weather_rows[weather_index]) <= current:
                    latest_weather = weather_rows[weather_index]
                    weather_index += 1
                weather = latest_weather or self.state_projector.fallback_weather(current)

                for pot in pots:
                    self.state_projector.apply_hourly_environment(
                        states[pot["id"]],
                        pot,
                        weather,
                        current.date(),
                        hours=0.25,
                    )

                resolution = _tiered_resolution_for_time(current, period)
                if resolution:
                    sample_count = _sample_count_for_resolution(resolution)
                    for pot in pots:
                        row = self.state_projector.sensor_row(pot, states[pot["id"]], weather, current, source)
                        row["reading_resolution"] = resolution
                        row["sample_count"] = sample_count
                        pending_rows.append(row)
                        by_resolution[resolution] += 1

                    if len(pending_rows) >= batch_size:
                        inserted_or_updated += self.state_repository.upsert_sensor_rows(conn, pending_rows)
                        pending_rows.clear()

                for pot in pots:
                    self.state_projector.apply_virtual_irrigation_if_due(states[pot["id"]], pot, weather, current)

                current += timedelta(minutes=15)

            if pending_rows:
                inserted_or_updated += self.state_repository.upsert_sensor_rows(conn, pending_rows)
                pending_rows.clear()

            conn.commit()

        return {
            "source": source,
            "start_date": start_date.isoformat(),
            "end_at": end_at.isoformat(),
            "raw_start": period["raw_start"].isoformat(),
            "hourly_start": period["hourly_start"].isoformat(),
            "daily_start": period["daily_start"].isoformat(),
            "pot_count": len(pots),
            "placement_sensor_count": placement.get("sensor_count", 0),
            "deleted_existing": deleted_existing,
            "upserted_readings": inserted_or_updated,
            "expected_readings": sum(by_resolution.values()),
            "by_resolution": by_resolution,
            "weather_rows": len(weather_rows),
        }

    def generate_sensor_readings_at(
        self,
        recorded_at: datetime,
        source: str = SensorSource.DEFAULT.value,
    ) -> dict[str, Any]:
        """Generate one scheduled reading for every selected sensor location."""
        initialize_database()
        self.placement_service.ensure_default_if_missing()
        recorded_at = self.cadence.align_to_reading_interval(self.cadence.as_local(recorded_at))
        pots = self.state_repository.load_pots()
        previous = self.state_repository.load_latest_sensor_states(recorded_at, source)
        weather = self.state_repository.load_latest_weather_at(recorded_at) or self.state_projector.fallback_weather(recorded_at)

        rows = []
        for pot in pots:
            previous_row = previous.get(pot["id"])
            if previous_row:
                state = {
                    "moisture": soil.number(previous_row["soil_moisture_pct"], float(pot["moisture_target_pct"])),
                    "last_recorded_at": self.cadence.as_local(previous_row["recorded_at"]),
                }
                previous_weather = (
                    self.state_repository.load_latest_weather_at(state["last_recorded_at"])
                    or self.state_projector.fallback_weather(state["last_recorded_at"])
                )
                self.state_projector.apply_virtual_irrigation_if_due(
                    state,
                    pot,
                    previous_weather,
                    state["last_recorded_at"],
                )
            else:
                state = self.state_projector.initial_state_for_pot(pot)

            interval_minutes = self.cadence.reading_interval_minutes()
            intervals_elapsed = max(
                1,
                min(
                    24 * 60 // interval_minutes,
                    int((recorded_at - state["last_recorded_at"]).total_seconds() // (interval_minutes * 60)),
                ),
            )
            for _ in range(intervals_elapsed):
                self.state_projector.apply_hourly_environment(
                    state,
                    pot,
                    weather,
                    recorded_at.date(),
                    hours=interval_minutes / 60.0,
                )
            rows.append(self.state_projector.sensor_row(pot, state, weather, recorded_at, source))

        with get_connection() as conn:
            upserted = self.state_repository.upsert_sensor_rows(conn, rows, update_changed_at=source == SensorSource.ACTUAL.value)
            conn.commit()

        return {
            "source": source,
            "recorded_at": recorded_at.isoformat(),
            "pot_count": len(pots),
            "upserted_readings": upserted,
        }

    def generate_due_sensor_readings(
        self,
        now: datetime | None = None,
        source: str = SensorSource.DEFAULT.value,
    ) -> list[dict[str, Any]]:
        """Generate readings for scheduled times that are due today and missing."""
        now = self.cadence.as_local(now or datetime.now(LOCAL_TZ))
        due_times = [item for item in self.cadence.scheduled_datetimes(now.date()) if item <= now]
        if source == SensorSource.ACTUAL.value:
            if not due_times:
                return []
            latest_due = due_times[-1]
            return (
                []
                if self.state_repository.reading_exists(latest_due, source)
                else [self.generate_sensor_readings_at(latest_due, source=source)]
            )

        results = []
        for scheduled_at in due_times:
            if not self.state_repository.reading_exists(scheduled_at, source):
                results.append(self.generate_sensor_readings_at(scheduled_at, source=source))
        return results


def _tiered_resolution_for_time(recorded_at: datetime, period: dict[str, datetime]) -> str | None:
    if period["raw_start"] <= recorded_at <= period["end_at"]:
        return SensorReadingResolution.RAW.value
    if period["hourly_start"] <= recorded_at < period["raw_start"]:
        return SensorReadingResolution.HOURLY.value if recorded_at.minute == 0 else None
    if period["daily_start"] <= recorded_at < period["hourly_start"]:
        return SensorReadingResolution.DAILY.value if recorded_at.time().replace(second=0, microsecond=0) in DAILY_READING_TIMES else None
    return None


def _sample_count_for_resolution(resolution: str) -> int:
    if resolution == SensorReadingResolution.HOURLY.value:
        return 4
    if resolution == SensorReadingResolution.DAILY.value:
        return 1
    return 1
