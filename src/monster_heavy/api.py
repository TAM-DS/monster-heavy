"""Read-only local audit API. Production authentication is outside v1 scope."""

import os
from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from monster_heavy.persistence.audit import AuditStore


def create_app(dsn: str | None = None) -> FastAPI:
    app = FastAPI(title="Monster Heavy audit", docs_url=None, redoc_url=None, openapi_url=None)

    def store():
        return AuditStore(dsn or os.environ["DATABASE_URL"])

    def response(value):
        # Preserve exact decimal values, including cash, quantities and measured seconds.
        return JSONResponse(jsonable_encoder(value, custom_encoder={Decimal: str}))

    @app.get("/health")
    def health():
        return {"status": "alive"}

    @app.get("/ready")
    def ready():
        try:
            return store().ready()
        except (psycopg.Error, RuntimeError, KeyError) as exc:
            raise HTTPException(status_code=503, detail="Database not ready") from exc

    @app.get("/metrics")
    def metrics():
        return response(store().metrics())

    @app.get("/attempts/{attempt_id}")
    def attempt(attempt_id: UUID):
        result = store().reconstruct(attempt_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Attempt not found")
        return response(result)

    return app


app = create_app()
