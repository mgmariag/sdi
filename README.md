# Digital Twin Irrigation System

Master's thesis application for simulating and evaluating irrigation decisions with a database-backed digital twin.

## Architecture

The application is organized around one FastAPI backend, one SAPUI5 frontend, PostgreSQL, and an optional one-shot ANFIS trainer. On startup the backend initializes the database, refreshes weather when enabled, prepares generated sensor data, starts prescription and actuation workers, and warms the default experiment cache.

| Service | Port | Responsibility |
| --- | ---: | --- |
| `frontend` | `8080` | SAPUI5 user interface served by Nginx |
| `backend` | `8000` | API routes, weather/sensor ingestion, scheduled workers, default control execution, experiment execution, cached results |
| `anfis-trainer` | n/a | Optional profiled worker that trains and persists the ANFIS model, then exits |
| `postgres` | `5432` | Shared persistent database |

The frontend calls the backend through `/api/...`. The backend code lives under `digital_twin/`; legacy prototype folders were removed.

## Important Folders

- `digital_twin/api/` - FastAPI app factory and route modules.
- `digital_twin/application/` - use cases for experiments, persisted ANFIS models, weather refresh, sensors, actuators, and the runtime control loop.
- `digital_twin/domain/` - singular domain modules for pots, plants, sensors, valves, irrigation, soil rules, and weather helpers.
- `digital_twin/infrastructure/` - environment-backed settings, database connection/schema/repositories, Open-Meteo integration, and internal schedulers.
- `digital_twin/simulation/` - simulation engine, weather model, irrigation controller policies, sensor calibration, valve rollups, and experiment strategy execution.
- `webapp/` - SAPUI5 frontend.

## Running With Docker

```powershell
docker compose build
docker compose up -d --force-recreate
```

Run the optional ANFIS trainer when sensor readings have changed:

```powershell
docker compose --profile anfis-training up --build --force-recreate anfis-trainer
```

The trainer is a one-shot worker, not a web service: it has no browser URL or exposed port. Read the terminal/container logs for the training result. The container stops after training or no-op completion.
The trainer checks the latest sensor-reading watermark and skips work when the persisted model is current.
Run it before using the ANFIS experiment endpoint in a fresh database.

Open the UI at:

```text
http://localhost:8080
```

Useful service endpoints:

- Main API: `http://localhost:8000/api/hello`
- Weather summary: `http://localhost:8000/api/weather/cluj-napoca/summary`
- Weather CSV import: `POST http://localhost:8000/api/weather/cluj-napoca/import-csv?csv_path=<server-local-file>`
- Sensor summary: `http://localhost:8000/api/sensors/summary`

## Local Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall digital_twin tests
npm.cmd run build
docker compose config
```
