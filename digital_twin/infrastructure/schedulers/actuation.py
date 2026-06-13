from __future__ import annotations

import logging
import threading
import time as sleep_time

from digital_twin.application.control_loop.runtime import RuntimeControlLoop
from digital_twin.core.config import get_settings

logger = logging.getLogger(__name__)


class ActuationScheduler:
    """Consumes due irrigation prescription windows through the runtime control loop."""

    def __init__(self, control_loop: RuntimeControlLoop | None = None) -> None:
        self.control_loop = control_loop or RuntimeControlLoop()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                result = self.control_loop.run_due_actuation()
                if result["materializedCount"] or result["completedCount"] or result["failedCount"]:
                    logger.info("Actuator consumption result: %s", result)
            except Exception as exc:
                logger.warning("Actuator consumption failed: %s", exc)
            sleep_time.sleep(max(1, get_settings().actuation_poll_seconds))
