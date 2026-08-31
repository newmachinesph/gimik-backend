FROM python:3.12-slim

# Dependencies first, in their own layer: application code changes on every
# build and the wheel set almost never does.
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY seed ./seed
COPY scripts ./scripts

RUN pip install --no-cache-dir . \
    && adduser --system --group --no-create-home gora \
    && chown -R gora:gora /srv

# Not root. A process that only needs to read its own source and talk to
# Postgres has no business being able to write the filesystem it runs on.
USER gora

# Documentation only; the platform decides the real port through $PORT.
EXPOSE 8000

# --proxy-headers with a trusted forwarder is what makes request.client.host
# the caller rather than the load balancer. Without it every anonymous request
# shares one rate-limit bucket, which is the same as having none. The trusted
# set is deliberately configurable: "*" is correct only behind a proxy that
# always overwrites the header, which is true of Railway and Fly.
ENV FORWARDED_ALLOW_IPS="*"

# Migrations run before the first request rather than in a build step: the
# database is not reachable at build time, and a container that starts against
# an unmigrated schema fails in a much worse way than one that will not start.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
