from __future__ import annotations

from fastapi import BackgroundTasks

from app.services import email


class FakeJob:
    id = "job-123"


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def enqueue(self, *args, **kwargs):
        self.calls.append(("now", args, kwargs))
        return FakeJob()

    def enqueue_in(self, *args, **kwargs):
        self.calls.append(("later", args, kwargs))
        return FakeJob()


def test_enqueue_email_uses_retries_and_scheduler(monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr(email, "_queue", lambda: queue)

    job_id = email.enqueue_email("person@example.com", "Subject", "Body", delay_seconds=90)

    assert job_id == "job-123"
    kind, args, options = queue.calls[0]
    assert kind == "later"
    assert args[0].total_seconds() == 90
    assert args[1] is email.deliver_email
    assert options["retry"].max == 3
    assert options["retry"].intervals == [60, 300, 900]


def test_dispatch_falls_back_when_redis_is_disabled(monkeypatch):
    tasks = BackgroundTasks()
    monkeypatch.setattr(email.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(email.settings, "redis_url", "")

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
