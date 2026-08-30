# gora-backend-v2

The Gora corpus as an API: FastAPI + SQLAlchemy (async) + Alembic over
Postgres, with the catalog artwork served from the MinIO bucket the v1 stack
(`getgora-backend`) already seeded. **Not wired to the app yet** - gora-v2 runs
on its on-device demo store until the gateway seam is flipped.

## The shape

One table per corpus noun: users (+photos), somedays, keeps (with the two
visibility axes), passes, invites, friendships, hidden_users, groups
(+members), threads (+participants, messages), plans (+people), journal
entries, reports. The wire shapes in `app/schemas.py` mirror the app store's
types one for one.

The corpus rules live in the routers, not in prose: friendship is mutual,
declining and hiding are silent, visibility (Friends / Groups / Everyone-in-
your-city, as Duo / Group) gates both who appears under "Who's in" and who may
invite, locking creates the plan thread, done stamps the record.

## Images

`seed/catalog.json` is the same 522-concept catalog the app bundles. Every
slug's artwork already exists as `gora-catalog/catalog/<slug>.webp` in the v1
MinIO - seeding writes rows only, no image moves, and `catalog` responses hand
out plain URLs into that public bucket. User media stays private in
`gora-user-media` behind presigned URLs (ported from v1).

## Run it

The v1 MinIO must be up first (it owns the artwork):

    cd ../getgora-backend && docker compose up -d minio

Then:

    cp .env.example .env          # fill JWT_SECRET
    docker compose up -d postgres
    python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
    .venv/Scripts/python -m alembic upgrade head
    .venv/Scripts/python scripts/seed.py --verify-artwork
    .venv/Scripts/python -m uvicorn app.main:app --port 8100

`docker compose up -d` runs the API containerised instead (port 8100, reaching
the host's MinIO via `host.docker.internal`).

## Tests

    .venv/Scripts/python -m pytest

`tests/test_lifecycle.py` walks the whole journey over the wire - register ->
keep -> be seen -> invite -> accept -> thread -> lock -> done -> journal - plus
the social edges (mutual friendship, group membership vs friendship, quiet
hide/unhide, visibility gating). SQLite in memory, storage faked, the app
otherwise real.
