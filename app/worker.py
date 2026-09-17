"""RQ worker entrypoint for queued and scheduled transactional email."""

from redis import Redis
from rq import Queue, Worker
from rq.serializers import JSONSerializer

from app.config import get_settings


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
    return Worker(
        [queue],
        connection=connection,
        serializer=JSONSerializer,
    )


def main() -> None:
    """Process queued email and promote scheduled jobs when they become due."""
    create_worker().work(with_scheduler=True)


if __name__ == "__main__":
    main()
