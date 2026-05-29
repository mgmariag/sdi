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
        }
        self.assertTrue(expected_paths.issubset(paths))

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

        self.assertTrue(callable(engine._apply_planned_volume))

    def test_settings_centralize_runtime_defaults(self) -> None:
        settings = get_settings()

        self.assertEqual(settings.weather_location.name, "Cluj-Napoca")
        self.assertEqual(settings.sensor_source, "simulated_sensor")
        self.assertGreater(settings.experiment_snapshot_cache_ttl_seconds, 0)


if __name__ == "__main__":
    unittest.main()
