from __future__ import annotations

from fastapi import BackgroundTasks

from app.services import email


class FakePublishResult:
    message_id = "msg-123"


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish_json(self, **kwargs):
        self.calls.append(kwargs)
        return FakePublishResult()


def test_enqueue_email_uses_signed_callback_and_redacts_body(monkeypatch):
    from types import SimpleNamespace

    messages = FakeMessages()
    monkeypatch.setattr(email, "_qstash", lambda: SimpleNamespace(message=messages))
    monkeypatch.setattr(
        email.settings, "qstash_email_callback_url", "https://api.gimik.app/internal/qstash/email"
    )

    job_id = email.enqueue_email("person@example.com", "Subject", "Body", delay_seconds=90)

    assert job_id == "msg-123"
    options = messages.calls[0]
    assert options["url"] == "https://api.gimik.app/internal/qstash/email"
    assert options["body"] == {"to": "person@example.com", "subject": "Subject", "text": "Body"}
    assert options["delay"] == "90s"
    assert options["retries"] == 3
    assert options["redact"] == {"body": True}


def test_dispatch_falls_back_when_qstash_is_disabled(monkeypatch):
    tasks = BackgroundTasks()
    monkeypatch.setattr(email.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(email.settings, "qstash_token", "")

    email._dispatch(tasks, "person@example.com", "Subject", "Body")

    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].func is email._deliver_safely


def test_zeptomail_delivery_uses_starttls_and_login(monkeypatch):
    events: list[tuple] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send", message["From"], message["To"], message["Subject"]))

    monkeypatch.setattr(email.settings, "smtp_host", "smtp.zeptomail.com")
    monkeypatch.setattr(email.settings, "smtp_port", 587)
    monkeypatch.setattr(email.settings, "smtp_use_tls", True)
    monkeypatch.setattr(email.settings, "smtp_username", "emailapikey")
    monkeypatch.setattr(email.settings, "smtp_password", "token")
    monkeypatch.setattr(email.settings, "mail_from", "Gimik <noreply@gimik.app>")
    monkeypatch.setattr(email.smtplib, "SMTP", FakeSmtp)

    email.deliver_email("person@example.com", "Subject", "Body")

    assert events == [
        ("connect", "smtp.zeptomail.com", 587, 10),
        ("starttls",),
        ("login", "emailapikey", "token"),
        ("send", "Gimik <noreply@gimik.app>", "person@example.com", "Subject"),
    ]
