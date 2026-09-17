"""Transactional mail, queued in Redis and delivered by an RQ worker.

Production requests persist each message before returning. The worker retries
temporary SMTP failures on an increasing schedule. Local development without
Redis keeps the old FastAPI background-task path so Mailpit remains convenient.

The copy lives here rather than in a template engine because there are two
messages. When there is a third, this becomes a directory of templates.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from functools import lru_cache

from fastapi import BackgroundTasks
from redis import Redis
from rq import Queue, Retry
from rq.serializers import JSONSerializer

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("gimik.email")

TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 5
JOB_TIMEOUT_SECONDS = 30
RETRY_INTERVALS_SECONDS = [60, 300, 900]


@lru_cache
def _queue() -> Queue:
    connection = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    return Queue(settings.email_queue_name, connection=connection, serializer=JSONSerializer)


def deliver_email(to: str, subject: str, text: str) -> None:
    """RQ entry point. SMTP errors deliberately escape so RQ can retry them."""
    if not settings.mail_enabled:
        log.info("mail disabled; skipped %r", subject)
        return
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_SECONDS) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    log.info("sent %r", subject)


def enqueue_email(to: str, subject: str, text: str, *, delay_seconds: int = 0) -> str:
    """Persist an email job and return its ID.

    A non-zero delay uses RQ's scheduled registry. The worker must run with
    ``--with-scheduler`` so both delayed sends and delayed retries are promoted.
    """
    args = (deliver_email, to, subject, text)
    options = {
        "job_timeout": JOB_TIMEOUT_SECONDS,
        "result_ttl": 86_400,
        "failure_ttl": 604_800,
        "retry": Retry(max=len(RETRY_INTERVALS_SECONDS), interval=RETRY_INTERVALS_SECONDS),
    }
    if delay_seconds > 0:
        job = _queue().enqueue_in(timedelta(seconds=delay_seconds), *args, **options)
    else:
        job = _queue().enqueue(*args, **options)
    return job.id


def _deliver_safely(to: str, subject: str, text: str) -> None:
    try:
        deliver_email(to, subject, text)
    except Exception:
        # The no-Redis development fallback has no worker to own retries.
        log.exception("could not send %r", subject)


def _dispatch(tasks: BackgroundTasks, to: str, subject: str, text: str) -> None:
    if not settings.mail_enabled:
        log.info("mail disabled; skipped %r", subject)
        return
    if settings.redis_url:
        try:
            job_id = enqueue_email(to, subject, text)
            log.info("queued %r as %s", subject, job_id)
            return
        except Exception:
            # Redis should not turn account creation into an outage. A direct
            # background send preserves delivery while the queue is degraded.
            log.exception("could not queue %r; falling back to direct delivery", subject)
    tasks.add_task(_deliver_safely, to, subject, text)


def send_verify_email(tasks: BackgroundTasks, *, to: str, name: str, token: str) -> None:
    """Proves the address. Gates nothing, on purpose.

    The account already works, so this is not a hurdle in the way of using the
    product - it is the thing that lets Gimik send this person mail later
    without guessing, and lets someone invite them by address knowing it lands.
    Written to be ignorable today and useful when they get round to it.
    """
    first = (name or "there").split(" ")[0]
    link = f"{settings.app_scheme}://verify?token={token}"
    _dispatch(
        tasks,
        to,
        "Confirm your email for Gimik",
        f"""{first},

Confirm this is your address:

{link}

Or paste this code into Settings:

{token}

Nothing breaks if you ignore it - your list already works. Confirming just
means we can reach you, and that someone inviting you by email finds you.

- Gimik
""",
    )


def send_welcome(tasks: BackgroundTasks, *, to: str, name: str) -> None:
    """The one email a new account gets besides the confirmation."""
    first = (name or "there").split(" ")[0]
    _dispatch(
        tasks,
        to,
        "Your Gimik list is open",
        f"""{first},

Your list is open.

Keep the things you keep meaning to do. The people who want them too will find
you - that is the whole product, and it starts working the moment your list has
a few things on it.

Three is usually enough to start.

- Gimik
{settings.app_base_url}
""",
    )


def send_password_changed(tasks: BackgroundTasks, *, to: str, name: str) -> None:
    """Sent after a password change, because the useful one is the warning you
    did not expect."""
    first = (name or "there").split(" ")[0]
    _dispatch(
        tasks,
        to,
        "Your Gimik password changed",
        f"""{first},

Your Gimik password was just changed.

If that was you, there is nothing to do. If it was not, reply to this message
straight away.

- Gimik
""",
    )


def send_password_reset(tasks: BackgroundTasks, *, to: str, name: str, token: str) -> None:
    """The link opens the app straight onto the reset screen.

    The token is in the URL and also printed plainly underneath, because a deep
    link that does not fire - a desktop inbox, a stripped scheme - should leave
    the person with something they can still use rather than a dead end.
    """
    first = (name or "there").split(" ")[0]
    link = f"{settings.app_scheme}://reset?token={token}"
    _dispatch(
        tasks,
        to,
        "Reset your Gimik password",
        f"""{first},

Open this on the phone with Gimik installed:

{link}

Or paste this code into the app's reset screen:

{token}

The link works for {settings.jwt_reset_ttl_minutes} minutes. If you did not ask
for it, ignore this - nothing has changed.

- Gimik
""",
    )
