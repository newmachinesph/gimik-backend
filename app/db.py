"""Engine and session. Connection arguments follow from the notes in config.py."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()


def _connect_args() -> dict[str, Any]:
    args: dict[str, Any] = {}
    if settings.sqlalchemy_url.startswith("sqlite"):
        return args
    if settings.requires_tls:
        args["ssl"] = ssl.create_default_context()
    if settings.is_pgbouncer:
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0
    return args


engine = create_async_engine(
    settings.sqlalchemy_url,
    connect_args=_connect_args(),
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session. Commits are explicit; anything unhandled rolls back."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
