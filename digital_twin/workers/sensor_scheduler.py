from __future__ import annotations

from datetime import datetime, timedelta
import logging
import threading
import time as sleep_time

from digital_twin.core.config import get_settings
from digital_twin.core.time import local_timezone
from digital_twin.services.sensor_service import SensorService
from digital_twin.services.sensor_readings import next_scheduled_sensor_datetime


logger = logging.getLogger("digital_twin.sensor_scheduler")


class SensorScheduler:
    """Runs simulated sensor generation and aggregate cleanup loops."""

    def __init__(self, service: SensorService | None = None) -> None:
        self.service = service or SensorService()
        self._thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None

    def start(self, source: str) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, args=(source,), daemon=True)
            self._thread.start()

        if get_settings().sensor_cleanup_enabled:
            self.start_cleanup(source)

    def start_cleanup(self, source: str) -> None:
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._cleanup_thread = threading.Thread(target=self._cleanup_loop, args=(source,), daemon=True)
            self._cleanup_thread.start()

    def _loop(self, source: str) -> None:
        tz = local_timezone()
        while True:
            next_run = next_scheduled_sensor_datetime(datetime.now(tz))
            seconds = max(1, int((next_run - datetime.now(tz)).total_seconds()))
            logger.info("Next sensor reading scheduled at %s", next_run.isoformat())
            sleep_time.sleep(seconds)
            try:
                result = self.service.generate_at(next_run, source=source)
                logger.info("Generated scheduled sensor readings: %s", result)
            except Exception as exc:
                logger.warning("Scheduled sensor reading failed: %s", exc)

    def _cleanup_loop(self, source: str) -> None:
        tz = local_timezone()
        while True:
            next_run = self._next_cleanup_datetime(datetime.now(tz))
            seconds = max(1, int((next_run - datetime.now(tz)).total_seconds()))
            logger.info("Next sensor aggregate cleanup scheduled at %s", next_run.isoformat())
            sleep_time.sleep(seconds)
            try:
                result = self.service.cleanup(source=source)
                logger.info("Sensor aggregate cleanup completed: %s", result)
            except Exception as exc:
                logger.warning("Sensor aggregate cleanup failed: %s", exc)

    @staticmethod
    def _next_cleanup_datetime(now: datetime) -> datetime:
        settings = get_settings()
        candidate = datetime.combine(now.date(), settings.sensor_cleanup_time, tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

