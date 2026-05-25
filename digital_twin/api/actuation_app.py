from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from digital_twin.api.routes.actuation import service_router
from digital_twin.db.actuation_repository import ActuationRepository
from digital_twin.db.schema import initialize_database


def initialize_actuation_service() -> None:
    initialize_database()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_actuation_service()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Irrigation Actuation Service", lifespan=lifespan)
    app.include_router(service_router)

    @app.get("/health")
    def health():
        try:
            return ActuationRepository().summary()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    return app


app = create_app()
