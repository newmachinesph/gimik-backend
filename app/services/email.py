"""Transactional mail delivered through QStash to this API's signed callback.

QStash holds pending messages and retries temporary SMTP failures. Local
development without QStash uses a FastAPI background task for Mailpit.

The copy lives here rather than in a template engine because there are two
messages. When there is a third, this becomes a directory of templates.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from functools import lru_cache

from fastapi import BackgroundTasks
from qstash import QStash

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("gimik.email")

TIMEOUT_SECONDS = 10
RETRY_DELAY = "min(900000, pow(5, retried) * 60000)"


@lru_cache
def _qstash() -> QStash:
    return QStash(settings.qstash_token, base_url=settings.qstash_url)


def deliver_email(to: str, subject: str, text: str) -> None:
    """SMTP errors escape so QStash can retry the signed callback."""
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
    """Persist a redacted QStash message and return its ID."""
    result = _qstash().message.publish_json(
        url=settings.qstash_email_callback_url,
        body={"to": to, "subject": subject, "text": text},
        retries=3,
        retry_delay=RETRY_DELAY,
        delay=f"{delay_seconds}s" if delay_seconds > 0 else None,
        timeout="30s",
        redact={"body": True},
    )
    return result.message_id


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
    if settings.qstash_enabled:
        try:
            job_id = enqueue_email(to, subject, text)
            log.info("queued %r as %s", subject, job_id)
            return
        except Exception:
            # A queue outage should not turn account creation into an outage.
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


def send_moderation_alert(tasks: BackgroundTasks, *, subject: str, text: str) -> None:
    """A report or a block, sent to the people who have 24 hours to act on it.

    Logged as well as mailed: with mail switched off the report is still a row
    in `reports` and a line in the log, never nothing.
    """
    subject = " ".join(subject.split())  # a header cannot hold a line break
    log.warning("moderation: %s", subject)
    _dispatch(tasks, settings.moderation_email, f"[Gimik moderation] {subject}", text)


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
