from types import SimpleNamespace

from app import worker


def test_create_worker_uses_configured_email_queue(monkeypatch):
    events: list[tuple] = []
    connection = object()
    queue = object()

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://example", email_queue_name="mail"),
    )
    monkeypatch.setattr(
        worker.Redis,
        "from_url",
        lambda url, **kwargs: events.append(("redis", url, kwargs)) or connection,
    )
    monkeypatch.setattr(
        worker,
        "Queue",
        lambda name, **kwargs: events.append(("queue", name, kwargs)) or queue,
    )
    monkeypatch.setattr(
        worker,
        "Worker",
        lambda queues, **kwargs: events.append(("worker", queues, kwargs)) or "worker",
    )

    assert worker.create_worker() == "worker"
    assert events[0][0:2] == ("redis", "redis://example")
    assert events[1][0:2] == ("queue", "mail")
    assert events[1][2]["connection"] is connection
    assert events[2][1] == [queue]


def test_worker_enables_scheduler(monkeypatch):
    events: list[dict] = []
    fake_worker = SimpleNamespace(work=lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(worker, "create_worker", lambda: fake_worker)

    worker.main()

    assert events == [{"with_scheduler": True}]
