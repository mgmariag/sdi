from __future__ import annotations

from fastapi import APIRouter, Query

from digital_twin.api.errors import http_error
from digital_twin.services.actuation import IrrigationActuationService


api_router = APIRouter(prefix="/api/actuation")
service_router = APIRouter(prefix="/actuation")
service = IrrigationActuationService()


@api_router.get("/summary")
def api_actuation_summary():
    try:
        return service.summary()
    except Exception as exc:
        raise http_error(exc, 503, "Irrigation actuation unavailable") from exc


@service_router.get("/summary")
def service_actuation_summary():
    try:
        return service.summary()
    except Exception as exc:
        raise http_error(exc, 503, "Irrigation actuation unavailable") from exc


@service_router.post("/run-due")
def run_due_actuations(
    actuator_node: str = Query("irrigation-actuator"),
    limit: int = Query(100, ge=1, le=1000),
):
    try:
        return service.run_due(actuator_node=actuator_node, limit=limit)
    except Exception as exc:
        raise http_error(exc, 500, "Irrigation actuation failed") from exc
