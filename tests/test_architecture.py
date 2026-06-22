from __future__ import annotations

import unittest
from pathlib import Path

from digital_twin.api.main import create_app
from digital_twin.infrastructure.config import get_settings


class ArchitectureTests(unittest.TestCase):
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
            "/api/weather/cluj-napoca/import-csv",
            "/api/experiment",
            "/api/experiment/sampling",
            "/api/experiment/anfis",
            "/api/experiment/fuzzy",
            "/api/experiment/runs",
            "/api/experiment/runs/{run_id}",
            "/api/control-loop/prescriptions/prepare",
            "/api/control-loop/prescriptions/dispatch",
            "/api/control-loop/actuations/run-due",
            "/api/control-loop/actuations/summary",
        }
        self.assertTrue(expected_paths.issubset(paths))
        self.assertNotIn("/sensors/summary", paths)
        self.assertNotIn("/weather/cluj-napoca/summary", paths)
        self.assertNotIn("/api/experiment/prescriptions/prepare", paths)
        self.assertNotIn("/api/experiment/actuations/run-due", paths)

    def test_frontend_proxy_targets_only_consolidated_backend(self) -> None:
        root = Path(__file__).resolve().parents[1]
        nginx_config = (root / "nginx.conf").read_text(encoding="utf-8")
        compose_config = (root / "docker-compose.yml").read_text(encoding="utf-8")

        for obsolete in ("sensor-service", "weather-service", "actuation-service", "8001", "8002", "8003"):
            self.assertNotIn(obsolete, nginx_config)
            self.assertNotIn(obsolete, compose_config)

    def test_settings_centralize_runtime_defaults(self) -> None:
        settings = get_settings()

        self.assertEqual(settings.weather_location.name, "Cluj-Napoca")
        self.assertEqual(settings.sensor_source, "simulated_sensor")
        self.assertGreater(settings.experiment_snapshot_cache_ttl_seconds, 0)


if __name__ == "__main__":
    unittest.main()
