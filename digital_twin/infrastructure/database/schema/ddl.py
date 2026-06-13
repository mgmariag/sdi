from __future__ import annotations


def _schema_is_current(conn) -> bool:
    row = conn.execute(
        """
        SELECT
            to_regclass('public.pots') IS NOT NULL AS has_pots,
            to_regclass('public.sensor_readings') IS NOT NULL AS has_sensor_readings,
            to_regclass('public.weather_hourly') IS NOT NULL AS has_weather_hourly,
            to_regclass('public.sensor_location_recommendations') IS NOT NULL AS has_sensor_locations,
            to_regclass('public.anfis_models') IS NOT NULL AS has_anfis_models,
            to_regclass('public.experiment_runs') IS NOT NULL AS has_experiment_runs
        """
    ).fetchone()
    if not row or not all(row):
        return False

    detail = conn.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'sensor_readings'::regclass
                  AND conname = 'sensor_readings_pkey'
                  AND pg_get_constraintdef(oid) = 'PRIMARY KEY (sensor_id, recorded_at)'
            ) AS has_sensor_primary_key,
            NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'sensor_readings'
                  AND column_name = 'id'
            ) AS sensor_id_column_removed,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'weather_hourly'
                  AND column_name = 'observed_local_at'
            ) AS has_local_weather_time,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'sensor_location_recommendations'
                  AND column_name = 'sensor_id'
            ) AS has_location_sensor_id,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_events'
                  AND column_name = 'sensor_id'
            ) AS has_event_sensor_id,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_events'
                  AND column_name = 'valve_number'
            ) AS has_event_valve_number,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_events'
                  AND column_name = 'payload'
            ) AS has_event_payload,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_actuations'
                  AND column_name = 'valve_number'
            ) AS has_actuation_valve_number,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_actuations'
                  AND column_name = 'payload'
            ) AS has_actuation_payload,
            NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_events'
                  AND column_name = 'decision_id'
            ) AS event_decision_id_removed,
            NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'irrigation_events'
                  AND column_name = 'pot_id'
            ) AS event_pot_id_removed,
            to_regclass('public.irrigation_decisions') IS NULL AS irrigation_decisions_removed,
            to_regclass('public.alerts') IS NULL AS alerts_removed,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'pots'
                  AND column_name = 'rain_exposure'
            ) AS has_pot_rain_exposure,
            NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'pots'
                  AND column_name = 'default_location'
            ) AS default_location_removed,
            NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'pots'
                  AND column_name = 'winter_location'
            ) AS winter_location_removed,
            to_regclass('public.irrigation_prescriptions') IS NOT NULL AS has_irrigation_prescriptions,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'experiment_runs'
                  AND column_name = 'payload'
            ) AS has_experiment_run_payload,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'experiment_runs'
                  AND column_name = 'started_at'
            ) AS has_experiment_run_started_at,
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'experiment_runs'
                  AND column_name = 'completed_at'
            ) AS has_experiment_run_completed_at,
            EXISTS (SELECT 1 FROM pots LIMIT 1) AS has_seeded_pots
        """
    ).fetchone()
    return bool(detail and all(detail))


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
            balcony_zone TEXT NOT NULL,
            rain_exposure TEXT NOT NULL DEFAULT 'partially_exposed'
                CONSTRAINT pots_rain_exposure_check
                CHECK (rain_exposure IN ('covered', 'partially_exposed', 'fully_exposed')),
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

        CREATE TABLE IF NOT EXISTS weather_hourly (
            id BIGSERIAL PRIMARY KEY,
            location_name TEXT NOT NULL,
            latitude NUMERIC(9, 6) NOT NULL,
            longitude NUMERIC(9, 6) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            observed_local_at TIMESTAMP NOT NULL,
            observed_date DATE NOT NULL,
            observed_hour SMALLINT NOT NULL CHECK (observed_hour BETWEEN 0 AND 23),
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
            UNIQUE (location_name, source, observed_local_at)
        );

        CREATE TABLE IF NOT EXISTS sensor_readings (
            sensor_id BIGINT NOT NULL,
            recorded_at TIMESTAMP NOT NULL,
            soil_moisture_pct NUMERIC(5, 2) NOT NULL,
            air_temperature_c NUMERIC(6, 2),
            air_humidity_pct NUMERIC(5, 2),
            substrate_temperature_c NUMERIC(6, 2),
            source TEXT NOT NULL DEFAULT 'simulation',
            reading_resolution TEXT NOT NULL DEFAULT 'raw_15min',
            sample_count INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'Europe/Bucharest'),
            changed_at TIMESTAMP,
            PRIMARY KEY (sensor_id, recorded_at)
        );

        CREATE TABLE IF NOT EXISTS anfis_models (
            id BIGSERIAL PRIMARY KEY,
            model_key TEXT NOT NULL UNIQUE,
            trained_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            training_start_date DATE,
            training_end_date DATE,
            train_samples INTEGER NOT NULL DEFAULT 0,
            test_samples INTEGER NOT NULL DEFAULT 0,
            fit_samples INTEGER NOT NULL DEFAULT 0,
            calibration_samples INTEGER NOT NULL DEFAULT 0,
            weighted_fit_samples INTEGER NOT NULL DEFAULT 0,
            seed INTEGER,
            generations INTEGER NOT NULL DEFAULT 0,
            population INTEGER NOT NULL DEFAULT 0,
            sensor_source TEXT NOT NULL DEFAULT 'simulated_sensor',
            sensor_reading_count BIGINT NOT NULL DEFAULT 0,
            sensor_readings_max_recorded_at TIMESTAMP,
            model_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            changed_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS sensor_location_recommendations (
            id BIGSERIAL PRIMARY KEY,
            sensor_id BIGINT NOT NULL,
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

        CREATE TABLE IF NOT EXISTS irrigation_events (
            id BIGSERIAL PRIMARY KEY,
            experiment_type TEXT NOT NULL DEFAULT 'baseline',
            sensor_id BIGINT NOT NULL,
            scheduled_start_at TIMESTAMPTZ NOT NULL,
            scheduled_end_at TIMESTAMPTZ NOT NULL,
            flow_rate_ml_min NUMERIC(8, 2) NOT NULL,
            planned_volume_ml NUMERIC(10, 2) NOT NULL,
            valve_number INTEGER,
            valve_zone TEXT,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            cycle_count INTEGER NOT NULL DEFAULT 1,
            soak_pause_min INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'running', 'completed', 'skipped', 'cancelled')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            changed_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS irrigation_actuations (
            id BIGSERIAL PRIMARY KEY,
            event_id BIGINT REFERENCES irrigation_events(id) ON DELETE CASCADE,
            experiment_type TEXT NOT NULL DEFAULT 'baseline',
            pot_id BIGINT NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            scheduled_start_at TIMESTAMPTZ NOT NULL,
            scheduled_end_at TIMESTAMPTZ NOT NULL,
            flow_rate_ml_min NUMERIC(8, 2) NOT NULL,
            planned_volume_ml NUMERIC(10, 2) NOT NULL,
            valve_number INTEGER,
            valve_zone TEXT,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
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

        CREATE TABLE IF NOT EXISTS irrigation_prescriptions (
            id BIGSERIAL PRIMARY KEY,
            experiment_type TEXT NOT NULL,
            prescription_date DATE NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL,
            dispatched_at TIMESTAMPTZ NOT NULL,
            planned_volume_ml NUMERIC(12, 2) NOT NULL DEFAULT 0,
            valve_runs INTEGER NOT NULL DEFAULT 0,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'dispatched'
                CHECK (status IN ('draft', 'dispatched', 'cancelled')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            changed_at TIMESTAMPTZ,
            UNIQUE (experiment_type, prescription_date)
        );

        CREATE TABLE IF NOT EXISTS experiment_runs (
            id BIGSERIAL PRIMARY KEY,
            experiment_type TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
            summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

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

        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pots_active_id
            ON pots (id)
            WHERE active = true;
        CREATE INDEX IF NOT EXISTS idx_pots_size_id
            ON pots (size_class, id);
        CREATE INDEX IF NOT EXISTS idx_pots_plant_type_id
            ON pots (plant_type_code, id);

        CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_time
            ON sensor_readings (sensor_id, recorded_at);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_recorded_sensor
            ON sensor_readings (source, recorded_at, sensor_id)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_sensor_recorded_desc
            ON sensor_readings (source, sensor_id, recorded_at DESC)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_recorded_desc_sensor
            ON sensor_readings (recorded_at DESC, sensor_id);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_resolution_recorded_sensor
            ON sensor_readings (source, reading_resolution, recorded_at, sensor_id)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c, sample_count);
        CREATE INDEX IF NOT EXISTS idx_sensor_readings_source_sensor_resolution_recorded_desc
            ON sensor_readings (source, sensor_id, reading_resolution, recorded_at DESC)
            INCLUDE (soil_moisture_pct, air_temperature_c, air_humidity_pct, substrate_temperature_c, sample_count);
        CREATE INDEX IF NOT EXISTS idx_anfis_models_key_trained
            ON anfis_models (model_key, trained_at DESC);
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
        CREATE INDEX IF NOT EXISTS idx_weather_hourly_location_local
            ON weather_hourly (location_name, observed_local_at);
        CREATE INDEX IF NOT EXISTS idx_weather_hourly_location_local_date
            ON weather_hourly (location_name, observed_date);
        CREATE INDEX IF NOT EXISTS idx_weather_refresh_runs_date_source
            ON weather_refresh_runs (refresh_date, source);
        CREATE INDEX IF NOT EXISTS idx_weather_refresh_runs_recent
            ON weather_refresh_runs (refresh_date DESC, started_at DESC);

        DROP INDEX IF EXISTS idx_irrigation_decisions_pot_date;
        DROP INDEX IF EXISTS idx_irrigation_decisions_experiment_date_pot;
        DROP INDEX IF EXISTS idx_irrigation_events_pot_start;
        DROP INDEX IF EXISTS uq_irrigation_decisions_experiment_pot_slot;
        DROP INDEX IF EXISTS uq_irrigation_events_experiment_pot_start;

        CREATE INDEX IF NOT EXISTS idx_irrigation_events_sensor_start
            ON irrigation_events (sensor_id, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_events_experiment_planned_start
            ON irrigation_events (experiment_type, scheduled_start_at)
            WHERE status = 'planned';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_irrigation_events_experiment_sensor_start
            ON irrigation_events (experiment_type, sensor_id, scheduled_start_at);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_irrigation_actuations_experiment_pot_start
            ON irrigation_actuations (experiment_type, pot_id, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_due
            ON irrigation_actuations (status, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_valve_start
            ON irrigation_actuations (valve_number, scheduled_start_at);
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_planned_due
            ON irrigation_actuations (scheduled_start_at, id)
            WHERE status = 'planned';
        CREATE INDEX IF NOT EXISTS idx_irrigation_actuations_experiment_planned_start
            ON irrigation_actuations (experiment_type, scheduled_start_at)
            WHERE status = 'planned';
        CREATE INDEX IF NOT EXISTS idx_irrigation_prescriptions_date
            ON irrigation_prescriptions (prescription_date, experiment_type);
        CREATE INDEX IF NOT EXISTS idx_experiment_runs_type_range_created
            ON experiment_runs (experiment_type, start_date, end_date, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_experiment_runs_created
            ON experiment_runs (created_at DESC);
        """
    )
