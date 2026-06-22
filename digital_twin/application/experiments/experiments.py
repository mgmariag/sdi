from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, ClassVar, Protocol

from digital_twin.application.control_loop.prescription import runtime_prescription_store
from digital_twin.application.exceptions import ExperimentConfigurationError
from digital_twin.application.experiments.anfis_model_service import (
    DEFAULT_ANFIS_GENERATIONS,
    DEFAULT_ANFIS_POPULATION,
    AnfisModelService,
)
from digital_twin.infrastructure.database.repositories.anfis_model_repository import (
    DEFAULT_MODEL_KEY,
)
from digital_twin.simulation.anfis.model import DEFAULT_INPUTS as ANFIS_INPUT_FEATURES
from digital_twin.simulation.anfis.modeling import ANFIS_TRAINING_DATASET_VERSION
from digital_twin.simulation.engine import SimulationEngine
from digital_twin.simulation.shared.types import ExperimentSnapshot

SnapshotProvider = Callable[[date, date], tuple[ExperimentSnapshot, bool]]
BaselineProvider = Callable[[date, date], dict[str, Any]]


class ExperimentStrategy(Protocol):
    start: date
    end: date
    experiment_type: str

    def run(self, context: "ExperimentExecutionContext", persist: bool = True) -> dict[str, Any]:
        ...


class RuntimePrescriptionStore(Protocol):
    def upsert_from_result(
        self,
        experiment_type: str,
        start: date,
        end: date,
        result: dict[str, Any],
    ) -> None:
        ...


@dataclass
class ExperimentExecutionContext:
    """Shared services used by concrete experiment strategies."""

    snapshot_provider: SnapshotProvider
    baseline_provider: BaselineProvider | None = None
    runtime_store: RuntimePrescriptionStore = runtime_prescription_store
    anfis_model_service: AnfisModelService = field(default_factory=AnfisModelService)
    simulation_engine: SimulationEngine = field(default_factory=SimulationEngine)

    def snapshot(self, start: date, end: date) -> tuple[ExperimentSnapshot, bool]:
        return self.snapshot_provider(start, end)

    def shared_baseline(self, start: date, end: date) -> dict[str, Any]:
        if self.baseline_provider is None:
            raise RuntimeError("This experiment context cannot load a shared baseline")
        return self.baseline_provider(start, end)

    def store_runtime_prescription(
        self,
        experiment_type: str,
        start: date,
        end: date,
        result: dict[str, Any],
    ) -> None:
        self.runtime_store.upsert_from_result(experiment_type, start, end, result)

    def annotate_snapshot_cache(
        self,
        result: dict[str, Any],
        snapshot: ExperimentSnapshot,
        cache_hit: bool,
    ) -> dict[str, Any]:
        summary = result.setdefault("summary", {})
        summary["dbSnapshotCacheHit"] = cache_hit
        summary["dbSnapshotLoadedAt"] = snapshot.loaded_at.isoformat()
        summary["dbSnapshotWeatherRows"] = len(snapshot.selected_weather_rows)
        summary["dbSnapshotSensorRows"] = snapshot.sensor_context.get("row_count", 0)
        summary["dbSnapshotEstimatedWeatherRows"] = snapshot.estimated_selected_weather_rows
        summary["dbSnapshotEstimatedWeatherRowsTotal"] = snapshot.estimated_weather_rows
        summary["dbSnapshotEstimatedLookaheadWeatherRows"] = snapshot.estimated_lookahead_weather_rows
        summary["dbSnapshotInitialStateRows"] = len(snapshot.initial_pot_states)
        return result


@dataclass(frozen=True)
class BaselineExperiment:
    start: date
    end: date

    cache_version: ClassVar[str] = "baseline-db-v17-seasonal-rain-dose-valve-details"
    experiment_type: str = "baseline"

    def run(self, context: ExperimentExecutionContext, persist: bool = True) -> dict[str, Any]:
        snapshot, snapshot_cache_hit = context.snapshot(self.start, self.end)
        result = context.simulation_engine.run_default_dt_irrigation_control(
            start_date=self.start,
            end_date=self.end,
            persist=False,
            snapshot=snapshot,
        )
        result = context.annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
        if persist:
            context.store_runtime_prescription(self.experiment_type, self.start, self.end, result)
        return result


@dataclass(frozen=True)
class SamplingIrrigationExperiment:
    start: date
    end: date
    sample_interval_days: int
    sample_interval_hours: int | None

    cache_version: ClassVar[str] = "sampling-db-sensor-weather-v17-sample-point-moisture-sync"
    experiment_type: str = "sampling"

    def run(self, context: ExperimentExecutionContext, persist: bool = True) -> dict[str, Any]:
        snapshot, snapshot_cache_hit = context.snapshot(self.start, self.end)
        baseline = context.shared_baseline(self.start, self.end)
        result = context.simulation_engine.run_daily_sampling_experiment(
            start_date=self.start,
            end_date=self.end,
            sample_interval_days=self.sample_interval_days,
            sample_interval_hours=self.sample_interval_hours,
            persist=False,
            snapshot=snapshot,
            baseline_result=baseline,
        )
        result = context.annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
        if persist:
            context.store_runtime_prescription(self.experiment_type, self.start, self.end, result)
        return result


@dataclass(frozen=True)
class AnfisIrrigationExperiment:
    start: date
    end: date
    seed: int | None

    cache_version: ClassVar[str] = "anfis-db-size-flow-pots-v17-weighted-zone-calibrated-valve-details"
    model_key_by_start_year: ClassVar[dict[int, str]] = {
        2023: "anfis-2023-simulated",
    }
    experiment_type: str = "anfis"

    def run(self, context: ExperimentExecutionContext, persist: bool = True) -> dict[str, Any]:
        snapshot, snapshot_cache_hit = context.snapshot(self.start, self.end)
        baseline = context.shared_baseline(self.start, self.end)
        resolved_model_key, persisted_model = self._load_persisted_model(context)
        result = context.simulation_engine.run_daily_anfis_experiment(
            start_date=self.start,
            end_date=self.end,
            seed=self.seed,
            persist=False,
            snapshot=snapshot,
            baseline_result=baseline,
            trained_model=persisted_model["model"],
            training_metadata=persisted_model["metadata"],
        )
        result = context.annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
        summary = result.setdefault("summary", {})
        summary["anfisModelKey"] = resolved_model_key
        summary["anfisModelSelectionPolicy"] = "start_year_auto"
        summary["anfisModelSelectionYear"] = self.start.year
        if persist:
            context.store_runtime_prescription(self.experiment_type, self.start, self.end, result)
        return result

    def _load_persisted_model(self, context: ExperimentExecutionContext) -> tuple[str, dict[str, Any]]:
        resolved_model_key = self.resolve_model_key(self.start)
        persisted_model = context.anfis_model_service.load_latest_model(resolved_model_key)
        if not persisted_model:
            raise ExperimentConfigurationError(
                f"No persisted ANFIS model is available for key '{resolved_model_key}'. Run "
                "`docker compose --profile anfis-training up --build anfis-trainer` first, "
                "or add a trained model for the requested date range."
            )
        if not self._persisted_model_matches(
            persisted_model["metadata"],
            self.seed,
            strict_training_config=resolved_model_key == DEFAULT_MODEL_KEY,
        ):
            raise ExperimentConfigurationError(
                "The persisted ANFIS model was trained with different ANFIS seed, input-feature, "
                "sample-policy, or default training settings. "
                "Run the ANFIS trainer with matching parameters or use the default endpoint parameters."
            )
        return resolved_model_key, persisted_model

    @classmethod
    def resolve_model_key(cls, start: date) -> str:
        """Select the persisted ANFIS model from the range lower bound."""
        return cls.model_key_by_start_year.get(start.year, DEFAULT_MODEL_KEY)

    @staticmethod
    def _persisted_model_matches(
        metadata: dict[str, Any],
        seed: int | None,
        strict_training_config: bool = True,
    ) -> bool:
        base_match = (
            metadata.get("training_sample_policy") == "all_available_sensor_readings"
            and int(metadata.get("training_dataset_version") or 0) == ANFIS_TRAINING_DATASET_VERSION
            and metadata.get("seed") == seed
            and list(metadata.get("anfis_input_features") or []) == list(ANFIS_INPUT_FEATURES)
        )
        if not base_match:
            return False
        if not strict_training_config:
            return True
        return (
            int(metadata.get("generations") or 0) == DEFAULT_ANFIS_GENERATIONS
            and int(metadata.get("population") or 0) == DEFAULT_ANFIS_POPULATION
        )


@dataclass(frozen=True)
class FuzzyDigitalTwinExperiment:
    start: date
    end: date

    cache_version: ClassVar[str] = "fuzzy-dt-db-v10-volume-score-valve-details"
    experiment_type: str = "fuzzy_dt"

    def run(self, context: ExperimentExecutionContext, persist: bool = True) -> dict[str, Any]:
        snapshot, snapshot_cache_hit = context.snapshot(self.start, self.end)
        baseline = context.shared_baseline(self.start, self.end)
        result = context.simulation_engine.run_daily_fuzzy_dt_experiment(
            start_date=self.start,
            end_date=self.end,
            persist=False,
            snapshot=snapshot,
            baseline_result=baseline,
        )
        result = context.annotate_snapshot_cache(result, snapshot, snapshot_cache_hit)
        if persist:
            context.store_runtime_prescription(self.experiment_type, self.start, self.end, result)
        return result
