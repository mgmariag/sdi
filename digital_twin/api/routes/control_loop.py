from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from digital_twin.api.errors import http_error
from digital_twin.application.control_loop.runtime import RuntimeControlLoop

router = APIRouter(prefix="/api/control-loop")
control_loop = RuntimeControlLoop()


@router.post("/prescriptions/prepare")
def prepare_next_day_prescriptions(target: date | None = Query(None)):
    try:
        return control_loop.prepare_next_day_prescriptions(target=target)
    except Exception as exc:
        raise http_error(exc, 500, "Prescription preparation failed") from exc


@router.post("/prescriptions/dispatch")
def dispatch_next_day_prescriptions(target: date | None = Query(None)):
    try:
        return control_loop.dispatch_next_day_prescriptions(target=target)
    except Exception as exc:
        raise http_error(exc, 500, "Prescription dispatch failed") from exc


@router.post("/actuations/run-due")
def run_due_actuations():
    try:
        return control_loop.run_due_actuation()
    except Exception as exc:
        raise http_error(exc, 500, "Actuator consumption failed") from exc


@router.get("/actuations/summary")
def actuation_summary():
    try:
        return control_loop.actuation_summary()
    except Exception as exc:
        raise http_error(exc, 500, "Actuation summary failed") from exc
