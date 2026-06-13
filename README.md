# Digital Twin Irrigation System

Master's thesis application for simulating and evaluating irrigation decisions with a database-backed digital twin.

## Architecture

The application is organized around one FastAPI backend, one SAPUI5 frontend, PostgreSQL, and an optional one-shot ANFIS trainer. Sensor updates, weather refresh, prescription dispatch, and actuator consumption run as internal backend workers.

| Service | Port | Responsibility |
| --- | ---: | --- |
| `frontend` | `8080` | SAPUI5 user interface served by Nginx |
| `backend` | `8000` | API routes, weather/sensor ingestion, scheduled workers, default control execution, experiment execution, cached results |
| `anfis-trainer` | n/a | Optional profiled worker that trains and persists the ANFIS model, then exits |
| `postgres` | `5432` | Shared persistent database |

The frontend calls the backend through `/api/...`. The backend code lives under `digital_twin/`; legacy prototype folders were removed.

## Important Folders

- `digital_twin/api/` - FastAPI app factory and route modules.
- `digital_twin/application/` - use cases for experiments, ANFIS training, weather refresh, sensor history, placement, and the runtime control loop.
- `digital_twin/infrastructure/` - database connection/schema/repositories, importers, external weather client, and internal schedulers.
- `digital_twin/domain/` - shared domain constants and simple domain models.
- `digital_twin/experiments/` - sampling, ANFIS-GA, and fuzzy DT experiment wrappers.
- `digital_twin/simulation/` - split simulation engine, DTOs, soil/weather models, and irrigation controller logic.
- `webapp/` - SAPUI5 frontend.

## Running With Docker

```powershell
docker compose build
docker compose up -d --force-recreate
```

Run the optional ANFIS trainer when sensor readings have changed:

```powershell
docker compose --profile anfis-training up --build anfis-trainer
```

The trainer checks the latest sensor-reading watermark, skips work when the persisted model is current, and stops the container after training or no-op completion.
Run it before using the ANFIS experiment endpoint in a fresh database.

Open the UI at:

```text
http://localhost:8080
```

Useful service endpoints:

- Main API: `http://localhost:8000/api/hello`
- Weather summary: `http://localhost:8000/api/weather/cluj-napoca/summary`
- Sensor summary: `http://localhost:8000/api/sensors/summary`

## Local Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_architecture
.\.venv\Scripts\python.exe -m compileall digital_twin tests
npm.cmd run build
docker compose config
```
