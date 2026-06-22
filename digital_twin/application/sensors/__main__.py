from __future__ import annotations

import argparse
import time as sleep_time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from digital_twin.application.sensors.placement import SensorPlacementService
from digital_twin.application.sensors.reading_cadence import (
    DEFAULT_SENSOR_READING_CADENCE,
    DEFAULT_HISTORY_START,
    LOCAL_TZ,
)
from digital_twin.application.sensors.service import SensorService
from digital_twin.domain.sensor import SensorSource
from digital_twin.infrastructure.config import get_settings


def run_sensor_service() -> None:
    settings = get_settings()
    source = settings.sensor_source
    sensor_service = SensorService()
    placement = SensorPlacementService().ensure_default_if_missing()
    print(f"Sensor placement ready: {placement.get('sensor_count', 0)} sensors", flush=True)
    if settings.sensor_seed_history_on_startup:
        summary = sensor_service.ensure_tiered_history(source=source, cleanup=settings.sensor_cleanup_enabled)
        print(f"Tiered sensor history ready: {summary}", flush=True)

    due = sensor_service.generate_due(source=source)
    if due:
        print(f"Generated due sensor readings: {due}", flush=True)

    if settings.sensor_cleanup_enabled and not settings.sensor_seed_history_on_startup:
        cleanup = sensor_service.cleanup(source=source)
        print(f"Sensor aggregate cleanup completed: {cleanup}", flush=True)

    while True:
        next_run = DEFAULT_SENSOR_READING_CADENCE.next_scheduled_datetime(datetime.now(LOCAL_TZ))
        seconds = max(1, int((next_run - datetime.now(LOCAL_TZ)).total_seconds()))
        print(f"Next sensor reading scheduled at {next_run.isoformat()}", flush=True)
        sleep_time.sleep(seconds)
        result = sensor_service.generate_at(next_run, source=source)
        print(f"Generated scheduled sensor readings: {result}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and maintain simulated pot sensor readings.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser("seed-history")
    seed_parser.add_argument("--start", default=DEFAULT_HISTORY_START.isoformat())
    seed_parser.add_argument("--end", default=DEFAULT_SENSOR_READING_CADENCE.today_local().isoformat())
    seed_parser.add_argument("--source", default=SensorSource.DEFAULT.value)

    tiered_parser = subparsers.add_parser("seed-tiered")
    tiered_parser.add_argument("--start", default=None)
    tiered_parser.add_argument("--end-at", default=None)
    tiered_parser.add_argument("--source", default=SensorSource.DEFAULT.value)
    tiered_parser.add_argument("--append", action="store_true")

    once_parser = subparsers.add_parser("run-once")
    once_parser.add_argument(
        "--at",
        default=datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0).isoformat(),
    )
    once_parser.add_argument("--source", default=SensorSource.DEFAULT.value)

    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("--source", default=None)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--source", default=None)

    subparsers.add_parser("service")

    args = parser.parse_args()
    sensor_service = SensorService()
    if args.command == "seed-history":
        result = sensor_service.seed_history(
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end),
            source=args.source,
        )
        print(_json_ready(result))
    elif args.command == "seed-tiered":
        result = sensor_service.seed_tiered_history(
            start_date=date.fromisoformat(args.start) if args.start else None,
            end_at=datetime.fromisoformat(args.end_at) if args.end_at else None,
            source=args.source,
            replace_existing=not args.append,
        )
        print(_json_ready(result))
    elif args.command == "run-once":
        result = sensor_service.generate_at(datetime.fromisoformat(args.at), source=args.source)
        print(_json_ready(result))
    elif args.command == "summary":
        print(sensor_service.summary(source=args.source))
    elif args.command == "cleanup":
        print(_json_ready(sensor_service.cleanup(source=args.source)))
    elif args.command == "service":
        run_sensor_service()


def _json_ready(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


if __name__ == "__main__":
    main()
