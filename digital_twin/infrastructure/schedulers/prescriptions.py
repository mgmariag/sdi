from __future__ import annotations

import logging
import threading
import time as sleep_time
from datetime import datetime, timedelta

from digital_twin.application.clock import ApplicationClock
from digital_twin.application.control_loop.runtime import RuntimeControlLoop
from digital_twin.infrastructure.config import get_settings

logger = logging.getLogger(__name__)


class PrescriptionScheduler:
    """Publishes tomorrow's four irrigation prescriptions at the configured time."""

    def __init__(
        self,
        control_loop: RuntimeControlLoop | None = None,
        clock: ApplicationClock | None = None,
    ) -> None:
        self.clock = clock or ApplicationClock()
        self.control_loop = control_loop or RuntimeControlLoop(clock=self.clock)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        tz = self.clock.local_timezone()
        while True:
            next_run = self._next_dispatch_datetime(datetime.now(tz))
            seconds = max(1, int((next_run - datetime.now(tz)).total_seconds()))
            logger.info("Next irrigation prescription dispatch scheduled at %s", next_run.isoformat())
            sleep_time.sleep(seconds)
            try:
                result = self.control_loop.dispatch_next_day_prescriptions()
                logger.info("Dispatched irrigation prescriptions: %s", result)
            except Exception as exc:
                logger.warning("Irrigation prescription dispatch failed: %s", exc)

    @staticmethod
    def _next_dispatch_datetime(now: datetime) -> datetime:
        settings = get_settings()
        candidate = datetime.combine(now.date(), settings.prescription_dispatch_time, tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
