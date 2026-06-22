# Technical Specification: Digital Twin Irrigation System

## 1. Document Scope

This specification describes the current implementation of the Digital Twin irrigation prototype in this repository. It covers the backend, frontend, database schema, simulation logic, experiment workflows, deployment setup, verification assets, known limitations, and recommended improvements for making the application closer to a realistic irrigation-system digital twin.

The live PostgreSQL database was not queried while preparing this document because Docker Desktop was not running. The database section is therefore based on the source-defined schema under `digital_twin/infrastructure/database/schema/` and the repository access code under `digital_twin/infrastructure/database/repositories/`.

## 2. Product Summary

The project is a master's thesis application for evaluating irrigation decisions through a database-backed digital twin. The implemented system models a balcony/container irrigation installation with:

- A FastAPI backend exposing weather, sensor, overview, experiment, prescription, and actuator endpoints.
- A PostgreSQL database storing pot inventory, weather cache, sensor readings, sensor placement recommendations, irrigation prescriptions, planned events, and simulated actuator records.
- A SAPUI5 frontend for selecting experiment ranges, setting sampling frequency, running irrigation strategies, inspecting charts, and reviewing pot/sensor/valve information.
- Simulation logic that compares a default threshold-based digital-twin controller with sparse sensing, fuzzy digital-twin control, and ANFIS-GA style control.
- Startup hooks and internal backend workers for forecast refresh, generated sensor data, prescription dispatch, and actuator consumption.
- An optional one-shot ANFIS training worker that can be run independently in Docker and persists the trained model in PostgreSQL.

The current system should be described as a realistic prototype and simulation, not as a validated field deployment. It supports actual sensor ingestion through an API, but most data generation paths are synthetic unless real readings are posted to the backend.

## 3. System Context

### 3.1 Actors

- Research user: runs experiments, changes date ranges, compares irrigation strategies, and reviews outputs.
- Digital twin backend: owns the model state, simulation runs, weather cache, sensor lifecycle, prescriptions, and actuation simulation.
- PostgreSQL database: persists reference data, weather, sensors, and irrigation state.
- Open-Meteo service: optional external data source for historical and forecast weather ingestion.
- Sensor node or integration script: optional client that can post actual soil-moisture readings to `/api/sensors/ingest`.
- Simulated actuator node: backend service that consumes due irrigation actuation rows and writes feedback into sensor readings.

### 3.2 Runtime Services

| Service | Port | Technology | Responsibility |
| --- | ---: | --- | --- |
| `frontend` | 8080 | Nginx + built SAPUI5 app | Serves the UI and proxies `/api/*` calls to the backend. |
| `backend` | 8000 | FastAPI + Uvicorn | API routes, database initialization, weather/sensor services, simulation, prescriptions, workers. |
| `anfis-trainer` | n/a | Python worker | Optional Docker Compose profile service that trains ANFIS if sensor readings changed, persists the model, and exits. |
| `postgres` | 5432 | PostgreSQL 16 | Persistent state for pots, weather, sensors, prescriptions, events, actuations. |

The deployed Docker topology is defined in `docker-compose.yml`. Nginx proxies API traffic to `backend:8000` and serves UI5 resources through SAP CDN proxy locations.

## 4. Technology Stack

### 4.1 Backend

- Python 3.12
- FastAPI
- Uvicorn
- psycopg 3 with binary package
- PostgreSQL 16
- Python `unittest` tests

### 4.2 Frontend

- SAPUI5/OpenUI5 application generated from SAP Fiori tooling
- `sap.m`, `sap.ui.core`, `sap.viz`
- JSONModel-based application state
- VizFrame charts for moisture, water usage, weather, ANFIS signals, and fuzzy scores
- UI5 CLI build pipeline

### 4.3 Deployment

- `Dockerfile.backend` builds the backend runtime image.
- `Dockerfile.frontend` builds UI assets with Node 22 and serves them with Nginx.
- `docker-compose.yml` wires frontend, backend, PostgreSQL, and the optional `anfis-training` profile worker.

## 5. Repository Structure

| Path | Purpose |
| --- | --- |
| `digital_twin/api/` | FastAPI app factory and route modules. |
| `digital_twin/application/` | Use cases for experiments, persisted ANFIS models, weather refresh, sensors, actuators, and runtime control loop. |
| `digital_twin/domain/` | Singular domain modules for pots, plants, sensors, valves, irrigation, soil rules, and weather helpers. |
| `digital_twin/infrastructure/` | Environment-backed settings, database connection, schema initialization, repositories, Open-Meteo integration, and schedulers. |
| `digital_twin/simulation/` | Simulation engine, weather model, irrigation controller policies, sensor calibration, valve rollups, and experiment execution. |
| `webapp/` | SAPUI5 frontend, XML views, controller, models, styles, tests. |
| `tests/` | Backend unit tests for architecture, weather, sensors, simulation, prescriptions. |
| `dist/` | Generated frontend build output, not source of truth. |
| `node_modules/` | Installed frontend dependencies, not source of truth. |

## 6. Backend Architecture

### 6.1 Application Startup

The backend app is created in `digital_twin/api/main.py`. On lifespan startup it:

1. Initializes the PostgreSQL schema and seed data.
2. Refreshes weather forecasts when enabled.
3. Prepares tiered simulated sensor data and cleanup when enabled.
4. Starts prescription and actuation schedulers when enabled.
5. Warms the default baseline experiment cache.

Central settings are loaded from environment variables through the frozen `Settings` dataclass in `digital_twin/infrastructure/config.py`.

### 6.2 Layering

The backend follows this effective layering:

1. API routes validate request parameters and translate exceptions to HTTP responses.
2. Services coordinate business workflows and call repositories or simulation functions.
3. Repositories isolate database access and shape read models.
4. Simulation/control modules calculate digital-twin states, decisions, events, and summaries.
5. Database schema and seed functions define persistence structure and demo plant/pot inventory.

Some modules still expose helper functions for simulation performance and testability. The main service and repository boundaries are class-based.

## 7. Configuration

Runtime defaults are defined by `Settings` in `digital_twin/infrastructure/config.py`. The one-shot ANFIS trainer also reads the trainer-specific environment variables listed at the end of this table.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://dt_user:dt_password@localhost:5432/digital_twin` | Backend database connection. |
| `CORS_ORIGINS` | `http://localhost:8080,http://localhost:8081` | Allowed frontend origins. |
| `WEATHER_REFRESH_ON_STARTUP` | `true` | Enables weather refresh at startup. |
| `SENSOR_SOURCE` | `simulated_sensor` | Default generated sensor source. |
| `SENSOR_HISTORY_START` | today minus 29 days | Start date for generated sensor history. |
| `SENSOR_HISTORY_END` | unset | Optional end date override for generated sensor history. |
| `SENSOR_READING_INTERVAL_MINUTES` | `15` | Raw simulated sensor interval. |
| `SENSOR_SEED_HISTORY_ON_STARTUP` | `true` | Seeds tiered sensor history on startup. |
| `SENSOR_SCHEDULER_ENABLED` | `true` | Configuration flag for periodic sensor generation; current API startup prepares sensor data directly. |
| `SENSOR_CLEANUP_ENABLED` | `true` | Aggregates and cleans old raw sensor rows. |
| `SENSOR_CLEANUP_TIME` | `03:15` | Local cleanup time used by the optional sensor scheduler. |
| `PRESCRIPTION_SCHEDULER_ENABLED` | `true` | Enables daily prescription dispatch worker. |
| `PRESCRIPTION_DISPATCH_TIME` | `21:00` | Local time used to dispatch tomorrow's prescriptions. |
| `ACTUATION_SCHEDULER_ENABLED` | `true` | Enables actuation consumption worker. |
| `ACTUATION_POLL_SECONDS` | `60` | Poll interval for due actuation windows. |
| `EXPERIMENT_SNAPSHOT_CACHE_TTL_SECONDS` | `900` | Cache duration for database experiment snapshots. |
| `EXPERIMENT_PRECOMPUTE_RELATED` | `true` | Enables related experiment precompute after a run. |
| `EXPERIMENT_PRECOMPUTE_ANFIS` | `true` | Allows related precompute to include ANFIS tasks. |
| `DEFAULT_ANFIS_PARALLEL_WORKERS` | CPU-based | Worker count for ANFIS processing. |
| `DEFAULT_ANFIS_PARALLEL_BACKEND` | `process` | Parallel execution backend used by ANFIS processing. |
| `ANFIS_GENERATIONS` | `50` | Genetic-training generation count used by the one-shot ANFIS trainer. |
| `ANFIS_POPULATION` | `32` | Genetic-training population size used by the one-shot ANFIS trainer. |
| `ANFIS_TRAIN_FORCE` | `false` | Forces the one-shot ANFIS trainer to retrain even when the sensor watermark is current. |

The default location is Cluj-Napoca, Romania, with timezone `Europe/Bucharest`.

## 8. Database Specification

### 8.1 Schema Initialization

`digital_twin/infrastructure/database/schema/lifecycle.py`, `ddl.py`, and `seeding.py` own schema creation, idempotent initialization, compatibility cleanup, seed reference data, generated pot inventory, and schema-current checks. The backend calls `initialize_database()` on startup.

### 8.2 Tables

| Table | Main Responsibility |
| --- | --- |
| `plant_types` | Reference data for plant categories, water needs, moisture thresholds, winter targets, heat sensitivity. |
| `pot_size_profiles` | Reference data for size, diameter, volume, base drip flow, evaporation, and retention factors. |
| `pots` | Active container inventory with plant type, size, zone, exposure, material, soil profile, flow rate, water windows, moisture targets. |
| `weather_hourly` | Hourly weather observations/forecasts for Cluj-Napoca, including temperature, humidity, precipitation, wind, pressure, soil fields, radiation, raw payload. |
| `sensor_readings` | Soil moisture and microclimate readings keyed by `(sensor_id, recorded_at)`, with source and resolution. |
| `anfis_models` | Latest persisted ANFIS controller payload, training metrics, configuration, and sensor-reading watermark. |
| `sensor_location_recommendations` | Persisted recommended sensor locations, rank, score, reason, and valve-zone criteria. |
| `irrigation_events` | Planned/running/completed irrigation event rows generated from prescriptions. |
| `irrigation_actuations` | Physical or simulated actuator work items linked to events and pots. |
| `irrigation_prescriptions` | Daily dispatched prescription payloads per experiment type. |
| `experiment_runs` | Optional experiment result snapshots with range, execution timing, parameters, summaries, and payloads for reproducibility. |
| `weather_refresh_runs` | Forecast/archive refresh audit rows with inserted/updated/unchanged counts and errors. |

The schema intentionally drops legacy `irrigation_decisions` and `alerts` tables. Experiment decisions and alerts are returned as computed outputs and can be preserved inside selected `experiment_runs` snapshots rather than persisted as normalized primary database tables.

### 8.3 Seed Data

The initializer seeds:

- Four plant types: vegetables, herbs, ornamentals, succulents.
- Six pot size profiles: huge, large, medium, and small 7 cm, 15 cm, 30 cm.
- A deterministic default pot inventory of 200 pots using seed `2026`.

Generated pots include realistic variation in size, plant type, balcony zone, rain exposure, sun exposure, wind exposure, material, soil profile, flow rate, cycle-soak behavior, and watering windows.

### 8.4 Indexes and Constraints

The schema defines indexes for:

- Active pot and pot grouping reads.
- Sensor reads by source, sensor, resolution, and recorded time.
- Weather reads by location, UTC/local observed time, date, and source.
- Weather refresh audit lookup.
- Sensor placement rank and pot lookup.
- Irrigation events by sensor/start and experiment/status.
- Actuations by due status/start, valve/start, and experiment/status.
- Prescription date and experiment lookup.

Unique constraints protect weather hourly identity, ranked/pot sensor recommendations, event scheduling, actuation scheduling, and one prescription per experiment/date.

## 9. API Contract

### 9.1 System, Overview, Pots

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Root health-style message. |
| `GET` | `/api/hello` | UI connection message. |
| `GET` | `/api/db/health` | Database name, user, version, and pot count. |
| `GET` | `/api/overview` | Dashboard read model with current moisture, rain, recommendations, valve plan, plant overview. |
| `GET` | `/api/pots/summary` | Pot totals by size and plant type. |
| `GET` | `/api/pots` | Paginated pot list with optional size and plant filters. |

### 9.2 Weather

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/weather/cluj-napoca/summary` | Weather cache summary and availability. |
| `GET` | `/api/weather/cluj-napoca/hourly` | Hourly weather rows for a date range. |
| `POST` | `/api/weather/cluj-napoca/cache` | Cache Open-Meteo data by year range. |
| `POST` | `/api/weather/cluj-napoca/cache-range` | Cache Open-Meteo data by date range. |
| `POST` | `/api/weather/cluj-napoca/refresh-forecast` | Refresh forecast once per day, or force refresh. |
| `POST` | `/api/weather/cluj-napoca/import-csv` | Import a server-local Open-Meteo CSV into the weather cache. |

### 9.3 Sensors

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/sensors/summary` | Sensor coverage and reading summary. |
| `POST` | `/api/sensors/cleanup` | Aggregate and cleanup old sensor readings. |
| `POST` | `/api/sensors/ingest` | Ingest actual sensor readings. |
| `GET` | `/api/sensors/placements` | Current sensor placement plan. |
| `POST` | `/api/sensors/placements/recommend` | Score and replace sensor placement recommendations. |
| `POST` | `/api/sensors/placements/ensure` | Ensure a valid placement plan exists. |
| `POST` | `/api/sensors/seed` | Seed simulated sensor history. |
| `POST` | `/api/sensors/run-due` | Generate due simulated sensor readings. |
| `POST` | `/api/sensors/run-at` | Generate simulated readings for a requested timestamp. |

### 9.4 Experiments and Actuation

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/experiment` | Run default digital-twin control. |
| `GET` | `/api/experiment/sampling` | Run sparse-sensing experiment. |
| `GET` | `/api/experiment/anfis` | Run ANFIS-GA experiment. |
| `GET` | `/api/experiment/fuzzy` | Run fuzzy digital-twin experiment. |
| `POST` | `/api/experiment/precompute` | Schedule related experiment precomputation. |
| `GET` | `/api/experiment/runs` | List persisted experiment result snapshots. |
| `GET` | `/api/experiment/runs/{run_id}` | Read one persisted experiment result snapshot. |
| `POST` | `/api/control-loop/prescriptions/prepare` | Build tomorrow's runtime prescriptions. |
| `POST` | `/api/control-loop/prescriptions/dispatch` | Persist tomorrow's prescriptions. |
| `POST` | `/api/control-loop/actuations/run-due` | Materialize and consume due actuation windows. |
| `GET` | `/api/control-loop/actuations/summary` | Today's actuation and prescription summary. |

## 10. Core Workflows

### 10.1 Weather Ingestion

The weather service reads and writes the `weather_hourly` cache. It can:

1. Import weather from Open-Meteo archive and forecast APIs.
2. Import Open-Meteo CSV files.
3. Fill missing local hourly buckets with synthetic interpolated rows.
4. Track refresh runs in `weather_refresh_runs`.
5. Expose hourly rows and summary availability to the UI and simulation engine.

For future dates beyond available stored weather, the simulation layer estimates weather rows so experiments can still run into the forecast horizon.

### 10.2 Sensor Lifecycle

The sensor service supports three important source categories:

- `simulated_sensor`: generated readings used by default.
- `actual_sensor`: external readings posted to `/api/sensors/ingest`.
- actuator feedback source: generated after simulated actuator completion.

Sensor readings are generated at 15-minute resolution and then tiered/aggregated for storage efficiency. Experiment loading can combine actual, simulated, and feedback sources depending on availability.

### 10.3 Sensor Placement

`SensorPlacementService` scores active pots and persists representative locations. The default minimum sensor count is tied to the configured valve count, currently five valve zones:

1. `west_wall`
2. `south_rail`
3. `east_corner`
4. `north_shelter`
5. `hanging_row`

The placement algorithm prioritizes evaporative stress and representativeness using sun exposure, pot size, wind exposure, plant water need, material, rain exposure, heat sensitivity, evaporation factor, and low retention. It also enforces coverage across valve zones.

### 10.4 Experiment Execution

The experiment service resolves the date range, loads a cached database snapshot, and runs one of the experiment engines.

Common snapshot contents include:

- Active pots and pot profiles.
- Weather rows and day profiles.
- Sensor context and selected readings.
- Initial per-pot moisture states.
- Counts of estimated weather rows.

The simulation result contains entries, chart entries, pot details, sample events, alerts, summary metrics, valve rollups, and metadata for chart granularity and source coverage.

### 10.5 Prescription and Actuation

Experiment outputs can be converted into runtime prescriptions. The prescription store keeps the latest in-memory prescription per experiment and can persist daily prescription rows.
Executed experiments can also be saved as append-only `experiment_runs` snapshots. These snapshots store the experiment range, runtime start/end timestamps, parameters, summary metrics, and the JSON result payload so thesis figures and tables can be traced back to the exact application output without reintroducing normalized per-decision tables.

The actuation service then:

1. Reads due baseline prescriptions for today.
2. Materializes prescription events into `irrigation_events`.
3. Creates due `irrigation_actuations`.
4. Marks completed/failed actuator rows.
5. Applies completed actuation feedback into sensor state.

Only baseline prescriptions are currently materialized as physical actuation work through `PHYSICAL_ACTUATION_EXPERIMENT_TYPE = "baseline"`.

## 11. Simulation and Control Specification

### 11.1 Default Digital-Twin Control

The default strategy uses a threshold-based decision rule with realistic modifiers:

- Morning watering at 06:00.
- Evening watering on hot days at 18:00.
- Winter check during dormant months.
- Per-pot target and minimum moisture.
- Rain skip/reduction policy with rain exposure factors.
- Freeze-risk and winter watering restrictions.
- Plant type, heat sensitivity, sun exposure, wind exposure, pot size, and cadence rules.
- Valve-zone grouping and planned volume distribution.

The baseline acts as both a practical controller and the reference strategy for comparison.

### 11.2 Sparse-Sensing Experiment

The sampling experiment compares the baseline with a sparse sensor-update schedule. It estimates moisture for non-sampled pots from selected sensor locations, associations, and forecast state propagation.

Key metrics include:

- Baseline vs sparse water usage.
- Accuracy and mismatch counts.
- Missed/extra valve runs.
- Moisture estimation MAE, bias, max error, and refresh counts.
- Sensor association distance and selected sensor coverage.

### 11.3 Fuzzy Digital Twin Experiment

The fuzzy experiment computes irrigation prescriptions from three primary inputs:

- Soil moisture percentage.
- Temperature.
- Rain amount.

It also applies derived Digital Twin context indices as prescription modifiers:

- `drying_demand_index`, derived from temperature, humidity, wind, and radiation.
- `container_retention_index`, derived from pot size, container material, and soil retention.
- `plant_water_need_index`, derived from plant type, moisture target, and heat sensitivity.
- `moisture_trend`, derived from recent moisture history when available.

It uses fuzzy membership functions and defuzzification to produce a base irrigation depth in millimeters, then adjusts that prescription using the derived context indices. A dedicated `FuzzyIrrigationPolicy` owns fuzzy timing, trigger threshold, skip, prescription, and request-conversion semantics. The physical state, sensor, weather, and valve-zone mechanics remain shared simulation infrastructure.

The fuzzy policy is tuned as a comfort-preserving water saver. It uses a fuzzy comfort band near `moisture_target_pct - 6` while still staying above `moisture_min_pct + 3`, raises that band for heat/wind-sensitive cases, lets meaningful rain suppress only near-comfort non-critical watering, and scales fuzzy prescription volume toward the comfort band rather than the full target. Fuzzy can keep a small soft prescription above the trigger threshold so zone-level valve activation can include nearby pots when another pot opens the valve. Summary metrics report water savings together with comfort-preserved and moisture-safe savings values, using the fuzzy comfort band as the displayed threshold for the fuzzy experiment.

### 11.4 ANFIS-GA Style Experiment

The ANFIS implementation is a compact local approximation, not a validated agronomic model. It:

- Uses moisture, temperature, effective rain, drying-demand index, container-retention index, plant-water-need index, and moisture trend as inputs.
- Trains membership parameters and rule outputs from generated or recorded examples.
- Produces irrigation probability and category outputs.
- Applies operating thresholds and water-saving policy logic.
- Reports fit, RMSE, accuracy, decision accuracy, probability range, water savings, and moisture-safe savings.

Training is separated from normal experiment execution. The optional `anfis-trainer` Docker service checks the current `sensor_readings` count and latest timestamp against the latest row in `anfis_models`. When new readings or changed training parameters are detected, it trains from all available sensor-weather examples, stores the serialized global/zone ANFIS controller and calibration data in PostgreSQL, prints the result, and exits. It is a one-shot worker with no HTTP route or exposed port; operators start it with Docker Compose and read the terminal/container logs. The `/api/experiment/anfis` runtime path requires a persisted model whose seed/configuration matches the request.

Although the UI and labels use "ANFIS-GA", the current code is closer to a lightweight ANFIS-style learner with heuristic supervision and training signals.

### 11.5 Soil and Weather Dynamics

Soil moisture evolution considers:

- Season.
- Sun and wind exposure.
- Rain exposure.
- Container size and retention.
- Indoor/outdoor behavior during cold months.
- Irrigation volume delivery.
- Sensor calibration markers.
- Hot-day and dry-windy-day behavior.

Weather modeling uses stored observations/forecasts first and estimated future rows when needed.

## 12. Frontend Specification

### 12.1 Application Structure

The UI is a single SAPUI5 application with `View1.view.xml` as the main workspace and `View1.controller.js` as the controller. Application state is held in one JSONModel with:

- Experiment settings.
- Sampling settings.
- Sensor settings.
- Overview dashboard data.
- Sensor placement summary.
- Active experiment flag.
- Sampling, ANFIS, and fuzzy result entries.
- Chart data and source chart data.
- Summary cards and footer data.
- Loading flags.

### 12.2 User Workflows

The main UI supports:

1. Viewing current irrigation overview before selecting an experiment.
2. Selecting start/end dates.
3. Setting sensor count and sample interval.
4. Opening or ensuring sensor placement recommendations.
5. Running sampling, ANFIS-GA, or fuzzy DT experiments.
6. Reviewing chart output, tabular experiment data, pot data, valve data, and summary cards.
7. Automatic overview refresh while no experiment is active.

### 12.3 Charting

The chart layer is split into:

- `chartBuilder.js`: chart data shaping, derived fields, chart measures, palettes, visibility rules, axis mapping.
- `chartRuntime.js`: VizFrame styling, feed visibility, popovers, legends, overlay lines, initial windows, label visibility, formatters.

Charts display combinations of:

- Baseline moisture.
- Sparse/ANFIS/fuzzy moisture.
- Water usage.
- Rain.
- Maximum temperature.
- ANFIS zone signal.
- Fuzzy irrigation score.

Known UI text issue: several chart measure labels contain mojibake for Celsius units. This should be corrected to `deg C` or a properly encoded degree symbol in a separate UI cleanup.

## 13. Background Workers

| Worker | Source | Responsibility |
| --- | --- | --- |
| `SensorScheduler` | `digital_twin/infrastructure/schedulers/sensors.py` | Generates due simulated readings and performs scheduled cleanup. |
| `PrescriptionScheduler` | `digital_twin/infrastructure/schedulers/prescriptions.py` | Dispatches tomorrow's prescriptions at configured local time. |
| `ActuationScheduler` | `digital_twin/infrastructure/schedulers/actuation.py` | Polls for due actuation windows and consumes them. |
| `anfis_training` | `digital_twin/infrastructure/schedulers/anfis_training.py` | One-shot external worker that trains and persists ANFIS only when needed. |

Prescription and actuation workers run inside the backend process as daemon threads. Sensor data is prepared on API startup, and `SensorScheduler` remains available as the periodic generation/cleanup scheduler module. ANFIS training runs as a separate optional process through Docker Compose so expensive model fitting can be turned on and off independently.

## 14. Caching and Performance

The backend uses `SingleFlightCache` to prevent duplicate expensive experiment computations. The experiment service also caches database snapshots for a configurable TTL.

Current status:

- Baseline result cache warm-up is enabled.
- Snapshot caching is enabled.
- Baseline, sampling, ANFIS, and fuzzy result caching are enabled in `digital_twin/application/experiments/service.py`.
- Related experiment precompute is controlled by runtime settings in `schedule_related_precompute()`.
- ANFIS model training can be moved out of request handling by running `docker compose --profile anfis-training up --build --force-recreate anfis-trainer`; the trained model is reused from `anfis_models`.

If no matching persisted ANFIS model exists, the API returns a configuration error instructing the operator to run the trainer. Operationally, the intended path is to run the separate trainer after sensor data changes.

## 15. Error Handling

API routes catch exceptions and convert them through `digital_twin/api/errors.py`. Date validation errors are raised by service methods. Weather and sensor unavailability are reported as route-level HTTP errors with user-facing messages.

Frontend API calls use `apiClient.fetchJson()`, which parses backend error bodies and throws JavaScript `Error` objects with HTTP status and detail metadata.

## 16. Security and Operations

Current operational characteristics:

- No authentication or authorization is implemented.
- CORS is limited to localhost defaults unless overridden.
- Database credentials are hardcoded for local Docker use.
- Nginx proxies API traffic and UI5 resource paths.
- Weather ingestion uses external Open-Meteo APIs when cache endpoints or startup refresh are used.
- The actuator implementation is simulated and should not directly control real valves without a hardware safety layer.

For a real irrigation deployment, add authentication, role-based controls, secret management, audit logging, rate limits, physical interlocks, valve/pump telemetry, and failure-safe actuator behavior.

## 17. Verification Assets

Backend tests are stored under `tests/`. Important coverage includes:

- Architecture consolidation and API route registration.
- Weather refresh and forecast request logic.
- Baseline and fuzzy controller behavior.
- Valve distribution.
- Sensor placement across valve zones.
- Runtime prescription materialization.
- Overview repository read-model behavior.
- Sensor state lookback and sensor bounds.
- Indoor moisture loss.
- ANFIS training signals and moisture-safe savings metrics.

Frontend test shells exist under `webapp/test/`, including QUnit and OPA structures, but the strongest automated behavioral coverage currently appears to be backend Python tests.

Recommended local verification commands:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall digital_twin tests
npm.cmd run lint
npm.cmd run build
docker compose config
```

## 18. Current Limitations

- Most sensor data is synthetic unless readings are posted through `/api/sensors/ingest`.
- Weather data depends on cached Open-Meteo rows and estimated future rows where needed.
- No real MQTT/LoRa/HTTP hardware bridge is implemented for valves or sensor nodes.
- No hydraulic model for pump pressure, line losses, simultaneous valve limits beyond simple flow scheduling.
- No crop growth, yield, disease-risk, or plant-stress validation model.
- No calibration workflow against field measurements.
- ANFIS-GA is a lightweight approximation and should not be presented as a validated agronomic AI controller.
- Authentication, user management, and deployment hardening are absent.
- Prescription and actuation background workers still run as in-process daemon threads.
- Frontend has some encoding issues in temperature labels.
- There is no explicit migration framework; schema evolution is handled imperatively under `digital_twin/infrastructure/database/schema/`.

## 19. Recommended Improvements for a More Realistic Irrigation Digital Twin

### 19.1 Data and Hardware

- Add real sensor ingestion adapters for MQTT, HTTP device callbacks, or serial gateway imports.
- Store device metadata: sensor model, calibration coefficients, battery level, signal strength, last seen time, installation depth.
- Store valve and pump metadata: valve ID, zone, nominal flow, pressure limits, electrical state, manual override, failure state.
- Add actuator telemetry rows separate from simulated completion, including commanded volume, measured flow, current draw, and fault codes.
- Add rainfall gauge or local weather-station ingestion to calibrate Open-Meteo data.

### 19.2 Digital Twin Model

- Expand the existing soil, plant, and pot domain classes into richer per-pot parameter objects instead of relying mostly on dictionaries.
- Model hydraulic constraints: pump capacity, valve parallelism, pressure drop, emitter clogging, and minimum run time.
- Add water balance calibration by comparing predicted moisture with actual sensor readings.
- Add per-zone and per-pot uncertainty estimates.
- Add anomaly detection for stuck valves, dry sensor drift, implausible readings, and missing telemetry.

### 19.3 Experiment and AI Validation

- Persist experiment configurations and outputs with reproducible seeds.
- Version controller algorithms and simulation assumptions.
- Compare ANFIS/fuzzy outputs against expert-labeled irrigation decisions or measured field results.
- Add independent validation splits from actual historical readings when real data exists.
- Report confidence intervals, not only point estimates.

### 19.4 Architecture and Operations

- Add Alembic or another migration system for schema changes.
- Move schedulers to separate worker processes or a task queue.
- Add API authentication and role-based permissions.
- Add structured logging, metrics, health checks, and dashboards.
- Add integration tests using a real disposable PostgreSQL instance.
- Split device-control logic from simulation logic before connecting physical valves.

### 19.5 Frontend

- Add a dedicated hardware status view for sensors, valves, pump, and recent command outcomes.
- Add per-zone map/plan visualization for valve coverage and sensor placement.
- Add calibration screens for sensor offsets and soil parameters.
- Fix temperature label encoding and normalize all chart unit labels.
- Add empty/error/loading states per chart and per data table.

## 20. Relevant Source Code Index

### 20.1 Application and Configuration

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `digital_twin/api/main.py` | `WebApiApplication` | FastAPI composition, route registration, startup sequence, schema init, workers, cache warm-up. |
| `digital_twin/infrastructure/config.py` | `Settings`, `get_settings()` | Runtime configuration defaults and environment overrides. |
| `digital_twin/domain/weather.py` | `WeatherLocation`, `DEFAULT_WEATHER_LOCATION`, `local_observed_at()` | Location, timezone, and local weather timestamp helpers. |
| `digital_twin/application/clock.py` | `ApplicationClock` | Centralizes local timezone handling for application services. |
| `digital_twin/application/experiments/cache.py` | `SingleFlightCache` | Prevents duplicate expensive experiment computations. |
| `digital_twin/application/exceptions.py` | `DigitalTwinError`, `InvalidDateRange`, `NoWeatherData` | Expected application failures surfaced by API routes. |
| `digital_twin/infrastructure/exceptions.py` | `DatabaseUnavailable`, `WeatherProviderError` | Infrastructure failures translated by API routes. |

### 20.2 API Routes

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `digital_twin/api/sensor_route.py` | Sensor and system routes | Root, hello, overview, pots, sensor summary, cleanup, ingest, placement, seed, run-at/run-due. |
| `digital_twin/api/weather_route.py` | Weather API routes | Weather summary, hourly reads, cache, range cache, forecast refresh, CSV import. |
| `digital_twin/api/experiment_route.py` | Experiment routes | Baseline, sampling, ANFIS, fuzzy, precompute, and experiment run snapshots. |
| `digital_twin/api/actuation_route.py` | Actuation/control-loop routes | Prescription preparation/dispatch and actuator consumption. |
| `digital_twin/api/errors.py` | `ApiErrorMapper.to_http_error()` | Consistent HTTP error shaping. |

### 20.3 Database and Repositories

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `digital_twin/infrastructure/database/connection.py` | `get_connection()` | Shared psycopg connection boundary. |
| `digital_twin/infrastructure/database/schema/lifecycle.py` | `initialize_database()` | Idempotent database setup and schema-current gate. |
| `digital_twin/infrastructure/database/schema/ddl.py` | `create_schema()` | Defines tables, constraints, and indexes. |
| `digital_twin/infrastructure/database/schema/seeding.py` | `seed_reference_data()`, `seed_pots()` | Plant, pot-size, and generated inventory seed data. |
| `digital_twin/infrastructure/database/schema/queries.py` | Health and pot queries | Database health, pot summary, and paginated pot reads. |
| `digital_twin/infrastructure/database/repositories/pots.py` | `PotRepository` | API read boundary for database health and pot inventory. |
| `digital_twin/infrastructure/database/repositories/sensors.py` | `SensorRepository` | Sensor summary read boundary. |
| `digital_twin/infrastructure/database/repositories/sensor_placements.py` | `SensorPlacementRepository` | Sensor placement persistence. |
| `digital_twin/infrastructure/database/repositories/overview/current.py` | `OverviewRepository` | Dashboard read model composition. |
| `digital_twin/infrastructure/database/repositories/overview/valve_plan.py` | `valve_plan()` | Current valve-zone plan and grouping details. |
| `digital_twin/infrastructure/database/repositories/overview/irrigation_windows.py` | Irrigation window helpers | Next/recent irrigation windows for dashboard state. |
| `digital_twin/infrastructure/database/repositories/experiment_repository.py` | `ExperimentRunRepository` | Experiment result snapshot persistence. |
| `digital_twin/infrastructure/database/repositories/experiment_repository.py` | `ActuationRepository` | Prescription materialization and actuation state transitions. |
| `digital_twin/infrastructure/database/repositories/anfis_model_repository.py` | `AnfisModelRepository` | Serialized ANFIS controller persistence. |

### 20.4 Services and Workers

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `digital_twin/application/weather_refresh/service.py` | `WeatherRefreshService` | Weather orchestration boundary. |
| `digital_twin/application/weather_refresh/ingestion.py` | `WeatherIngestion` | Historical/forecast cache workflow, CSV import, and daily forecast refresh logic. |
| `digital_twin/application/weather_refresh/rows.py` | `WeatherRows` | Weather row normalization, local buckets, CSV parsing, and synthetic gap filling. |
| `digital_twin/application/weather_refresh/persistence.py` | `WeatherPersistence` | Weather cache upsert and import statistics. |
| `digital_twin/infrastructure/open_meteo.py` | Open-Meteo helpers | External archive/forecast request shaping and CSV parsing. |
| `digital_twin/application/sensors/service.py` | `SensorService` | Sensor generation, ingestion, cleanup boundary. |
| `digital_twin/application/sensors/generation.py` | `SensorReadingGenerator` | Seeds and generates simulated sensor readings. |
| `digital_twin/application/sensors/ingestion.py` | `SensorReadingIngestionService` | Stores actual sensor readings in raw local slots. |
| `digital_twin/application/sensors/maintenance.py` | `SensorReadingMaintenanceService` | Ensures coverage, reseeds missing ranges, and cleans up aggregates. |
| `digital_twin/application/sensors/availability.py` | `SensorAvailabilityService` | Loads sensor coverage for experiment ranges. |
| `digital_twin/application/sensors/state_rows.py` | Sensor state rows | Builds and persists sensor-derived pot state rows. |
| `digital_twin/application/sensors/reading_cadence.py` | `SensorReadingCadence` | Shared reading cadence, timezone, and timestamp helpers. |
| `digital_twin/application/actuators/feedback.py` | `ActuationFeedbackService` | Applies actuator completion feedback into sensor state. |
| `digital_twin/application/sensors/placement.py` | `SensorPlacementService` | Sensor scoring and valve-zone coverage. |
| `digital_twin/application/experiments/anfis_model_service.py` | `AnfisModelService` | Persisted ANFIS model loading and one-shot training orchestration. |
| `digital_twin/application/experiments/service.py` | `ExperimentService` | API-facing experiment boundary, range resolution, precompute, and run history reads. |
| `digital_twin/application/experiments/experiments.py` | Experiment execution classes | Baseline, sampling, ANFIS, and fuzzy execution objects with snapshot, model, and prescription handling. |
| `digital_twin/application/experiments/run_history.py` | `ExperimentRunHistory` | Experiment run snapshot persistence wrapper. |
| `digital_twin/application/experiments/precompute.py` | `ExperimentPrecomputeService` | Related experiment warm-up and process-pool precompute. |
| `digital_twin/application/control_loop/runtime.py` | `RuntimeControlLoop` | Runtime prescription and actuation use-case facade. |
| `digital_twin/application/control_loop/prescription.py` | `RuntimePrescriptionStore`, `PrescriptionStage` | Converts experiment outputs into prepared and dispatched prescriptions. |
| `digital_twin/application/actuators/service.py` | `IrrigationActuationService` | Actuator consumption workflow. |
| `digital_twin/infrastructure/schedulers/sensors.py` | `SensorScheduler` | Periodic sensor generation/cleanup. |
| `digital_twin/infrastructure/schedulers/prescriptions.py` | `PrescriptionScheduler` | Daily prescription dispatch. |
| `digital_twin/infrastructure/schedulers/actuation.py` | `ActuationScheduler` | Due actuation polling. |
| `digital_twin/infrastructure/schedulers/anfis_training.py` | Worker module | One-shot Docker entrypoint for ANFIS training. |

### 20.5 Simulation, Control, Experiments

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `digital_twin/domain/plant.py` | `PlantType`, `PlantCatalog` | Plant reference model and plant-specific defaults. |
| `digital_twin/domain/pot.py` | `PotSizeCatalog`, `PotExposureRules` | Pot size, exposure, flow, and inventory seed rules. |
| `digital_twin/domain/sensor.py` | `SensorSource`, `SensorReadingResolution` | Sensor source and storage-resolution identifiers. |
| `digital_twin/domain/valve.py` | `ValveLayout`, `DEFAULT_VALVE_LAYOUT` | Five-zone valve mapping and helper lookups. |
| `digital_twin/domain/irrigation.py` | `IrrigationSlot`, `IrrigationStatus` | Runtime irrigation slots and event/actuation statuses. |
| `digital_twin/domain/soil.py` | `SoilModel`, `DEFAULT_SOIL_MODEL` | Soil moisture evolution, seasonal factors, and clamp/number helpers. |
| `digital_twin/simulation/shared/constants.py` | Shared constants | Local timezone and ANFIS thresholds. |
| `digital_twin/simulation/shared/types.py` | `ExperimentSnapshot`, `PotState` | Simulation state transfer objects. |
| `digital_twin/application/experiments/snapshots.py` | `StateEstimator` | Builds initial pot state, weather groupings, future sensor priming, and day profiles. |
| `digital_twin/application/experiments/snapshots.py` | `ExperimentSnapshotLoader` | Loads sensed inputs and assembles experiment snapshots. |
| `digital_twin/simulation/state/environment.py` | `StateEnvironment` | Environmental state policy for initial states, weather grouping, day profiles, and moisture effects. |
| `digital_twin/simulation/engine.py` | `run_default_dt_irrigation_control()` | Baseline/default controller entry point. |
| `digital_twin/simulation/engine.py` | `run_daily_sampling_experiment()` | Sparse sensing experiment entry point. |
| `digital_twin/simulation/engine.py` | `run_daily_fuzzy_dt_experiment()` | Fuzzy digital-twin experiment entry point. |
| `digital_twin/simulation/engine.py` | `run_daily_anfis_experiment()` | ANFIS experiment entry point. |
| `digital_twin/simulation/daily_irrigation.py` | `run_default_daily_irrigation()` | Default daily irrigation simulation. |
| `digital_twin/simulation/sampling/execution.py` | `run_sparse_daily_irrigation()` | Sparse sensor schedule simulation. |
| `digital_twin/simulation/fuzzy/execution.py` | `run_fuzzy_dt_daily_irrigation()` | Fuzzy controller execution. |
| `digital_twin/simulation/anfis/experiment.py` | ANFIS training and experiment helpers | Trains from snapshot context and runs persisted ANFIS controllers. |
| `digital_twin/simulation/anfis/model.py` | `ANFIS` | Compact ANFIS-style model. |
| `digital_twin/simulation/anfis/controller.py` | `AnfisModelController` | Global/zone ANFIS model wrapper and calibration. |
| `digital_twin/simulation/irrigation_controller/domain_policy.py` | `IrrigationDomainPolicy` | Shared irrigation timing, threshold, rain, and water-sizing policy used across controllers. |
| `digital_twin/simulation/irrigation_controller/fuzzy_policy.py` | `FuzzyIrrigationPolicy` | Fuzzy-owned timing, trigger, skip, prescription, and request-conversion policy. |
| `digital_twin/simulation/irrigation_controller/baseline_decision.py` | `make_baseline_irrigation_decision()` | Baseline rule decision logic. |
| `digital_twin/simulation/irrigation_controller/fuzzy_decision.py` | `make_fuzzy_dt_decision()` | Fuzzy decision wrapper. |
| `digital_twin/simulation/irrigation_controller/environment.py` | Irrigation environment helpers | Rain exposure, weather profiles, and derived weather context. |
| `digital_twin/simulation/valves/distribution.py` | Valve distribution helpers | Zone distribution and cold-month indoor skip logic. |
| `digital_twin/simulation/valves/rollups.py` | Valve rollup helpers | Aggregates pot decisions into valve events and UI summaries. |
| `digital_twin/simulation/weather_model.py` | Weather helpers | Stored/estimated weather loading. |

### 20.6 Frontend

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `webapp/manifest.json` | SAPUI5 app metadata | UI libraries, routing, root view, CSS registration. |
| `webapp/Component.js` | UI component init | Device model, FLP user shim, router initialization. |
| `webapp/view/View1.view.xml:32` | Experiment buttons | User entry points for Sampling, ANFIS-GA, Fuzzy DT. |
| `webapp/controller/View1.controller.js:41` | `onInit()` | JSONModel initialization, overview/weather/sensor loading. |
| `webapp/controller/View1.controller.js:499` | `onRunSampling()` | Sampling experiment request flow. |
| `webapp/controller/View1.controller.js:555` | `onRunAnfis()` | ANFIS experiment request flow. |
| `webapp/controller/View1.controller.js:615` | `onRunFuzzyDt()` | Fuzzy experiment request flow. |
| `webapp/model/apiClient.js` | API helper | Backend URL resolution and error parsing. |
| `webapp/model/chartBuilder.js:69` | `prepareChartResult()` | Chart result normalization and derived fields. |
| `webapp/model/chartBuilder.js:85` | `EXPERIMENT_CHART_IDS` | Registered VizFrame chart IDs. |
| `webapp/model/chartRuntime.js` | Chart runtime mixin | VizFrame properties, legends, popovers, overlays, formatters. |
| `webapp/model/experimentMapper.js` | Experiment defaults | Default summaries and date range helpers. |
| `webapp/model/sensorPlacementBuilder.js` | Sensor placement UI helper | Frontend load/ensure/sync workflow. |
| `webapp/model/summaryCards.js` | Summary cards | Derived UI summary metrics and valve details. |
| `webapp/css/style.css` | UI styling | Dashboard, panels, charts, tables, and overview rail styling. |

### 20.7 Deployment and Tests

| Source | Relevant Code | Why It Matters |
| --- | --- | --- |
| `docker-compose.yml:1` | Service topology | Defines frontend/backend/postgres deployment. |
| `Dockerfile.backend` | Backend image | Python runtime and Uvicorn entrypoint. |
| `Dockerfile.frontend` | Frontend image | UI5 build and Nginx serve image. |
| `nginx.conf:19` | API proxy | Proxies `/api/` to backend container. |
| `package.json` | Frontend scripts | UI5 build, lint, start, test scripts. |
| `requirements.txt` | Backend dependencies | Minimal backend runtime dependencies used locally and by the backend Docker image. |
| `tests/test_architecture.py` | Architecture tests | Confirms route consolidation and schema expectations. |
| `tests/test_domain_catalogs.py` | Domain catalog tests | Plant, pot, valve, and related domain catalog behavior. |
| `tests/test_irrigation_controller.py` | Controller tests | Baseline/fuzzy controller behavior. |
| `tests/test_sensor_placement_service.py` | Placement tests | Valve-zone sensor coverage behavior. |
| `tests/test_control_loop.py` | Control-loop tests | Runtime prescription and actuation route/service behavior. |
| `tests/test_runtime_prescriptions.py` | Prescription tests | Prescription and actuation materialization behavior. |
| `tests/test_anfis_training.py` | ANFIS tests | Training signal and water-saving policy behavior. |
| `tests/test_anfis_model_service.py` | Persisted ANFIS tests | Model loading, metadata matching, and retraining decisions. |
| `tests/test_weather_csv.py` | Weather CSV tests | Open-Meteo CSV import behavior. |
| `tests/test_weather_ingestion.py` | Weather tests | Forecast refresh request behavior. |
