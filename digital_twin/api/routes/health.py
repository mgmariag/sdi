from __future__ import annotations

from fastapi import APIRouter

from digital_twin.api.errors import http_error
from digital_twin.db.pot_repository import PotRepository


router = APIRouter()
repository = PotRepository()


@router.get("/")
def root() -> dict[str, str]:
    return {"message": "Digital Twin Irrigation API running"}


@router.get("/api/hello")
def hello() -> dict[str, str]:
    return {"message": "Select an experiment to begin"}


@router.get("/api/db/health")
def database_health():
    try:
        return repository.health()
    except Exception as exc:
        raise http_error(exc, 503, "Database unavailable") from exc

