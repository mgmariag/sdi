from __future__ import annotations

from fastapi import APIRouter, Query

from digital_twin.api.errors import http_error
from digital_twin.db.pot_repository import PotRepository


router = APIRouter(prefix="/api/pots")
repository = PotRepository()


@router.get("/summary")
def pots_summary():
    try:
        return repository.summary()
    except Exception as exc:
        raise http_error(exc, 503, "Database unavailable") from exc


@router.get("")
def pots(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    size_class: str | None = Query(None),
    plant_type: str | None = Query(None),
):
    try:
        return {
            "items": repository.list(
                limit=limit,
                offset=offset,
                size_class=size_class,
                plant_type=plant_type,
            )
        }
    except Exception as exc:
        raise http_error(exc, 503, "Database unavailable") from exc

