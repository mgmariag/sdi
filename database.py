import random
import time
from contextlib import contextmanager
from datetime import time as time_of_day
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from digital_twin.core.config import get_settings


DEFAULT_DATABASE_URL = "postgresql://dt_user:dt_password@localhost:5432/digital_twin"
DEFAULT_POT_COUNT = 200
DEFAULT_SEED = 2026


def get_database_url() -> str:
    return get_settings().database_url


@contextmanager
def get_connection(row_factory=None):
    with psycopg.connect(get_database_url(), row_factory=row_factory) as conn:
        yield conn


def wait_for_database(max_attempts: int = 20, delay_seconds: float = 1.0) -> None:
    last_error = None
    for _ in range(max_attempts):
        try:
            with get_connection() as conn:
                conn.execute("SELECT 1")
                return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError("Database did not become available") from last_error


def initialize_database(pot_count: int = DEFAULT_POT_COUNT) -> None:
    wait_for_database()
    with get_connection() as conn:
        create_schema(conn)
        seed_reference_data(conn)
        seed_pots(conn, target_count=pot_count)
        sync_generated_pot_flow_rates(conn, target_count=pot_count)
        conn.commit()


def create_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plant_types (
            code TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            water_need_level TEXT NOT NULL CHECK (water_need_level IN ('low', 'medium', 'high')),
            moisture_min_pct NUMERIC(5, 2) NOT NULL,
            moisture_target_pct NUMERIC(5, 2) NOT NULL,
            moisture_max_pct NUMERIC(5, 2) NOT NULL,
            winter_moisture_target_pct NUMERIC(5, 2) NOT NULL DEFAULT 15,
            heat_sensitive BOOLEAN NOT NULL DEFAULT FALSE,
            allows_second_watering BOOLEAN NOT NULL DEFAULT FALSE,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS pot_size_profiles (
            code TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            small_subtype TEXT,
            diameter_cm NUMERIC(6, 2),
            volume_l NUMERIC(7, 2),
            base_drip_flow_ml_min NUMERIC(8, 2) NOT NULL,
            evaporation_factor NUMERIC(5, 2) NOT NULL,
            retention_factor NUMERIC(5, 2) NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pots (
            id BIGSERIAL PRIMARY KEY,
            pot_code TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            size_class TEXT NOT NULL CHECK (size_class IN ('huge', 'large', 'medium', 'small')),
            small_subtype TEXT,
            plant_type_code TEXT NOT NULL REFERENCES plant_types(code),
            default_location TEXT NOT NULL CHECK (default_location IN ('outdoor', 'indoor')),
            winter_location TEXT NOT NULL CHECK (winter_location IN ('outdoor', 'indoor')),
            balcony_zone TEXT NOT NULL,
            sun_exposure TEXT NOT NULL CHECK (sun_exposure IN ('shade', 'partial', 'full', 'reflected_heat')),
            wind_exposure TEXT NOT NULL CHECK (wind_exposure IN ('sheltered', 'moderate', 'gusty')),
            container_material TEXT NOT NULL,
            soil_profile TEXT NOT NULL,
            drip_flow_ml_min NUMERIC(8, 2) NOT NULL,
            cycle_soak_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            morning_window_start TIME NOT NULL DEFAULT '06:00',
            morning_window_end TIME NOT NULL DEFAULT '09:00',
            evening_window_start TIME NOT NULL DEFAULT '17:00',
            evening_window_end TIME NOT NULL DEFAULT '19:00',
            moisture_min_pct NUMERIC(5, 2) NOT NULL,
            moisture_target_pct NUMERIC(5, 2) NOT NULL,
            moisture_max_pct NUMERIC(5, 2) NOT NULL,
            winter_moisture_target_pct NUMERIC(5, 2) NOT NULL DEFAULT 15,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE pots ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

        CREATE TABLE IF NOT EXISTS weather_hourly (
            id BIGSERIAL PRIMARY KEY,
            location_name TEXT NOT NULL,
            latitude NUMERIC(9, 6) NOT NULL,
            longitude NUMERIC(9, 6) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            source TEXT NOT NULL,
            temperature_c NUMERIC(6, 2),
            relative_humidity_pct NUMERIC(5, 2),
            precipitation_mm NUMERIC(7, 2),
            wind_speed_kmh NUMERIC(7, 2),
            wind_gust_kmh NUMERIC(7, 2),
            cloud_cover_pct NUMERIC(5, 2),
            apparent_temperature_c NUMERIC(6, 2),
            is_day BOOLEAN,
            precipitation_probability_pct NUMERIC(5, 2),
            evapotranspiration_mm NUMERIC(7, 3),
            rain_mm NUMERIC(7, 2),
            showers_mm NUMERIC(7, 2),
            snowfall_cm NUMERIC(7, 2),
            weather_code INTEGER,
            pressure_msl_hpa NUMERIC(7, 2),
            surface_pressure_hpa NUMERIC(7, 2),
            wind_direction_10m_deg NUMERIC(6, 2),
            soil_temperature_0cm_c NUMERIC(6, 2),
            soil_temperature_6cm_c NUMERIC(6, 2),
            soil_moisture_0_to_1cm NUMERIC(7, 4),
            soil_moisture_1_to_3cm NUMERIC(7, 4),
            shortwave_radiation_w_m2 NUMERIC(9, 2),
            raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            changed_at TIMESTAMPTZ,
            UNIQUE (location_name, source, observed_at)
        );
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'weather_hourly'
                  AND column_name = 'is_forecast'
            ) THEN
                DELETE FROM weather_hourly wh
                USING (
                    SELECT id
                    FROM (
                        SELECT
                            id,
                            row_number() OVER (
                                PARTITION BY location_name, source, observed_at
                                ORDER BY is_forecast ASC, id DESC
                            ) AS duplicate_rank
                        FROM weather_hourly
                    ) ranked
                    WHERE duplicate_rank > 1
                ) duplicate_rows
                WHERE wh.id = duplicate_rows.id;
            END IF;
        END $$;
        ALTER TABLE weather_hourly DROP CONSTRAINT IF EXISTS weather_hourly_location_name_source_observed_at_is_forecast_key;
        ALTER TABLE weather_hourly DROP COLUMN IF EXISTS is_forecast;
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'weather_hourly_location_name_source_observed_at_key'
            ) THEN
                ALTER TABLE weather_hourly
                ADD CONSTRAINT weather_hourly_location_name_source_observed_at_key
                UNIQUE (location_name, source, observed_at);
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS balcony_microclimate_hourly (
            id BIGSERIAL PRIMARY KEY,
            weather_hourly_id BIGINT REFERENCES weather_hourly(id) ON DELETE SET NULL,
            balcony_zone TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            temperature_c NUMERIC(6, 2),
            relative_humidity_pct NUMERIC(5, 2),
            wind_speed_kmh NUMERIC(7, 2),
            sun_exposure TEXT NOT NULL,
            reflected_heat_delta_c NUMERIC(5, 2) NOT NULL DEFAULT 0,
            evaporation_index NUMERIC(6, 3) NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (balcony_zone, observed_at)
        );

        CREATE TABLE IF NOT EXISTS sensor_readings (
            id BIGSERIAL PRIMARY KEY,
            pot_id BIGINT NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            recorded_at TIMESTAMPTZ NOT NULL,
            soil_moisture_pct NUMERIC(5, 2) NOT NULL,
            air_temperature_c NUMERIC(6, 2),
            air_humidity_pct NUMERIC(5, 2),
            substrate_temperature_c NUMERIC(6, 2),
            source TEXT NOT NULL DEFAULT 'simulation',
            reading_resolution TEXT NOT NULL DEFAULT 'raw_15min',
            sample_count INTEGER NOT NULL DEFAULT 1,
            weather_observed_at TIMESTAMPTZ,
            weather_source TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            changed_at TIMESTAMPTZ,
            UNIQUE (pot_id, recorded_at, source, reading_resolution)
        );

        ALTER TABLE sensor_readings
            ADD COLUMN IF NOT EXISTS reading_resolution TEXT NOT NULL DEFAULT 'raw_15min';
        ALTER TABLE sensor_readings
            ADD COLUMN IF NOT EXISTS sample_count INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE sensor_readings
            ADD COLUMN IF NOT EXISTS weather_observed_at TIMESTAMPTZ;
        ALTER TABLE sensor_readings
            ADD COLUMN IF NOT EXISTS weather_source TEXT;
        ALTER TABLE sensor_readings
            ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;
        ALTER TABLE sensor_readings
            DROP CONSTRAINT IF EXISTS sensor_readings_pot_id_recorded_at_source_key;
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'sensor_readings_pot_id_recorded_at_source_reading_resolution_key'
            ) THEN
                ALTER TABLE sensor_readings
                ADD CONSTRAINT sensor_readings_pot_id_recorded_at_source_reading_resolution_key
                UNIQUE (pot_id, recorded_at, source, reading_resolution);
            END IF;
        END $$;

        DO $$
        BEGIN
            IF to_regclass('sensor_readings_hourly') IS NOT NULL THEN
                INSERT INTO sensor_readings (
                    pot_id,
                    recorded_at,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    source,
                    reading_resolution,
                    sample_count,
                    created_at,
                    changed_at
                )
                SELECT
                    pot_id,
                    bucket_start_at,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    source,
                    'hourly',
                    sample_count,
                    created_at,
                    changed_at
                FROM sensor_readings_hourly
                ON CONFLICT (pot_id, recorded_at, source, reading_resolution) DO UPDATE SET
                    soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                    air_temperature_c = EXCLUDED.air_temperature_c,
                    air_humidity_pct = EXCLUDED.air_humidity_pct,
                    substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                    sample_count = EXCLUDED.sample_count,
                    changed_at = now();
            END IF;
        END $$;

        DO $$
        BEGIN
            IF to_regclass('sensor_readings_daily') IS NOT NULL THEN
                INSERT INTO sensor_readings (
                    pot_id,
                    recorded_at,
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    source,
                    reading_resolution,
                    sample_count,
                    created_at,
                    changed_at
                )
                SELECT
                    pot_id,
                    (bucket_date + time '12:00') AT TIME ZONE 'Europe/Bucharest',
                    soil_moisture_pct,
                    air_temperature_c,
                    air_humidity_pct,
                    substrate_temperature_c,
                    source,
                    'daily',
                    sample_count,
                    created_at,
                    changed_at
                FROM sensor_readings_daily
                ON CONFLICT (pot_id, recorded_at, source, reading_resolution) DO UPDATE SET
                    soil_moisture_pct = EXCLUDED.soil_moisture_pct,
                    air_temperature_c = EXCLUDED.air_temperature_c,
                    air_humidity_pct = EXCLUDED.air_humidity_pct,
                    substrate_temperature_c = EXCLUDED.substrate_temperature_c,
                    sample_count = EXCLUDED.sample_count,
                    changed_at = now();
            END IF;
        END $$;

        DROP TABLE IF EXISTS sensor_readings_hourly;
        DROP TABLE IF EXISTS sensor_readings_daily;

        CREATE TABLE IF NOT EXISTS sensor_location_recommendations (
            id BIGSERIAL PRIMARY KEY,
            requested_sensor_count INTEGER NOT NULL CHECK (requested_sensor_count > 0),
            rank INTEGER NOT NULL CHECK (rank > 0),
            pot_id BIGINT NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            score NUMERIC(8, 3) NOT NULL,
            reason TEXT NOT NULL,
            criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (rank),
            UNIQUE (pot_id)
        );

        CREATE TABLE IF NOT EXISTS irrigation_decisions (
            id BIGSERIAL PRIMARY KEY,
            experiment_type TEXT NOT NULL DEFAULT 'baseline',
            pot_id BIGINT NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            decided_at TIMESTAMPTZ NOT NULL,
            decision_date DATE NOT NULL,
            decision_slot TEXT NOT NULL CHECK (decision_slot IN ('morning', 'evening', 'midday_alert', 'winter_check')),
            should_irrigate BOOLEAN NOT NULL,
            reason_code TEXT NOT NULL,
            reason_detail TEXT NOT NULL,
            current_moisture_pct NUMERIC(5, 2),
            target_moisture_pct NUMERIC(5, 2),
            weather_hourly_id BIGINT REFERENCES weather_hourly(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE irrigation_decisions ADD COLUMN IF NOT EXISTS experiment_type TEXT NOT NULL DEFAULT 'baseline';
        ALTER TABLE irrigation_decisions ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;

        CREATE TABLE IF NOT EXISTS irrigation_events (
            id BIGSERIAL PRIMARY KEY,
            experiment_type TEXT NOT NULL DEFAULT 'baseline',
            decision_id BIGINT REFERENCES irrigation_decisions(id) ON DELETE SET NULL,
            pot_id BIGINT NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            scheduled_start_at TIMESTAMPTZ NOT NULL,
            scheduled_end_at TIMESTAMPTZ NOT NULL,
            flow_rate_ml_min NUMERIC(8, 2) NOT NULL,
            planned_volume_ml NUMERIC(10, 2) NOT NULL,
            cycle_count INTEGER NOT NULL DEFAULT 1,
            soak_pause_min INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'running', 'completed', 'skipped', 'cancelled')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE irrigation_events ADD COLUMN IF NOT EXISTS experiment_type TEXT NOT NULL DEFAULT 'baseline';
        ALTER TABLE irrigation_events ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;

        CREATE TABLE IF NOT EXISTS alerts (
            id BIGSERIAL PRIMARY KEY,
            experiment_type TEXT NOT NULL DEFAULT 'baseline',
            pot_id BIGINT REFERENCES pots(id) ON DELETE CASCADE,
            raised_at TIMESTAMPTZ NOT NULL,
            alert_type TEXT NOT NULL CHECK (alert_type IN ('emergency_dryness', 'too_wet_too_long', 'freeze_risk', 'sensor_stale', 'runoff_risk')),
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS experiment_type TEXT NOT NULL DEFAULT 'baseline';
        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;

        CREATE TABLE IF NOT EXISTS irrigation_actuations (
            id BIGSERIAL PRIMARY KEY,
            event_id BIGINT REFERENCES irrigation_events(id) ON DELETE CASCADE,
            experiment_type TEXT NOT NULL DEFAULT 'baseline',
            pot_id BIGINT NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            scheduled_start_at TIMESTAMPTZ NOT NULL,
            scheduled_end_at TIMESTAMPTZ NOT NULL,
            flow_rate_ml_min NUMERIC(8, 2) NOT NULL,
            planned_volume_ml NUMERIC(10, 2) NOT NULL,
            cycle_count INTEGER NOT NULL DEFAULT 1,
            soak_pause_min INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'running', 'completed', 'skipped', 'cancelled', 'failed')),
            actuator_node TEXT NOT NULL DEFAULT 'irrigation-actuator',
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            delivered_volume_ml NUMERIC(10, 2),
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            changed_at TIMESTAMPTZ
        );

        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS apparent_temperature_c NUMERIC(6, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS is_day BOOLEAN;
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS precipitation_probability_pct NUMERIC(5, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS evapotranspiration_mm NUMERIC(7, 3);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS rain_mm NUMERIC(7, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS showers_mm NUMERIC(7, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS snowfall_cm NUMERIC(7, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS weather_code INTEGER;
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS pressure_msl_hpa NUMERIC(7, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS surface_pressure_hpa NUMERIC(7, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS wind_direction_10m_deg NUMERIC(6, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS soil_temperature_0cm_c NUMERIC(6, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS soil_temperature_6cm_c NUMERIC(6, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS soil_moisture_0_to_1cm NUMERIC(7, 4);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS soil_moisture_1_to_3cm NUMERIC(7, 4);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS shortwave_radiation_w_m2 NUMERIC(9, 2);
        ALTER TABLE weather_hourly ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;

        CREATE TABLE IF NOT EXISTS weather_refresh_runs (
            id BIGSERIAL PRIMARY KEY,
            refresh_date DATE NOT NULL,
            source TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            inserted_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            skipped_existing_observed_count INTEGER NOT NULL DEFAULT 0,
            error_detail TEXT,
            UNIQUE (refresh_date, source)
        );

        DELETE FROM irrigation_decisions d
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY experiment_type, pot_id, decided_at, decision_slot
                        ORDER BY id DESC
                    ) AS duplicate_rank
                FROM irrigation_decisions
            ) ranked
            WHERE duplicate_rank > 1
        ) duplicate_rows
        WHERE d.id = duplicate_rows.id;

        DELETE FROM irrigation_events e
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY experiment_type, pot_id, scheduled_start_at
                        ORDER BY id DESC
                    ) AS duplicate_rank
                FROM irrigation_events
            ) ranked
            WHERE duplicate_rank > 1
        ) duplicate_rows
        WHERE e.id = duplicate_rows.id;

        DELETE FROM alerts a
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY experiment_type, pot_id, raised_at, alert_type
                        ORDER BY id DESC
                    ) AS duplicate_rank
                FROM alerts
            ) ranked
            WHERE duplicate_rank > 1
        ) duplicate_rows
        WHERE a.id = duplicate_rows.id;

        CREATE INDEX IF NOT EXISTS idx_pots_active_id
            ON pots (id)
            WHERE active = true;
        CREATE INDEX IF NOT EXISTS idx_pots_size_id
            ON pots (size_class, id);
        CREATE INDEX IF NOT EXISTS idx_pots_plant_type_id
            ON pots (plant_type_code, id);

        CREATE INDEX IF NOT EXISTS idx_sensor_readings_pot_time
            ON sensor_readings (pot_id, recorded_at);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_recorded_pot
            ON sensor_readings (source, recorded_at, pot_id)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_pot_recorded_desc
            ON sensor_readings (source, pot_id, recorded_at DESC)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_recorded_desc_pot
            ON sensor_readings (recorded_at DESC, pot_id);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_resolution_recorded_pot
            ON sensor_readings (source, reading_resolution, recorded_at, pot_id)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c, sample_count);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_pot_resolution_recorded_desc
            ON sensor_readings (source, pot_id, reading_resolution, recorded_at DESC)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c, sample_count, weather_observed_at, weather_source);
        CREATE INDEX IF NOT EXISTS idx_sensor_location_recommendations_rank
            ON sensor_location_recommendations (rank);
        CREATE INDEX IF NOT EXISTS idx_sensor_location_recommendations_pot
            ON sensor_location_recommendations (pot_id);

        CREATE INDEX IF NOT EXISTS idx_weather_hourly_time
            ON weather_hourly (observed_at);
        CREATE INDEX IF NOT EXISTS idx_weather_hourly_location_time
            ON weather_hourly (location_name, observed_at);
        CREATE INDEX IF NOT EXISTS idx_weather_hourly_location_observed_source
            ON weather_hourly (location_name, observed_at, source);
        CREATE INDEX IF NOT EXISTS idx_weather_hourly_location_local_date
            ON weather_hourly (location_name, ((observed_at AT TIME ZONE 'Europe/Bucharest')::date));
        CREATE INDEX IF NOT EXISTS idx_weather_refresh_runs_date_source
            ON weather_refresh_runs (refresh_date, source);
        CREATE INDEX IF NOT EXISTS idx_weather_refresh_runs_recent
            ON weather_refresh_runs (refresh_date DESC, started_at DESC);

        CREATE INDEX IF NOT EXISTS idx_irrigation_decisions_pot_date
            ON irrigation_decisions (pot_id, decision_date);
        CREATE INDEX IF NOT EXISTS idx_irrigation_decisions_experiment_date_pot
            ON irrigation_decisions (experiment_type, decision_date, pot_id);
        CREATE INDEX IF NOT EXISTS idx_irrigation_events_pot_start
            ON irrigation_events (pot_id, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_events_experiment_planned_start
            ON irrigation_events (experiment_type, scheduled_start_at)
            WHERE status = 'planned';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_irrigation_decisions_experiment_pot_slot
            ON irrigation_decisions (experiment_type, pot_id, decided_at, decision_slot);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_irrigation_events_experiment_pot_start
            ON irrigation_events (experiment_type, pot_id, scheduled_start_at);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_experiment_pot_type_time
            ON alerts (experiment_type, pot_id, raised_at, alert_type);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_irrigation_actuations_experiment_pot_start
            ON irrigation_actuations (experiment_type, pot_id, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_due
            ON irrigation_actuations (status, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_planned_due
            ON irrigation_actuations (scheduled_start_at, id)
            WHERE status = 'planned';
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_experiment_planned_start
            ON irrigation_actuations (experiment_type, scheduled_start_at)
            WHERE status = 'planned';
        CREATE INDEX IF NOT EXISTS idx_alerts_pot_time
            ON alerts (pot_id, raised_at);
        CREATE INDEX IF NOT EXISTS idx_alerts_experiment_time
            ON alerts (experiment_type, raised_at, pot_id);
        """
    )


def seed_reference_data(conn) -> None:
    plant_types = [
        (
            "vegetables",
            "Vegetables",
            "high",
            32,
            55,
            78,
            15,
            True,
            True,
            "Consistent moisture; likely candidate for second evening watering in heatwaves.",
        ),
        (
            "herbs",
            "Herbs",
            "medium",
            28,
            50,
            74,
            15,
            True,
            True,
            "Most culinary herbs prefer morning watering and can need evening checks in hot wind.",
        ),
        (
            "ornamentals",
            "Ornamentals",
            "medium",
            24,
            45,
            72,
            15,
            False,
            False,
            "Container ornamentals usually tolerate one morning watering unless exposed.",
        ),
        (
            "succulents",
            "Succulents",
            "low",
            12,
            25,
            45,
            15,
            False,
            False,
            "Drought tolerant; water less frequently and avoid prolonged wet soil.",
        ),
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO plant_types (
                code, label, water_need_level, moisture_min_pct, moisture_target_pct,
                moisture_max_pct, winter_moisture_target_pct, heat_sensitive,
                allows_second_watering, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                label = EXCLUDED.label,
                water_need_level = EXCLUDED.water_need_level,
                moisture_min_pct = EXCLUDED.moisture_min_pct,
                moisture_target_pct = EXCLUDED.moisture_target_pct,
                moisture_max_pct = EXCLUDED.moisture_max_pct,
                winter_moisture_target_pct = EXCLUDED.winter_moisture_target_pct,
                heat_sensitive = EXCLUDED.heat_sensitive,
                allows_second_watering = EXCLUDED.allows_second_watering,
                notes = EXCLUDED.notes
            """,
            plant_types,
        )

    size_profiles = [
        ("huge", "Huge planter", None, 70, 90, 30, 0.75, 1.35),
        ("large", "Large pot", None, 45, 45, 24, 0.9, 1.18),
        ("medium", "Medium pot", None, 30, 20, 18, 1.0, 1.0),
        ("small_7cm", "Small pot 7 cm", "7cm", 7, 0.4, 4, 1.9, 0.45),
        ("small_15cm", "Small pot 15 cm", "15cm", 15, 2.2, 8, 1.55, 0.62),
        ("small_30cm", "Small pot 30 cm", "30cm", 30, 12, 14, 1.18, 0.88),
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO pot_size_profiles (
                code, label, small_subtype, diameter_cm, volume_l,
                base_drip_flow_ml_min, evaporation_factor, retention_factor
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                label = EXCLUDED.label,
                small_subtype = EXCLUDED.small_subtype,
                diameter_cm = EXCLUDED.diameter_cm,
                volume_l = EXCLUDED.volume_l,
                base_drip_flow_ml_min = EXCLUDED.base_drip_flow_ml_min,
                evaporation_factor = EXCLUDED.evaporation_factor,
                retention_factor = EXCLUDED.retention_factor
            """,
            size_profiles,
        )


def seed_pots(conn, target_count: int = DEFAULT_POT_COUNT, seed: int = DEFAULT_SEED) -> int:
    existing_count = conn.execute("SELECT count(*) FROM pots").fetchone()[0]
    if existing_count >= target_count:
        return 0

    rng = random.Random(seed)
    profiles = _load_size_profiles(conn)
    plant_types = _load_plant_types(conn)
    generated = [_generate_pot(i, rng, profiles, plant_types) for i in range(1, target_count + 1)]

    inserted = 0
    for pot in generated:
        result = conn.execute(
            """
            INSERT INTO pots (
                pot_code, label, size_class, small_subtype, plant_type_code,
                default_location, winter_location, balcony_zone, sun_exposure,
                wind_exposure, container_material, soil_profile, drip_flow_ml_min,
                cycle_soak_enabled, morning_window_start, morning_window_end,
                evening_window_start, evening_window_end, moisture_min_pct,
                moisture_target_pct, moisture_max_pct, winter_moisture_target_pct
            )
            VALUES (
                %(pot_code)s, %(label)s, %(size_class)s, %(small_subtype)s,
                %(plant_type_code)s, %(default_location)s, %(winter_location)s,
                %(balcony_zone)s, %(sun_exposure)s, %(wind_exposure)s,
                %(container_material)s, %(soil_profile)s, %(drip_flow_ml_min)s,
                %(cycle_soak_enabled)s, %(morning_window_start)s, %(morning_window_end)s,
                %(evening_window_start)s, %(evening_window_end)s, %(moisture_min_pct)s,
                %(moisture_target_pct)s, %(moisture_max_pct)s, %(winter_moisture_target_pct)s
            )
            ON CONFLICT (pot_code) DO NOTHING
            RETURNING id
            """,
            pot,
        ).fetchone()
        if result:
            inserted += 1

    return inserted


def sync_generated_pot_flow_rates(conn, target_count: int = DEFAULT_POT_COUNT, seed: int = DEFAULT_SEED) -> int:
    """Refresh generated demo-pot emitter rates after profile changes.

    Only deterministic POT-### seed records are touched. This keeps existing
    demo data realistic without overwriting unrelated/custom pot rows.
    """
    rng = random.Random(seed)
    profiles = _load_size_profiles(conn)
    plant_types = _load_plant_types(conn)
    generated = [_generate_pot(i, rng, profiles, plant_types) for i in range(1, target_count + 1)]

    updated = 0
    for pot in generated:
        result = conn.execute(
            """
            UPDATE pots
            SET drip_flow_ml_min = %(drip_flow_ml_min)s
            WHERE pot_code = %(pot_code)s
              AND pot_code ~ '^POT-[0-9]{3}$'
              AND drip_flow_ml_min IS DISTINCT FROM %(drip_flow_ml_min)s
            RETURNING id
            """,
            pot,
        ).fetchone()
        if result:
            updated += 1
    return updated


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
        by_winter_location = conn.execute(
            """
            SELECT winter_location, count(*) AS count
            FROM pots
            GROUP BY winter_location
            ORDER BY winter_location
            """
        ).fetchall()
        return _json_ready(
            {
                "total": totals["total"],
                "by_size": by_size,
                "by_plant_type": by_plant,
                "by_winter_location": by_winter_location,
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
            p.default_location,
            p.winter_location,
            p.balcony_zone,
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


def _load_size_profiles(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT code, small_subtype, base_drip_flow_ml_min
        FROM pot_size_profiles
        """
    ).fetchall()
    return {row[0]: {"small_subtype": row[1], "base_drip_flow_ml_min": row[2]} for row in rows}


def _load_plant_types(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            code, moisture_min_pct, moisture_target_pct, moisture_max_pct,
            winter_moisture_target_pct, allows_second_watering
        FROM plant_types
        """
    ).fetchall()
    return {
        row[0]: {
            "moisture_min_pct": row[1],
            "moisture_target_pct": row[2],
            "moisture_max_pct": row[3],
            "winter_moisture_target_pct": row[4],
            "allows_second_watering": row[5],
        }
        for row in rows
    }


def _generate_pot(index: int, rng: random.Random, profiles: dict[str, dict[str, Any]], plant_types: dict[str, dict[str, Any]]) -> dict[str, Any]:
    size_class = _weighted_choice(
        rng,
        [
            ("huge", 0.12),
            ("large", 0.22),
            ("medium", 0.26),
            ("small", 0.40),
        ],
    )
    small_subtype = None
    profile_code = size_class
    if size_class == "small":
        small_subtype = _weighted_choice(rng, [("7cm", 0.25), ("15cm", 0.35), ("30cm", 0.40)])
        profile_code = f"small_{small_subtype}"

    plant_type_code = _weighted_choice(
        rng,
        [
            ("vegetables", 0.32),
            ("herbs", 0.28),
            ("ornamentals", 0.28),
            ("succulents", 0.12),
        ],
    )
    plant_type = plant_types[plant_type_code]
    profile = profiles[profile_code]

    sun_exposure = _weighted_choice(
        rng,
        [
            ("full", 0.34),
            ("partial", 0.32),
            ("reflected_heat", 0.20),
            ("shade", 0.14),
        ],
    )
    wind_exposure = _weighted_choice(
        rng,
        [
            ("moderate", 0.46),
            ("sheltered", 0.32),
            ("gusty", 0.22),
        ],
    )
    drip_flow = _adjust_flow(profile["base_drip_flow_ml_min"], plant_type_code, sun_exposure, wind_exposure, rng)
    cycle_soak = size_class in {"huge", "large"} or (sun_exposure == "reflected_heat" and wind_exposure == "gusty")
    if plant_type_code == "succulents":
        cycle_soak = False

    winter_location = "indoor" if index <= 100 else "outdoor"
    default_location = "outdoor" if rng.random() > 0.08 else "indoor"

    return {
        "pot_code": f"POT-{index:03d}",
        "label": _pot_label(index, size_class, small_subtype, plant_type_code),
        "size_class": size_class,
        "small_subtype": small_subtype,
        "plant_type_code": plant_type_code,
        "default_location": default_location,
        "winter_location": winter_location,
        "balcony_zone": _weighted_choice(
            rng,
            [
                ("south_rail", 0.30),
                ("west_wall", 0.22),
                ("east_corner", 0.18),
                ("north_shelter", 0.14),
                ("hanging_row", 0.16),
            ],
        ),
        "sun_exposure": sun_exposure,
        "wind_exposure": wind_exposure,
        "container_material": _weighted_choice(
            rng,
            [
                ("terracotta", 0.34),
                ("plastic", 0.30),
                ("ceramic", 0.22),
                ("fabric", 0.14),
            ],
        ),
        "soil_profile": _soil_profile(plant_type_code),
        "drip_flow_ml_min": drip_flow,
        "cycle_soak_enabled": cycle_soak,
        "morning_window_start": time_of_day(6, 0),
        "morning_window_end": time_of_day(9, 0),
        "evening_window_start": time_of_day(17, 0),
        "evening_window_end": time_of_day(19, 0),
        "moisture_min_pct": plant_type["moisture_min_pct"],
        "moisture_target_pct": plant_type["moisture_target_pct"],
        "moisture_max_pct": plant_type["moisture_max_pct"],
        "winter_moisture_target_pct": plant_type["winter_moisture_target_pct"],
    }


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    total = sum(weight for _, weight in options)
    marker = rng.uniform(0, total)
    cumulative = 0.0
    for value, weight in options:
        cumulative += weight
        if marker <= cumulative:
            return value
    return options[-1][0]


def _adjust_flow(base_flow: Decimal, plant_type_code: str, sun_exposure: str, wind_exposure: str, rng: random.Random) -> Decimal:
    multiplier = Decimal("1.0")
    if plant_type_code == "vegetables":
        multiplier += Decimal("0.12")
    elif plant_type_code == "succulents":
        multiplier -= Decimal("0.25")

    if sun_exposure == "reflected_heat":
        multiplier += Decimal("0.12")
    elif sun_exposure == "shade":
        multiplier -= Decimal("0.08")

    if wind_exposure == "gusty":
        multiplier += Decimal("0.10")
    elif wind_exposure == "sheltered":
        multiplier -= Decimal("0.05")

    jitter = Decimal(str(round(rng.uniform(-0.08, 0.08), 3)))
    flow = Decimal(base_flow) * (multiplier + jitter)
    return flow.quantize(Decimal("0.01"))


def _soil_profile(plant_type_code: str) -> str:
    return {
        "vegetables": "moisture_retentive_container_mix",
        "herbs": "free_draining_organic_mix",
        "ornamentals": "balanced_potting_mix",
        "succulents": "gritty_fast_draining_mix",
    }[plant_type_code]


def _pot_label(index: int, size_class: str, small_subtype: str | None, plant_type_code: str) -> str:
    size = f"{size_class} {small_subtype}" if small_subtype else size_class
    plant = plant_type_code.replace("_", " ")
    return f"{size.title()} {plant.title()} Pot {index:03d}"


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
