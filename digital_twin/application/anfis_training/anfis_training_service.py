from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any

from digital_twin.core.config import get_settings
from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.repositories.anfis_model_repository import (
    DEFAULT_MODEL_KEY,
    AnfisModelRepository,
)
from digital_twin.infrastructure.database.schema.lifecycle import initialize_database
from digital_twin.simulation.anfis.experiment import (
    train_anfis_model_from_snapshot_context,
)
from digital_twin.simulation.anfis.model import DEFAULT_INPUTS as ANFIS_INPUT_FEATURES
from digital_twin.simulation.anfis.modeling import (
    ANFIS_TRAINING_DATASET_VERSION,
    deserialize_trained_anfis_model,
    serialize_trained_anfis_model,
)

DEFAULT_ANFIS_GENERATIONS = 50
DEFAULT_ANFIS_POPULATION = 32
DEFAULT_ANFIS_TRAINING_MONTHS = 12

logger = logging.getLogger("digital_twin.anfis_model")


class AnfisModelService:
    """Coordinates persisted ANFIS model training and runtime loading."""

    def __init__(self, repository: AnfisModelRepository | None = None) -> None:
        self.repository = repository or AnfisModelRepository()

    def load_latest_model(
        self,
        model_key: str = DEFAULT_MODEL_KEY,
    ) -> dict[str, Any] | None:
        row = self.repository.latest(model_key)
        if not row:
            return None
        payload = row.get("model_payload") or {}
        metrics = dict(row.get("metrics") or {})
        model = deserialize_trained_anfis_model(payload)
        actual_input_features = list(getattr(model.global_model, "input_names", []) or [])
        metadata = {
            **metrics,
            "model_id": row.get("id"),
            "model_key": row.get("model_key"),
            "model_source": "persisted_model",
            "trained_at": row.get("trained_at"),
            "sensor_source": row.get("sensor_source"),
            "sensor_reading_count": row.get("sensor_reading_count"),
            "sensor_readings_max_recorded_at": row.get("sensor_readings_max_recorded_at"),
            "train_samples": row.get("train_samples", metrics.get("train_samples", 0)),
            "test_samples": row.get("test_samples", metrics.get("test_samples", 0)),
            "fit_samples": row.get("fit_samples", metrics.get("fit_samples", 0)),
            "calibration_samples": row.get("calibration_samples", metrics.get("calibration_samples", 0)),
            "weighted_fit_samples": row.get("weighted_fit_samples", metrics.get("weighted_fit_samples", 0)),
            "seed": row.get("seed"),
            "generations": row.get("generations"),
            "population": row.get("population"),
            "training_start_date": row.get("training_start_date") or metrics.get("training_start_date"),
            "training_end_date": row.get("training_end_date") or metrics.get("training_end_date"),
            "anfis_input_features": actual_input_features,
            "expected_anfis_input_features": list(ANFIS_INPUT_FEATURES),
        }
        return {
            "model": model,
            "metadata": metadata,
            "row": row,
        }

    def train_if_needed(
        self,
        *,
        model_key: str = DEFAULT_MODEL_KEY,
        start_date: date | None = None,
        end_date: date | None = None,
        seed: int | None = None,
        generations: int = DEFAULT_ANFIS_GENERATIONS,
        population: int = DEFAULT_ANFIS_POPULATION,
        force: bool = False,
    ) -> dict[str, Any]:
        initialize_database()
        settings = get_settings()
        seed = settings.default_scenario_seed if seed is None else seed
        sensor_source = settings.sensor_source
        sensor_watermark = self.repository.sensor_watermark(sensor_source)
        if int(sensor_watermark.get("sensor_reading_count") or 0) <= 0:
            return {
                "status": "skipped",
                "reason": "no_sensor_readings",
                "modelKey": model_key,
                "sensorWatermark": sensor_watermark,
            }

        latest = self.repository.latest(model_key)
        config = {
            "sensor_source": sensor_source,
            "seed": seed,
            "generations": int(generations),
            "population": int(population),
            "training_dataset_version": ANFIS_TRAINING_DATASET_VERSION,
            "anfis_input_features": list(ANFIS_INPUT_FEATURES),
        }
        if not force and latest and not self._needs_training(latest, sensor_watermark, config):
            return {
                "status": "skipped",
                "reason": "model_current",
                "modelKey": model_key,
                "modelId": latest.get("id"),
                "trainedAt": latest.get("trained_at"),
                "sensorWatermark": sensor_watermark,
            }

        start_date, end_date = self._resolve_training_range(start_date, end_date)
        logger.info(
            "Training ANFIS model %s from all available sensor readings: source=%s rows=%s latest=%s range=%s..%s generations=%s population=%s",
            model_key,
            sensor_source,
            sensor_watermark.get("sensor_reading_count"),
            sensor_watermark.get("sensor_readings_max_recorded_at"),
            start_date,
            end_date,
            generations,
            population,
        )
        training_result = train_anfis_model_from_snapshot_context(
            start_date=start_date,
            end_date=end_date,
            seed=seed,
            generations=generations,
            population=population,
        )
        metrics = {
            **training_result.metadata,
            "evaluation": training_result.evaluation,
            "sensor_watermark": sensor_watermark,
            "anfis_input_features": list(training_result.model.global_model.input_names),
        }
        saved = self.repository.upsert(
            model_key=model_key,
            model_payload=serialize_trained_anfis_model(training_result.model),
            metrics=metrics,
            sensor_source=sensor_source,
            sensor_watermark=sensor_watermark,
            train_samples=int(metrics.get("train_samples") or 0),
            test_samples=int(metrics.get("test_samples") or 0),
            fit_samples=int(metrics.get("fit_samples") or 0),
            calibration_samples=int(metrics.get("calibration_samples") or 0),
            weighted_fit_samples=int(metrics.get("weighted_fit_samples") or 0),
            seed=seed,
            generations=generations,
            population=population,
            training_start_date=metrics.get("training_start_date"),
            training_end_date=metrics.get("training_end_date"),
        )
        return {
            "status": "trained",
            "reason": "forced" if force else "new_sensor_readings_or_missing_model",
            "modelKey": model_key,
            "modelId": saved.get("id"),
            "trainedAt": saved.get("trained_at"),
            "trainingStartDate": saved.get("training_start_date"),
            "trainingEndDate": saved.get("training_end_date"),
            "sensorWatermark": sensor_watermark,
            "evaluation": training_result.evaluation,
            "trainSamples": metrics.get("train_samples"),
            "testSamples": metrics.get("test_samples"),
        }

    @staticmethod
    def _needs_training(
        latest: dict[str, Any],
        sensor_watermark: dict[str, Any],
        config: dict[str, Any],
    ) -> bool:
        if str(latest.get("sensor_source") or "") != str(config["sensor_source"]):
            return True
        if int(latest.get("sensor_reading_count") or 0) != int(sensor_watermark.get("sensor_reading_count") or 0):
            return True
        if str(latest.get("sensor_readings_max_recorded_at") or "") != str(
            sensor_watermark.get("sensor_readings_max_recorded_at") or ""
        ):
            return True
        metrics = latest.get("metrics") or {}
        if metrics.get("training_sample_policy") != "all_available_sensor_readings":
            return True
        if int(metrics.get("training_dataset_version") or 0) != int(config.get("training_dataset_version") or 0):
            return True
        if AnfisModelService._stored_input_features(latest) != list(config.get("anfis_input_features") or []):
            return True
        for field in ("seed", "generations", "population"):
            if latest.get(field) != config.get(field):
                return True
        return False

    @staticmethod
    def _stored_input_features(latest: dict[str, Any]) -> list[str]:
        metrics = latest.get("metrics") or {}
        metric_features = metrics.get("anfis_input_features")
        if isinstance(metric_features, list) and metric_features:
            return [str(item) for item in metric_features]

        payload = latest.get("model_payload") or {}
        global_model = payload.get("global_model") or {}
        payload_features = global_model.get("input_names")
        if isinstance(payload_features, list) and payload_features:
            return [str(item) for item in payload_features]
        return []

    @staticmethod
    def _resolve_training_range(start_date: date | None, end_date: date | None) -> tuple[date, date]:
        if end_date is None:
            with get_connection() as conn:
                end_date = conn.execute("SELECT max(observed_date) FROM weather_hourly").fetchone()[0]
        end_date = end_date or date.today()
        return start_date or _add_months(end_date, -DEFAULT_ANFIS_TRAINING_MONTHS), end_date


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
