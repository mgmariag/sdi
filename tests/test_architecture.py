from __future__ import annotations

import unittest

from backend.cache import SingleFlightCache as LegacySingleFlightCache
from digital_twin.api.main import create_app
from digital_twin.core.cache import SingleFlightCache
from digital_twin.core.config import get_settings
from services.sensor_api import app as sensor_app
from services.weather_api import app as weather_app


class ArchitectureTests(unittest.TestCase):
    def test_legacy_cache_entrypoint_uses_package_cache(self) -> None:
        self.assertIs(LegacySingleFlightCache, SingleFlightCache)

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
        }
        self.assertTrue(expected_paths.issubset(paths))

    def test_ingestion_service_entrypoints_keep_compatible_routes(self) -> None:
        weather_paths = {route.path for route in weather_app.routes}
        sensor_paths = {route.path for route in sensor_app.routes}

        self.assertIn("/weather/cluj-napoca/refresh-forecast", weather_paths)
        self.assertIn("/weather/cluj-napoca/cache-range", weather_paths)
        self.assertIn("/sensors/run-due", sensor_paths)
        self.assertIn("/sensors/run-at", sensor_paths)

    def test_settings_centralize_runtime_defaults(self) -> None:
        settings = get_settings()

        self.assertEqual(settings.weather_location.name, "Cluj-Napoca")
        self.assertEqual(settings.sensor_source, "simulated_sensor")
        self.assertGreater(settings.experiment_snapshot_cache_ttl_seconds, 0)


if __name__ == "__main__":
    unittest.main()

