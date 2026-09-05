from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.routers import (
    auth,
    catalog,
    feed,
    groups,
    media,
    places,
    plans,
    realtime,
    social,
    threads,
)

settings = get_settings()
log = logging.getLogger("gora")

# Before anything else can fail. `send_default_pii` stays off: this API handles
# people's messages and locations, and the useful part of a report is the stack,
# not the payload that produced it.
if settings.sentry_enabled:
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly at boot rather than on the first request that needs storage.
    try:
        from app.services import storage

        storage.ensure_bucket()
        storage.ensure_catalog_bucket()
    except Exception:
        log.exception("object storage is unreachable")
    yield
    from app.services.ratelimit import close_redis

    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="Gora API v2",
    version="2.0.0",
    docs_url=None if settings.is_production else "/docs",
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(feed.router)
app.include_router(social.router)
app.include_router(groups.router)
app.include_router(threads.router)
app.include_router(plans.router)
app.include_router(media.router)
app.include_router(places.router)
app.include_router(realtime.router)


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Reports the dependencies, not just the process."""
    checks: dict[str, str] = {"api": "ok"}

    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as error:  # noqa: BLE001 - health reports failures, never raises them
        checks["database"] = f"error: {type(error).__name__}"

    try:
        from app.services import storage

        storage.ensure_bucket()
        storage.ensure_catalog_bucket()
        checks["storage"] = "ok"
    except Exception as error:  # noqa: BLE001 - health reports failures, never raises them
        checks["storage"] = f"error: {type(error).__name__}"

    healthy = all(value == "ok" for key, value in checks.items() if key != "status")
    checks["status"] = "ok" if healthy else "degraded"
    return checks
