from __future__ import annotations

import logging
from datetime import date
from typing import Any

from digital_twin.infrastructure.database.repositories.experiment_repository import (
    ExperimentRunRepository,
)


class ExperimentRunHistory:
    """Persists and formats experiment run history records."""

    def __init__(
        self,
        repository: ExperimentRunRepository | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository or ExperimentRunRepository()
        self.logger = logger or logging.getLogger("digital_twin.application.experiments")

    def store(
        self,
        experiment_type: str,
        start: date,
        end: date,
        parameters: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        try:
            row = self.repository.create(
                experiment_type=experiment_type,
                start_date=start,
                end_date=end,
                parameters=parameters,
                result=result,
            )
        except Exception as exc:
            self.logger.warning("Failed to persist %s experiment run for %s..%s: %s", experiment_type, start, end, exc)
            return
        result.setdefault("summary", {})["experimentRunId"] = row["id"]
        result["summary"]["experimentRunSavedAt"] = row["created_at"].isoformat()
        result["summary"]["experimentRunStartedAt"] = row["started_at"].isoformat()
        result["summary"]["experimentRunCompletedAt"] = row["completed_at"].isoformat()

    def list_runs(self, experiment_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return [
            self._format(row)
            for row in self.repository.latest(experiment_type=experiment_type, limit=limit)
        ]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.repository.get(run_id)
        return self._format(row, include_payload=True) if row else None

    @staticmethod
    def _format(row: dict[str, Any], include_payload: bool = False) -> dict[str, Any]:
        output = {
            "id": row["id"],
            "experimentType": row["experiment_type"],
            "startDate": row["start_date"].isoformat(),
            "endDate": row["end_date"].isoformat(),
            "computedAt": row["computed_at"].isoformat(),
            "startedAt": row["started_at"].isoformat(),
            "completedAt": row["completed_at"].isoformat(),
            "createdAt": row["created_at"].isoformat(),
            "parameters": row.get("parameters") or {},
            "summary": row.get("summary") or {},
        }
        if include_payload:
            output["payload"] = row.get("payload") or {}
        return output
