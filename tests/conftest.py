"""Test harness: SQLite in memory, storage faked, the app otherwise real.

Environment is pinned before anything imports app.config, because Settings is
cached and reads the process env exactly once.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("S3_ENDPOINT_URL", "http://minio.test:9000")
os.environ.setdefault("S3_ACCESS_KEY", "test")
os.environ.setdefault("S3_SECRET_KEY", "test")

# Overwritten rather than defaulted: the container these run in carries real
# credentials from .env, and a test suite must not reach the network. A shared
# rate limiter in particular would make the suite order-dependent and fail the
# moment a bucket is spent - which is exactly how this was discovered.
# GOOGLE_MAPS_API_KEY is blanked for the same reason: an empty key makes
# _provider_search return [] without ever opening a socket.
os.environ["REDIS_URL"] = ""
os.environ["GOOGLE_MAPS_API_KEY"] = ""
os.environ["ABLY_API_KEY"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SENTRY_DSN"] = ""
# Pinned rather than blanked: blank would disable Google sign-in entirely and
# the endpoint's own tests could not run. A fake id keeps the audience check
# live while guaranteeing no real client is ever accepted here.
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["GOOGLE_IOS_CLIENT_ID"] = ""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.models import Base, Someday
from app.services import images, storage


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch: pytest.MonkeyPatch):
    """No MinIO in tests; URLs are deterministic strings."""
    monkeypatch.setattr(storage, "ensure_bucket", lambda: None)
    monkeypatch.setattr(storage, "ensure_catalog_bucket", lambda: None)
    monkeypatch.setattr(storage, "presign_upload", lambda key, ct, expires=900: f"https://store.test/put/{key}")
    monkeypatch.setattr(storage, "presign_download", lambda key, expires=900: f"https://store.test/get/{key}")
    monkeypatch.setattr(storage, "delete_object", lambda key: None)
    monkeypatch.setattr(images, "process_upload", images.normalized_key)


@pytest.fixture
async def client():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    TestSession = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with TestSession() as session:
            yield session

    from app.main import app

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.sessionmaker = TestSession  # handed to tests that need direct DB access
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
async def somedays(client):
    """A tiny catalog: one duo-friendly concept, one group concept."""
    async with client.sessionmaker() as session:
        reef = Someday(
            slug="dive-a-coral-reef", title="Dive a coral reef", category="Water", party="duo"
        )
        lights = Someday(
            slug="see-an-aurora", title="See an aurora", category="Nature", party="group"
        )
        session.add_all([reef, lights])
        await session.commit()
        return {"reef": str(reef.id), "lights": str(lights.id)}


async def register(client, email: str, name: str, city: str | None = None) -> dict[str, str]:
    """Registers and returns auth headers; city is set via the profile patch."""
    response = await client.post(
        "/auth/register",
        json={"email": email, "password": "long-enough-pass", "name": name, "gender": "woman"},
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if city is not None:
        patched = await client.patch("/auth/me", json={"city": city}, headers=headers)
        assert patched.status_code == 200, patched.text
    return headers


async def grant_plus(client, headers: dict[str, str], *, days: int = 30) -> None:
    """Entitle the account behind these headers.

    Writes the column directly rather than going through a purchase: there is
    no endpoint that grants Plus, and there should not be - only the RevenueCat
    webhook ever moves this, and the webhook is not what these tests are about.
    """
    import datetime as dt

    from sqlalchemy import select

    from app.models import User
    from app.security import decode_token

    token = headers["Authorization"].split(" ", 1)[1]
    user_id = uuid.UUID(decode_token(token, expect="access")["sub"])
    async with client.sessionmaker() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        assert user is not None
        user.plus_until = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
        user.tier = "plus"
        await session.commit()


@pytest.fixture
def make_plus_user(make_user, client):
    """A registered account that is entitled to everything."""

    async def _make(name: str, city: str | None = None) -> dict[str, str]:
        headers = await make_user(name, city)
        await grant_plus(client, headers)
        return headers

    return _make


@pytest.fixture
def make_user(client):
    counter = iter(range(1, 1000))

    async def _make(name: str, city: str | None = None) -> dict[str, str]:
        return await register(client, f"user{next(counter)}-{uuid.uuid4().hex[:6]}@test.dev", name, city)

    return _make
