from types import SimpleNamespace

from rq.defaults import DEFAULT_WORKER_TTL

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
        "EmailWorker",
        lambda queues, **kwargs: events.append(("worker", queues, kwargs)) or "worker",
    )

    assert worker.create_worker() == "worker"
    assert events[0][0:2] == ("redis", "redis://example")
    assert events[1][0:2] == ("queue", "mail")
    assert events[1][2]["connection"] is connection
    assert events[2][1] == [queue]
    # Below the blocking dequeue timeout, or due jobs wait for a second wakeup.
    assert events[2][2]["maintenance_interval"] < DEFAULT_WORKER_TTL - 15


def test_worker_runs_without_a_scheduler_process(monkeypatch):
    """The 1Hz poller is the thing this module exists to avoid starting."""
    events: list[dict] = []
    fake_worker = SimpleNamespace(work=lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(worker, "create_worker", lambda: fake_worker)

    worker.main()

    assert events == [{}]


def test_promotes_due_jobs_on_every_wakeup_but_builds_registries_once(monkeypatch):
    prepared: list[list[str]] = []
    promoted: list[bool] = []

    class FakeScheduler:
        def __init__(self, queues, **kwargs):
            self.queues = queues

        def prepare_registries(self, names):
            prepared.append(names)

        def enqueue_scheduled_jobs(self):
            promoted.append(True)

    monkeypatch.setattr(worker, "RQScheduler", FakeScheduler)

    instance = worker.EmailWorker.__new__(worker.EmailWorker)
    instance.queues = [SimpleNamespace(name="mail")]
    instance.connection = object()
    instance.serializer = object()

    instance._promote_scheduled_jobs()
    instance._promote_scheduled_jobs()

    assert prepared == [["mail"]]
    assert promoted == [True, True]


def test_maintenance_promotes_after_running_rq_s_own_cleanup(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        worker.Worker, "run_maintenance_tasks", lambda self: calls.append("cleanup")
    )

    instance = worker.EmailWorker.__new__(worker.EmailWorker)
    monkeypatch.setattr(instance, "_promote_scheduled_jobs", lambda: calls.append("promote"))

    instance.run_maintenance_tasks()

    assert calls == ["cleanup", "promote"]
