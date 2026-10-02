"""Settings. The Neon/asyncpg notes are inherited from v1 and still apply:

1. `sslmode` and `channel_binding` are libpq query parameters that asyncpg does
   not understand; they are stripped and TLS is requested via connect_args.
2. Neon's `-pooler` host is PgBouncer in transaction mode, which cannot hold a
   prepared statement across checkouts; statement caching is turned off there.

Locally the database is the compose Postgres and none of that triggers.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# libpq understands these; asyncpg does not.
_LIBPQ_ONLY = {"sslmode", "channel_binding", "sslrootcert", "options", "target_session_attrs"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    # DigitalOcean's managed database uses a cluster CA outside the system
    # trust store. Keep its public PEM as base64 in the service environment.
    database_ca_cert_b64: str = Field(default="", alias="DATABASE_CA_CERT_B64", repr=False)

    jwt_secret: str = Field(alias="JWT_SECRET")
    jwt_access_ttl_minutes: int = Field(default=30, alias="JWT_ACCESS_TTL_MINUTES")
    jwt_refresh_ttl_days: int = Field(default=30, alias="JWT_REFRESH_TTL_DAYS")
    jwt_reset_ttl_minutes: int = Field(default=30, alias="JWT_RESET_TTL_MINUTES")
    # A verification link is not a credential; it can afford to be patient.
    jwt_verify_ttl_hours: int = Field(default=72, alias="JWT_VERIFY_TTL_HOURS")
    jwt_algorithm: str = "HS256"

    s3_endpoint_url: str = Field(alias="S3_ENDPOINT_URL")
    s3_access_key: str = Field(alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(alias="S3_SECRET_KEY")
    s3_bucket: str = Field(default="gora-user-media", alias="S3_BUCKET")
    # The catalog bucket predates this API: the v1 stack seeded it with one WebP
    # per catalog slug (`catalog/<slug>.webp`), publicly readable. This API reads
    # from it and never writes to it.
    s3_catalog_bucket: str = Field(default="gora-catalog", alias="S3_CATALOG_BUCKET")
    s3_region: str = Field(default="auto", alias="S3_REGION")
    s3_public_url: str = Field(default="", alias="S3_PUBLIC_URL")
    # Public bucket root, including any CDN prefix, without the bucket name.
    s3_catalog_public_url: str = Field(default="", alias="S3_CATALOG_PUBLIC_URL")

    # Google Maps Platform, server-side only. Places Autocomplete + Details.
    google_maps_api_key: str = Field(default="", alias="GOOGLE_MAPS_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY", repr=False)
    place_assessment_model: str = Field(default="gpt-4o-mini", alias="PLACE_ASSESSMENT_MODEL")
    place_search_retention_days: int = Field(default=90, ge=1, le=365, alias="PLACE_SEARCH_RETENTION_DAYS")

    # V1 read-only customer lookup accepts a platform public SDK key too.
    # Never use the V2 management key here or expose a secret key to the app.
    revenuecat_api_key: str = Field(default="", alias="REVENUECAT_API_KEY", repr=False)
    revenuecat_webhook_authorization: str = Field(default="", alias="REVENUECAT_WEBHOOK_AUTHORIZATION", repr=False)
    # Explicit QA accounts only; sandbox purchases never grant everyone production access.
    revenuecat_sandbox_user_ids: str = Field(default="", alias="REVENUECAT_SANDBOX_USER_IDS")

    # Google sign-in. A Google ID token names the client it was minted for in
    # its `aud` claim, and that client differs by platform: the Android library
    # must be given the *web* client id to get a token at all, while iOS uses
    # its own. Both are therefore valid audiences, and an empty one is dropped
    # rather than accepted - a blank audience would match nothing on purpose.
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_ios_client_id: str = Field(default="", alias="GOOGLE_IOS_CLIENT_ID")

    # Sign in with Apple. A native iOS identity token's `aud` is the bundle id.
    # The key trio is only for exchanging and revoking tokens, which App Store
    # rules require on account deletion; sign-in itself works without it.
    apple_bundle_id: str = Field(default="com.sumnora.gimik", alias="APPLE_BUNDLE_ID")
    apple_team_id: str = Field(default="", alias="APPLE_TEAM_ID")
    apple_key_id: str = Field(default="", alias="APPLE_KEY_ID")
    apple_private_key: str = Field(default="", alias="APPLE_PRIVATE_KEY", repr=False)

    # Realtime. The root key never leaves this process: clients are handed
    # short-lived tokens scoped to their own channels.
    ably_api_key: str = Field(default="", alias="ABLY_API_KEY")
    ably_token_ttl_seconds: int = Field(default=3600, alias="ABLY_TOKEN_TTL_SECONDS")

    # Rate limiting. Redis over its own protocol - the compose service locally,
    # a Railway Redis plugin later. An absent URL disables limiting rather than
    # failing closed, because a broken limiter must not take the API down.
    redis_url: str = Field(default="", alias="REDIS_URL")

    # QStash calls the API to deliver transactional mail. Redis remains in use
    # for rate limiting, but no email worker process is needed.
    qstash_url: str = Field(default="https://qstash-us-east-1.upstash.io", alias="QSTASH_URL")
    qstash_token: str = Field(default="", alias="QSTASH_TOKEN", repr=False)
    qstash_current_signing_key: str = Field(
        default="", alias="QSTASH_CURRENT_SIGNING_KEY", repr=False
    )
    qstash_next_signing_key: str = Field(default="", alias="QSTASH_NEXT_SIGNING_KEY", repr=False)
    qstash_email_callback_url: str = Field(default="", alias="QSTASH_EMAIL_CALLBACK_URL")

    # Mail. Mailpit locally (:1025, no auth, no TLS); a real relay later.
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_use_tls: bool = Field(default=False, alias="SMTP_USE_TLS")
    mail_from: str = Field(default="Gimik <hello@gimik.ph>", alias="MAIL_FROM")
    # Where reports and blocks land. A person reads this inbox daily: App Store
    # guideline 1.2 gives 24 hours to act on a report.
    moderation_email: str = Field(default="support@gimik.app", alias="MODERATION_EMAIL")
    app_base_url: str = Field(default="http://localhost:8100", alias="APP_BASE_URL")
    # The deep link a reset email points at, opened by the app itself.
    app_scheme: str = Field(default="gimik", alias="APP_SCHEME")

    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")
    sentry_traces_sample_rate: float = Field(default=0.1, alias="SENTRY_TRACES_SAMPLE_RATE")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> Settings:
        """RFC 7518 puts the floor for HMAC-SHA256 at 32 bytes.

        A short secret is a weak signing key, and every access token, refresh
        token and password-reset link in the system rests on it. Development is
        warned; production refuses to boot, because the one place this must not
        be discovered is after deployment.
        """
        if len(self.jwt_secret) >= 32:
            return self
        message = (
            f"JWT_SECRET is {len(self.jwt_secret)} bytes; RFC 7518 wants at least 32 "
            "for HMAC-SHA256."
        )
        if self.is_production:
            raise ValueError(message)
        logging.getLogger("gimik.config").warning("%s Fine locally, not for deployment.", message)
        return self

    @model_validator(mode="after")
    def _check_qstash_mail(self) -> Settings:
        configured = (
            self.qstash_token,
            self.qstash_current_signing_key,
            self.qstash_next_signing_key,
            self.qstash_email_callback_url,
        )
        if any(configured) and not all(configured):
            raise ValueError("QStash email requires token, both signing keys and callback URL")
        if self.is_production and self.mail_enabled and not all(configured):
            raise ValueError("Production email requires QStash configuration")
        if all(configured) and not (
            self.qstash_url.startswith("https://")
            and self.qstash_email_callback_url.startswith("https://")
        ):
            raise ValueError("QStash and email callback URLs must use HTTPS")
        return self

    @property
    def realtime_enabled(self) -> bool:
        return bool(self.ably_api_key)

    @property
    def rate_limiting_enabled(self) -> bool:
        return bool(self.redis_url)

    @property
    def sentry_enabled(self) -> bool:
        return bool(self.sentry_dsn)

    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def qstash_enabled(self) -> bool:
        return bool(self.qstash_token and self.qstash_email_callback_url)

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        """The URL with the asyncpg driver and the libpq-only parameters removed."""
        parts = urlsplit(self.database_url)
        if parts.scheme.startswith("sqlite"):
            return self.database_url
        scheme = "postgresql+asyncpg"
        kept = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _LIBPQ_ONLY]
        return urlunsplit((scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))

    @property
    def google_audiences(self) -> list[str]:
        """Client ids a Google ID token may be addressed to. Empty disables it."""
        return [c for c in (self.google_client_id, self.google_ios_client_id) if c]

    @property
    def apple_revocation_enabled(self) -> bool:
        return bool(self.apple_team_id and self.apple_key_id and self.apple_private_key)

    @property
    def requires_tls(self) -> bool:
        query = dict(parse_qsl(urlsplit(self.database_url).query))
        sslmode = query.get("sslmode", "").lower()
        if sslmode in {"disable", "allow"}:
            return False
        return bool(sslmode) or "neon.tech" in urlsplit(self.database_url).netloc

    @property
    def is_pgbouncer(self) -> bool:
        # Neon's pooler is a "-pooler." host; DigitalOcean's pools listen on 25061.
        parts = urlsplit(self.database_url)
        return "-pooler." in parts.netloc or parts.port == 25061


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
