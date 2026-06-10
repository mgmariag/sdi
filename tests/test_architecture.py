from __future__ import annotations

import unittest
from pathlib import Path

from digital_twin.api.main import create_app
from digital_twin.core.cache import SingleFlightCache
from digital_twin.core.config import get_settings
from digital_twin.services.experiment_service import _experiment_cache


class ArchitectureTests(unittest.TestCase):
    def test_legacy_roots_are_removed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        removed_paths = [
            root / "backend",
            root / "services",
            root / "tools",
            root / "database.py",
            root / "weather_ingestion.py",
        ]
        self.assertFalse(any(path.exists() for path in removed_paths))

    def test_api_route_modules_are_consolidated(self) -> None:
        route_dir = Path(__file__).resolve().parents[1] / "digital_twin" / "api" / "routes"
        route_files = {path.name for path in route_dir.glob("*.py") if path.name != "__init__.py"}
        self.assertEqual(route_files, {"experiments.py", "weather.py", "sensors.py"})

    def test_experiment_service_uses_package_cache(self) -> None:
        self.assertIsInstance(_experiment_cache, SingleFlightCache)

    def test_default_strategy_is_control_logic_not_experiment(self) -> None:
        root = Path(__file__).resolve().parents[1]

        from digital_twin.control import DefaultStrategy, run_default_dt_control

        self.assertTrue(callable(run_default_dt_control))
        self.assertIsNotNone(DefaultStrategy)
        self.assertTrue((root / "digital_twin" / "control" / "default_strategy.py").exists())
        self.assertFalse((root / "digital_twin" / "control" / "baseline_strategy.py").exists())
        self.assertFalse((root / "digital_twin" / "experiments" / "baseline.py").exists())

    def test_main_api_registers_compatible_routes(self) -> None:
        app = create_app()
        paths = {route.path for route in app.routes}

        expected_paths = {
            "/api/hello",
            "/api/db/health",
            "/api/pots",
            "/api/pots/summary",
            "/api/sensors/summary",
            "/api/weather/cluj-napoca/summary",
            "/api/weather/cluj-napoca/hourly",
            "/api/experiment",
            "/api/experiment/sampling",
            "/api/experiment/anfis",
            "/api/experiment/fuzzy",
            "/api/experiment/runs",
            "/api/experiment/runs/{run_id}",
        }
        self.assertTrue(expected_paths.issubset(paths))
        self.assertNotIn("/sensors/summary", paths)
        self.assertNotIn("/weather/cluj-napoca/summary", paths)

    def test_frontend_proxy_targets_only_consolidated_backend(self) -> None:
        root = Path(__file__).resolve().parents[1]
        nginx_config = (root / "nginx.conf").read_text(encoding="utf-8")
        compose_config = (root / "docker-compose.yml").read_text(encoding="utf-8")

        for obsolete in ("sensor-service", "weather-service", "actuation-service", "8001", "8002", "8003"):
            self.assertNotIn(obsolete, nginx_config)
            self.assertNotIn(obsolete, compose_config)

    def test_schema_does_not_store_experiment_decisions_or_alerts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = (root / "digital_twin" / "db" / "schema.py").read_text(encoding="utf-8")

        self.assertNotIn("CREATE TABLE IF NOT EXISTS irrigation_decisions", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS alerts", schema)
        self.assertNotIn("decision_id BIGINT", schema)
        self.assertIn("DROP TABLE IF EXISTS irrigation_decisions", schema)
        self.assertIn("DROP TABLE IF EXISTS alerts", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS experiment_runs", schema)

    def test_consolidated_sensor_repositories_import(self) -> None:
        from digital_twin.db.repositories.sensor_repository import (
            OverviewRepository,
            PotRepository,
            SensorPlacementRepository,
        )

        self.assertIsNotNone(OverviewRepository)
        self.assertIsNotNone(PotRepository)
        self.assertIsNotNone(SensorPlacementRepository)

    def test_simulation_engine_keeps_controller_helpers_wired(self) -> None:
        from digital_twin.simulation import engine

        self.assertTrue(callable(engine._apply_event_delivery))
        self.assertTrue(callable(engine.DEFAULT_IRRIGATION_POLICY.irrigation_request))
        self.assertTrue(callable(engine.DEFAULT_FUZZY_POLICY.irrigation_request))

    def test_settings_centralize_runtime_defaults(self) -> None:
        settings = get_settings()

        self.assertEqual(settings.weather_location.name, "Cluj-Napoca")
        self.assertEqual(settings.sensor_source, "simulated_sensor")
        self.assertGreater(settings.experiment_snapshot_cache_ttl_seconds, 0)


if __name__ == "__main__":
    unittest.main()
