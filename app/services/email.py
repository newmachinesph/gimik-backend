"""Transactional mail. Mailpit locally, a real relay later.

Sent on a worker thread and never awaited by the caller: smtplib is blocking,
and an account should not fail to be created because a mail server took four
seconds to answer. A send that fails is logged; the account still exists, and
nothing in the product depends on the mail having arrived.

The copy lives here rather than in a template engine because there are two
messages. When there is a third, this becomes a directory of templates.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from fastapi import BackgroundTasks

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("gimik.email")

TIMEOUT_SECONDS = 10


def _send(to: str, subject: str, text: str) -> None:
    if not settings.mail_enabled:
        log.info("mail disabled; would have sent %r to %s", subject, to)
        return
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_SECONDS) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except Exception:
        # Never re-raised: this runs after the response has been sent.
        log.exception("could not send %r to %s", subject, to)


def send_verify_email(tasks: BackgroundTasks, *, to: str, name: str, token: str) -> None:
    """Proves the address. Gates nothing, on purpose.

    The account already works, so this is not a hurdle in the way of using the
    product - it is the thing that lets Gimik send this person mail later
    without guessing, and lets someone invite them by address knowing it lands.
    Written to be ignorable today and useful when they get round to it.
    """
    first = (name or "there").split(" ")[0]
    link = f"{settings.app_scheme}://verify?token={token}"
    tasks.add_task(
        _send,
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
    tasks.add_task(
        _send,
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
    tasks.add_task(
        _send,
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
    tasks.add_task(
        _send,
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
