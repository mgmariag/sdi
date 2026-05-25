from __future__ import annotations

from typing import Any

from database import get_database_health, get_pot_summary, list_pots


class PotRepository:
    """Read access for pot inventory and database health summaries."""

    def health(self) -> dict[str, Any]:
        return get_database_health()

    def summary(self) -> dict[str, Any]:
        return get_pot_summary()

    def list(self, limit: int = 50, offset: int = 0, size_class: str | None = None, plant_type: str | None = None) -> list[dict[str, Any]]:
        return list_pots(limit=limit, offset=offset, size_class=size_class, plant_type=plant_type)

