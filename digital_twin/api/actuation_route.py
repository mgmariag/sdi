from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from digital_twin.api.errors import ApiErrorMapper
from digital_twin.application.control_loop.runtime import RuntimeControlLoop

class ActuationRoutes:
    def __init__(
        self,
        control_loop: RuntimeControlLoop | None = None,
        error_mapper: ApiErrorMapper | None = None,
    ) -> None:
        self.router = APIRouter(prefix="/api/control-loop")
        self.control_loop = control_loop or RuntimeControlLoop()
        self.error_mapper = error_mapper or ApiErrorMapper()
        self.router.add_api_route("/prescriptions/prepare", self.prepare_next_day_prescriptions, methods=["POST"])
        self.router.add_api_route("/prescriptions/dispatch", self.dispatch_next_day_prescriptions, methods=["POST"])
        self.router.add_api_route("/actuations/run-due", self.run_due_actuations, methods=["POST"])
        self.router.add_api_route("/actuations/summary", self.actuation_summary, methods=["GET"])

    def prepare_next_day_prescriptions(self, target: date | None = Query(None)):
        try:
            return self.control_loop.prepare_next_day_prescriptions(target=target)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Prescription preparation failed") from exc

    def dispatch_next_day_prescriptions(self, target: date | None = Query(None)):
        try:
            return self.control_loop.dispatch_next_day_prescriptions(target=target)
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Prescription dispatch failed") from exc

    def run_due_actuations(self):
        try:
            return self.control_loop.run_due_actuation()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Actuator consumption failed") from exc

    def actuation_summary(self):
        try:
            return self.control_loop.actuation_summary()
        except Exception as exc:
            raise self.error_mapper.to_http_error(exc, 500, "Actuation summary failed") from exc
