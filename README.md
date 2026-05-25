# Digital Twin Irrigation System

Master's thesis application for simulating and evaluating irrigation decisions with a database-backed digital twin.

## Architecture

The application is split into explicit services:

| Service | Port | Responsibility |
| --- | ---: | --- |
| `frontend` | `8080` | SAPUI5 user interface served by Nginx |
| `backend` | `8000` | UI-facing API, pot metadata, experiment execution, cached experiment results |
| `weather-service` | `8001` | Open-Meteo ingestion and weather cache maintenance |
| `sensor-service` | `8002` | Scheduled simulated sensor readings and sensor database updates |
| `postgres` | `5432` | Shared persistent database |

The frontend calls the `backend` through `/api/...`. Ingestion operations are separated into their own services so weather refreshes and sensor updates do not run inside the UI API process.

## Important Folders

- `digital_twin/` - package-based backend architecture with API routers, config, repositories, domain contracts, services, workers, and scripts.
- `backend/` - FastAPI app and experiment cache/service orchestration.
- `services/` - standalone ingestion service entrypoints.
- `tools/` - domain simulation, ANFIS, sensor generation, and offline scripts.
- `webapp/` - SAPUI5 frontend.
- `database.py` - compatibility module for schema initialization, seed data, and data access helpers now surfaced through `digital_twin.db`.
- `weather_ingestion.py` - compatibility module for Open-Meteo retrieval and weather persistence now surfaced through `digital_twin.services`.

The legacy entrypoints remain stable:

- `backend.api:app` imports the UI-facing API from `digital_twin.api.main`.
- `services.weather_api:app` imports the weather service from `digital_twin.api.weather_app`.
- `services.sensor_api:app` imports the sensor service from `digital_twin.api.sensor_app`.

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
- Weather service health: `http://localhost:8001/health`
- Sensor service health: `http://localhost:8002/health`

## Local Verification

```powershell
python -m py_compile .\hello.py .\backend\api.py .\backend\cache.py .\backend\experiment_service.py .\services\weather_api.py .\services\sensor_api.py .\tools\daily_irrigation.py .\tools\sensor_readings.py .\weather_ingestion.py .\database.py .\digital_twin\api\main.py .\digital_twin\api\weather_app.py .\digital_twin\api\sensor_app.py
npm.cmd run build
docker compose config
```
