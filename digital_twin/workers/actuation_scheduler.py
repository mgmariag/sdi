from __future__ import annotations

import logging
import threading
import time as sleep_time

from digital_twin.core.config import get_settings
from digital_twin.services.irrigation_service import IrrigationActuationService


logger = logging.getLogger("digital_twin.actuation_scheduler")


class ActuationScheduler:
    """Consumes due irrigation prescription windows through the actuator service."""

    def __init__(self, service: IrrigationActuationService | None = None) -> None:
        self.service = service or IrrigationActuationService()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                result = self.service.run_due_prescription_windows()
                if result["materializedCount"] or result["completedCount"] or result["failedCount"]:
                    logger.info("Actuator consumption result: %s", result)
            except Exception as exc:
                logger.warning("Actuator consumption failed: %s", exc)
            sleep_time.sleep(max(1, get_settings().actuation_poll_seconds))
