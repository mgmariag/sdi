from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from typing import Any

from digital_twin.application.experiments.anfis_model_service import (
    DEFAULT_ANFIS_GENERATIONS,
    DEFAULT_ANFIS_POPULATION,
    AnfisModelService,
)
from digital_twin.infrastructure.config import get_settings
from digital_twin.infrastructure.database.repositories.anfis_model_repository import (
    DEFAULT_MODEL_KEY,
)

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    args = _parse_args(argv)
    service = AnfisModelService()
    try:
        result = service.train_if_needed(
            model_key=args.model_key,
            start_date=args.start,
            end_date=args.end,
            seed=args.seed,
            generations=args.generations,
            population=args.population,
            force=args.force,
        )
    except Exception:
        logger.exception("ANFIS training worker failed")
        return 1

    logger.info("ANFIS training worker result: %s", result)
    print(json.dumps(_json_ready(result), indent=2, sort_keys=True))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Train and persist the ANFIS model when sensor readings changed.")
    parser.add_argument("--model-key", default=os.getenv("ANFIS_MODEL_KEY", DEFAULT_MODEL_KEY))
    parser.add_argument("--start", type=date.fromisoformat, default=_env_date("ANFIS_TRAINING_START"))
    parser.add_argument("--end", type=date.fromisoformat, default=_env_date("ANFIS_TRAINING_END"))
    parser.add_argument("--seed", type=int, default=_env_int("ANFIS_TRAINING_SEED", settings.default_scenario_seed))
    parser.add_argument("--generations", type=int, default=_env_int("ANFIS_GENERATIONS", DEFAULT_ANFIS_GENERATIONS))
    parser.add_argument("--population", type=int, default=_env_int("ANFIS_POPULATION", DEFAULT_ANFIS_POPULATION))
    parser.add_argument("--force", action="store_true", default=_env_bool("ANFIS_TRAIN_FORCE", False))
    return parser.parse_args(argv)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_date(name: str) -> date | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return date.fromisoformat(value.strip())


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


if __name__ == "__main__":
    raise SystemExit(main())

