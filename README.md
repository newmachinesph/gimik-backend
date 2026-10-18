# gimik-backend

The API behind Gimik: FastAPI + SQLAlchemy (async) + Alembic over Postgres, with object storage over S3 (MinIO locally) for catalog artwork and user media. It serves the Gimik corpus - users, somedays, keeps, invites, friendships, groups, threads, plans and the journal - to the mobile app. (Gimik was formerly called Gora, which still shows up in some internal names.)

## Part of Gimik

Gimik turns "we should do that sometime" into a date on the calendar. See [gimik.app](https://gimik.app).

| Repo | What it is |
| --- | --- |
| [gimik-backend](https://github.com/newmachinesph/gimik-backend) | This API |
| [gimik-app](https://github.com/newmachinesph/gimik-app) | The Expo mobile app |
| [gimik-site](https://github.com/newmachinesph/gimik-site) | The Next.js marketing site |

## Tech stack

- Python 3.12+, FastAPI, Uvicorn
- SQLAlchemy 2 (async, asyncpg), Alembic, Postgres 16
- S3-compatible storage via boto3 (MinIO locally, presigned URLs for user media)
- Redis for rate limiting (fails open if unreachable)
- QStash + SMTP for transactional email (Mailpit locally)
- Sign in with Apple and Google ID-token verification, Argon2 password hashing, JWT sessions
- Ably (realtime tokens), RevenueCat (billing webhooks), Sentry (optional)
- pytest, pytest-asyncio, ruff

## Data model

One table per corpus noun: users (+photos), somedays, keeps (with two visibility axes), passes, invites, friendships, hidden_users, groups (+members), threads (+participants, messages), plans (+people), journal entries, reports. Wire shapes in `app/schemas.py` mirror the app store's types one for one.

The corpus rules live in the routers, not in prose:

- Friendship is mutual.
- Declining and hiding are silent.
- Visibility (Friends / Groups / Everyone in your city, as Duo / Group) gates both who appears under "Who's in" and who may invite.
- Locking a plan creates its thread.
- Marking a plan done stamps the record.

`seed/catalog.json` holds the 522-concept catalog the app bundles. Catalog artwork is served from a public bucket (`catalog/<slug>.webp`); user media stays private behind presigned URLs. Seeding writes rows only and does not move images.

## Getting started

Prerequisites: Docker with Compose, Python 3.12+.

1. Create the external volume that `docker-compose.yml` mounts for catalog artwork (it is declared external so `docker compose down -v` cannot delete it):

       docker volume create getgora-backend_minio-data

   A fresh volume is empty, so catalog image URLs will 404 until artwork is uploaded to the `catalog/` prefix of the catalog bucket. The API and tests do not need it.

2. Configure and start the stack (Postgres on 5433, MinIO on 9000/9001, Redis on 6379, Mailpit UI on 8026):

       cp .env.example .env          # set JWT_SECRET
       docker compose up -d

   The `api` service runs `alembic upgrade head` and then Uvicorn with `--reload`, so it is available at http://localhost:8100. Interactive docs are at `/docs` unless `ENVIRONMENT` is production.

3. Or run the API on the host against the compose services:

       docker compose up -d postgres minio redis mailpit
       python -m venv .venv
       .venv/Scripts/pip install -e ".[dev]"      # .venv/bin/pip on macOS/Linux
       .venv/Scripts/python -m alembic upgrade head
       .venv/Scripts/python scripts/seed.py       # catalog rows; --verify-artwork also checks each image exists
       .venv/Scripts/python -m uvicorn app.main:app --port 8100

### Environment variables

Names only; see `.env.example` for comments and defaults. Values are never committed.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL`, `DATABASE_CA_CERT_B64` | Postgres connection; optional base64 CA PEM for managed databases |
| `JWT_SECRET` | Signing secret for access and refresh tokens (required) |
| `JWT_ACCESS_TTL_MINUTES`, `JWT_REFRESH_TTL_DAYS`, `JWT_RESET_TTL_MINUTES`, `JWT_VERIFY_TTL_HOURS` | Token lifetimes |
| `S3_ENDPOINT_URL`, `S3_PUBLIC_URL`, `S3_CATALOG_PUBLIC_URL` | Storage endpoint, client-reachable host, optional public catalog root |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_CATALOG_BUCKET`, `S3_REGION` | Storage credentials, buckets, region |
| `ABLY_API_KEY`, `ABLY_TOKEN_TTL_SECONDS` | Realtime; blank disables it |
| `REDIS_URL` | Rate limiting; blank disables it |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `MAIL_FROM` | Outgoing mail; blank host disables delivery |
| `MODERATION_EMAIL` | Inbox for reports and blocks |
| `APP_BASE_URL`, `APP_SCHEME` | Link bases for emails and the app deep-link scheme |
| `QSTASH_URL`, `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY`, `QSTASH_EMAIL_CALLBACK_URL` | Queued email delivery and callback verification |
| `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE` | Error monitoring; blank disables it |
| `GOOGLE_MAPS_API_KEY` | Server-side place autocomplete |
| `OPENAI_API_KEY`, `PLACE_ASSESSMENT_MODEL`, `PLACE_SEARCH_RETENTION_DAYS` | Optional place assessment jobs |
| `REVENUECAT_API_KEY`, `REVENUECAT_WEBHOOK_AUTHORIZATION`, `REVENUECAT_SANDBOX_USER_IDS` | Billing lookup and webhook auth |
| `GOOGLE_CLIENT_ID`, `GOOGLE_IOS_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google sign-in; blank disables it |
| `ENVIRONMENT`, `CORS_ORIGINS` | Runtime mode and allowed origins |

### Email

Locally, mail goes to Mailpit (http://localhost:8026) and is sent via FastAPI background tasks. In production the API publishes jobs to QStash, which POSTs them back to `/internal/qstash/email`; the API verifies `Upstash-Signature` before sending over SMTP. Failed deliveries are retried after 60, 300 and 900 seconds.

## Seeding

    python scripts/seed_world.py [--reset]

Seeds nine demo people and one account that knows them, so a fresh database has something to look at. It is idempotent; `--reset` deletes the demo users and their data first. It prints a demo sign-in at the end. That password is for local development only and must never be used on a real deployment.

Other scripts in `scripts/`: `seed.py` (catalog rows), `seed_activities.py` (activity catalog from an export), `import_places.py` and `assess_places.py` (place catalog), `moderate.py` (`list`, `eject <handle>`), `folderize_media.py` (storage migration).

## Tests and the simulator

    .venv/Scripts/python -m pytest

Tests run against in-memory SQLite with storage faked and the app otherwise real. `tests/test_lifecycle.py` walks the whole journey over the wire - register, keep, be seen, invite, accept, thread, lock, done, journal - plus the social edges.

`scripts/simulate.py` drives the same loop with several people at once against a live stack (Postgres and MinIO running):

    .venv/Scripts/python scripts/simulate.py --base-url http://localhost:8100

## Project layout

    app/
      main.py         FastAPI app and middleware
      config.py       Settings from environment
      schemas.py      Wire shapes
      models/         SQLAlchemy models (domain, place catalog)
      routers/        auth, social, plans, threads, groups, feed, catalog, places,
                      media, billing, push, realtime, qstash_email
      services/       storage, email, ratelimit, moderation, billing, feed, push, auth providers
    alembic/          Migrations
    scripts/          Seeding, simulation, moderation and maintenance
    seed/             Catalog data
    tests/            pytest suite
    docker-compose.yml, Dockerfile

## Deployment

The API ships as a Docker image (see `Dockerfile`) that applies migrations on start.

## License

MIT. See [LICENSE](LICENSE).
