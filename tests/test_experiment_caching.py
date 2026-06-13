from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import digital_twin.application.experiments.cache_keys as cache_keys
import digital_twin.application.experiments.experiment_service as experiment_service
import digital_twin.application.experiments.precompute as precompute
from digital_twin.core.cache import SingleFlightCache


class ExperimentCachingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cache = experiment_service._experiment_cache
        experiment_service._experiment_cache = SingleFlightCache()

    def tearDown(self) -> None:
        experiment_service._experiment_cache = self._old_cache

    def test_sampling_experiment_reuses_cached_payload(self) -> None:
        calls = []

        def payload(start, end, sample_interval_days, sample_interval_hours, persist=True):
            calls.append((start, end, sample_interval_days, sample_interval_hours, persist))
            return {"summary": {"totalWaterUsage": 1.0}, "sampleEvents": []}

        with (
            patch.object(cache_keys, "sensor_placement_cache_token", return_value=("placement",)),
            patch.object(experiment_service, "_sampling_payload", side_effect=payload),
            patch.object(experiment_service, "_store_experiment_run"),
            patch.object(experiment_service, "schedule_related_precompute", return_value={}),
        ):
            first = experiment_service.run_sampling_experiment(date(2026, 6, 1), date(2026, 6, 2), 3, None)
            second = experiment_service.run_sampling_experiment(date(2026, 6, 1), date(2026, 6, 2), 3, None)

        self.assertEqual(len(calls), 1)
        self.assertFalse(first["summary"]["cacheHit"])
        self.assertTrue(second["summary"]["cacheHit"])

    def test_related_precompute_schedules_when_enabled(self) -> None:
        def started(label, _cache_key, _task, **_dependencies):
            return "started"

        with (
            patch.object(experiment_service, "_precompute_enabled", return_value=True),
            patch.object(experiment_service, "_precompute_anfis_enabled", return_value=False),
            patch.object(cache_keys, "sensor_placement_cache_token", return_value=("placement",)),
            patch.object(precompute, "start_precompute_task", side_effect=started),
        ):
            status = experiment_service.schedule_related_precompute(
                "sampling",
                date(2026, 6, 1),
                date(2026, 6, 2),
                sample_interval_days=3,
            )

        self.assertEqual(status["disabled"], ["anfis"])
        self.assertEqual(status["started"], ["baseline", "fuzzy_dt"])


if __name__ == "__main__":
    unittest.main()

