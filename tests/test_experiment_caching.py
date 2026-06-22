from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import digital_twin.application.experiments.service as service
import digital_twin.application.experiments.precompute as precompute
from digital_twin.application.experiments.cache import (
    ExperimentCacheKeys,
    ExperimentSnapshotCache,
    SingleFlightCache,
)
from digital_twin.application.experiments.experiments import (
    AnfisIrrigationExperiment,
    BaselineExperiment,
    FuzzyDigitalTwinExperiment,
    SamplingIrrigationExperiment,
)
from digital_twin.application.experiments.precompute import ExperimentPrecomputeService
from digital_twin.application.experiments.snapshots import ExperimentSnapshotLoader


class ExperimentCachingTests(unittest.TestCase):
    def test_sampling_experiment_reuses_cached_payload(self) -> None:
        calls = []
        cache_keys = ExperimentCacheKeys(lambda: ("placement",))
        experiment_service = service.ExperimentService(
            experiment_cache=SingleFlightCache(),
            cache_keys=cache_keys,
            run_history=_FakeRunHistory(),
            precompute_service=_FakePrecomputeService(),
        )

        def payload(experiment, persist=True):
            calls.append((experiment, persist))
            return {"summary": {"totalWaterUsage": 1.0}, "sampleEvents": []}

        with patch.object(experiment_service, "run_experiment_payload", side_effect=payload):
            first = experiment_service.run_sampling_for_range(date(2026, 6, 1), date(2026, 6, 2), 3, None)
            second = experiment_service.run_sampling_for_range(date(2026, 6, 1), date(2026, 6, 2), 3, None)

        self.assertEqual(len(calls), 1)
        self.assertFalse(first["summary"]["cacheHit"])
        self.assertTrue(second["summary"]["cacheHit"])

    def test_related_precompute_schedules_when_enabled(self) -> None:
        def started(label, _cache_key, _task, **_dependencies):
            return "started"

        cache_keys = ExperimentCacheKeys(lambda: ("placement",))
        precompute_service = ExperimentPrecomputeService(
            cache=SingleFlightCache(),
            compute_payload=lambda _task: {},
            cache_keys=cache_keys,
            settings_provider=lambda: _Settings(precompute_related=True, precompute_anfis=False),
        )

        with patch.object(precompute, "start_precompute_task", side_effect=started):
            status = precompute_service.schedule(
                "sampling",
                date(2026, 6, 1),
                date(2026, 6, 2),
                sample_interval_days=3,
                sample_interval_hours=None,
                seed=service.DEFAULT_SCENARIO_SEED,
            )

        self.assertEqual(status["disabled"], ["anfis"])
        self.assertEqual(status["started"], ["baseline", "fuzzy_dt"])

    def test_cache_helpers_use_experiment_owned_versions(self) -> None:
        cache_keys = ExperimentCacheKeys(lambda: ("placement",))

        self.assertEqual(
            cache_keys.baseline(date(2026, 6, 1), date(2026, 6, 2))[0],
            BaselineExperiment.cache_version,
        )
        self.assertEqual(
            cache_keys.sampling(date(2026, 6, 1), date(2026, 6, 2), 3, None)[0],
            SamplingIrrigationExperiment.cache_version,
        )
        self.assertEqual(
            cache_keys.anfis(date(2026, 6, 1), date(2026, 6, 2), 2026)[0],
            AnfisIrrigationExperiment.cache_version,
        )
        self.assertEqual(
            cache_keys.fuzzy_dt(date(2026, 6, 1), date(2026, 6, 2))[0],
            FuzzyDigitalTwinExperiment.cache_version,
        )

    def test_snapshot_cache_key_uses_snapshot_loader_version(self) -> None:
        snapshot_cache = ExperimentSnapshotCache(cache_keys=ExperimentCacheKeys(lambda: ("placement",)))

        self.assertEqual(
            snapshot_cache._cache_key(date(2026, 6, 1), date(2026, 6, 2)),
            (
                ExperimentSnapshotLoader.cache_version,
                date(2026, 6, 1),
                date(2026, 6, 2),
                ("placement",),
            ),
        )


class _FakeRunHistory:
    def __init__(self) -> None:
        self.stored = []

    def store(self, *args, **kwargs) -> None:
        self.stored.append((args, kwargs))


class _FakePrecomputeService:
    worker_count = 0

    def __init__(self) -> None:
        self.scheduled = []

    def schedule(self, *args, **kwargs) -> dict:
        self.scheduled.append((args, kwargs))
        return {}


class _Settings:
    def __init__(self, precompute_related: bool, precompute_anfis: bool) -> None:
        self.experiment_precompute_related = precompute_related
        self.experiment_precompute_anfis = precompute_anfis


if __name__ == "__main__":
    unittest.main()
