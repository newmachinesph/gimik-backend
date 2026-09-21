"""RQ worker entrypoint for queued and scheduled transactional email.

RQ's own scheduler is a second process on a hardcoded one-second loop. Seven
Redis commands a tick - a heartbeat pipeline, a lock refresh and one
ZRANGEBYSCORE - is roughly 605K Upstash commands a day spent watching a sorted
set that is empty except for the minutes after a send fails. It polls at that
rate whether the queue holds a thousand jobs or none, which is the whole
problem: the cost tracks the clock rather than the work.

Nothing here needs a second's precision. The only thing this queue schedules is
an SMTP retry at 60, 300 and 900 seconds, so promoting due jobs on the worker's
own wakeup is enough. The worker already surfaces from its blocking dequeue
every `worker_ttl - 15` seconds, and one ZRANGEBYSCORE on the way past costs
nothing - an idle worker settles at about 1.7K commands a day.

The trade is latency, not correctness. A retry fires up to one blocking timeout
late, and a delivery that was already 60 seconds behind does not care.
"""

from redis import Redis
from rq import Queue, Worker
from rq.scheduler import RQScheduler
from rq.serializers import JSONSerializer

from app.config import get_settings

# Kept below the blocking dequeue timeout so maintenance - and with it the
# promotion of due jobs - runs on every wakeup rather than every other one.
MAINTENANCE_INTERVAL_SECONDS = 60


class EmailWorker(Worker):
    """A worker that promotes its own scheduled jobs instead of forking a poller.

    ponytail: correct because the service runs a single instance. Two workers
    would race to promote the same job, which is what RQScheduler's lock exists
    to prevent - if this ever scales out, go back to `work(with_scheduler=True)`
    and pay for the lock.
    """

    _promoter: RQScheduler | None = None

    def run_maintenance_tasks(self) -> None:
        """RQ's periodic cleanup, plus the scheduled work it would otherwise miss.

        This is the only hook RQ calls once per blocking wakeup that is not the
        heartbeat, and it already runs on the first pass - so a restart drains
        anything that came due while the worker was down.
        """
        super().run_maintenance_tasks()
        self._promote_scheduled_jobs()

    def _promote_scheduled_jobs(self) -> None:
        """Move every retry whose delay has elapsed onto the work queue."""
        if self._promoter is None:
            self._promoter = RQScheduler(
                self.queues,
                connection=self.connection,
                serializer=self.serializer,
            )
            # Deliberately not acquire_locks(): the lock guards against a second
            # scheduler, and with one instance there is nobody to race.
            self._promoter.prepare_registries([queue.name for queue in self.queues])
        self._promoter.enqueue_scheduled_jobs()


def create_worker() -> Worker:
    """Build the email worker from environment-backed application settings."""
    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required to run the email worker")

    connection = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    queue = Queue(
        settings.email_queue_name,
        connection=connection,
        serializer=JSONSerializer,
    )
    return EmailWorker(
        [queue],
        connection=connection,
        serializer=JSONSerializer,
        maintenance_interval=MAINTENANCE_INTERVAL_SECONDS,
    )


def main() -> None:
    """Process queued email, promoting scheduled jobs as each wakeup comes round."""
    create_worker().work()


if __name__ == "__main__":
    main()
