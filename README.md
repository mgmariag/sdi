# Digital Twin Irrigation System

Master's thesis application for simulating and evaluating irrigation decisions with a database-backed digital twin.

## Architecture

The application is organized around one FastAPI backend, one SAPUI5 frontend, and PostgreSQL:

| Service | Port | Responsibility |
| --- | ---: | --- |
| `frontend` | `8080` | SAPUI5 user interface served by Nginx |
| `backend` | `8000` | API routes, weather/sensor ingestion, experiment execution, cached results |
| `postgres` | `5432` | Shared persistent database |

The frontend calls the backend through `/api/...`. The backend code lives under `digital_twin/`; legacy prototype folders were removed.

## Important Folders

- `digital_twin/api/` - FastAPI app factory and route modules.
- `digital_twin/services/` - experiment, weather, irrigation, and sensor services.
- `digital_twin/domain/` - domain models and irrigation method definitions.
- `digital_twin/db/` - database connection, schema initialization, and repositories.
- `digital_twin/experiments/` - baseline, sampling, ANFIS-GA, and fuzzy DT experiment wrappers.
- `digital_twin/simulation/` - split simulation engine, DTOs, soil/weather models, and irrigation controller logic.
- `webapp/` - SAPUI5 frontend.

## Running With Docker

```powershell
docker compose build
docker compose up -d --force-recreate
```

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
