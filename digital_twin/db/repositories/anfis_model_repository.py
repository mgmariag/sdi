from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_twin.db.connection import get_connection


DEFAULT_MODEL_KEY = "anfis-default"
DEFAULT_SENSOR_SOURCE = "simulated_sensor"
ACTUAL_SENSOR_SOURCE = "actual_sensor"
ACTUATOR_FEEDBACK_SOURCE = "actuator_feedback"


def _query_sources(source: str | None) -> list[str]:
    if source == DEFAULT_SENSOR_SOURCE or source is None:
        return [DEFAULT_SENSOR_SOURCE, ACTUAL_SENSOR_SOURCE, ACTUATOR_FEEDBACK_SOURCE]
    return [source]


class AnfisModelRepository:
    """Persistence boundary for the latest trained ANFIS controller."""

    def latest(self, model_key: str = DEFAULT_MODEL_KEY) -> dict[str, Any] | None:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM anfis_models
                WHERE model_key = %(model_key)s
                ORDER BY trained_at DESC, id DESC
                LIMIT 1
                """,
                {"model_key": model_key},
            ).fetchone()
        return _json_ready(row) if row else None

    def sensor_watermark(self, source: str = DEFAULT_SENSOR_SOURCE) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT
                    count(*)::bigint AS sensor_reading_count,
                    max(recorded_at) AS sensor_readings_max_recorded_at
                FROM sensor_readings
                WHERE source = ANY(%(sources)s)
                """,
                {"sources": _query_sources(source)},
            ).fetchone()
        return _json_ready(row or {})

    def upsert(
        self,
        *,
        model_key: str,
        model_payload: dict[str, Any],
        metrics: dict[str, Any],
        sensor_source: str,
        sensor_watermark: dict[str, Any],
        train_samples: int,
        test_samples: int,
        fit_samples: int,
        calibration_samples: int,
        weighted_fit_samples: int,
        seed: int | None,
        generations: int,
        population: int,
        training_start_date: date | str | None,
        training_end_date: date | str | None,
    ) -> dict[str, Any]:
        params = {
            "model_key": model_key,
            "model_payload": Jsonb(model_payload),
            "metrics": Jsonb(metrics),
            "sensor_source": sensor_source,
            "sensor_reading_count": int(sensor_watermark.get("sensor_reading_count") or 0),
            "sensor_readings_max_recorded_at": sensor_watermark.get("sensor_readings_max_recorded_at"),
            "train_samples": int(train_samples),
            "test_samples": int(test_samples),
            "fit_samples": int(fit_samples),
            "calibration_samples": int(calibration_samples),
            "weighted_fit_samples": int(weighted_fit_samples),
            "seed": seed,
            "generations": int(generations),
            "population": int(population),
            "training_start_date": training_start_date,
            "training_end_date": training_end_date,
        }
        with get_connection(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                INSERT INTO anfis_models (
                    model_key, model_payload, metrics, sensor_source,
                    sensor_reading_count, sensor_readings_max_recorded_at,
                    train_samples, test_samples, fit_samples, calibration_samples,
                    weighted_fit_samples, seed, generations, population,
                    training_start_date, training_end_date
                )
                VALUES (
                    %(model_key)s, %(model_payload)s, %(metrics)s, %(sensor_source)s,
                    %(sensor_reading_count)s, %(sensor_readings_max_recorded_at)s,
                    %(train_samples)s, %(test_samples)s, %(fit_samples)s,
                    %(calibration_samples)s, %(weighted_fit_samples)s, %(seed)s,
                    %(generations)s, %(population)s, %(training_start_date)s,
                    %(training_end_date)s
                )
                ON CONFLICT (model_key) DO UPDATE SET
                    model_payload = EXCLUDED.model_payload,
                    metrics = EXCLUDED.metrics,
                    sensor_source = EXCLUDED.sensor_source,
                    sensor_reading_count = EXCLUDED.sensor_reading_count,
                    sensor_readings_max_recorded_at = EXCLUDED.sensor_readings_max_recorded_at,
                    train_samples = EXCLUDED.train_samples,
                    test_samples = EXCLUDED.test_samples,
                    fit_samples = EXCLUDED.fit_samples,
                    calibration_samples = EXCLUDED.calibration_samples,
                    weighted_fit_samples = EXCLUDED.weighted_fit_samples,
                    seed = EXCLUDED.seed,
                    generations = EXCLUDED.generations,
                    population = EXCLUDED.population,
                    training_start_date = EXCLUDED.training_start_date,
                    training_end_date = EXCLUDED.training_end_date,
                    trained_at = now(),
                    changed_at = now()
                RETURNING *
                """,
                params,
            ).fetchone()
            conn.commit()
        return _json_ready(row)


def _json_ready(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
