import asyncio
import os
import sys
import time
import urllib.request

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from src.core.config import AppConfig
from src.infrastructure.taskiq.health_state import (
    LEGACY_TASKIQ_QUEUE_NAME,
    TASKIQ_CONSUMER_GROUP_NAME,
    TASKIQ_HEARTBEAT_MAX_AGE_SECONDS,
    taskiq_deployment_id,
    taskiq_heartbeat_key,
    taskiq_queue_name,
    taskiq_stream_max_entries,
)


async def legacy_taskiq_backlog(redis: Redis) -> int:
    """Count only pending/unread legacy work, not old acknowledged history."""
    try:
        groups = await redis.xinfo_groups(LEGACY_TASKIQ_QUEUE_NAME)
    except ResponseError as error:
        if "no such key" in str(error).casefold():
            return 0
        raise

    for group in groups:
        name = group.get("name")
        if isinstance(name, bytes):
            name = name.decode()
        if name == TASKIQ_CONSUMER_GROUP_NAME:
            pending = int(group.get("pending") or 0)
            lag = group.get("lag")
            # Redis may report lag=None after history was manually modified.
            # XLEN is a conservative fallback: it can over-count acknowledged
            # legacy history but never hides work during a migration.
            unread = (
                int(lag) if lag is not None else int(await redis.xlen(LEGACY_TASKIQ_QUEUE_NAME))
            )
            return pending + unread

    # No consumer group means every legacy entry is unread.
    return int(await redis.xlen(LEGACY_TASKIQ_QUEUE_NAME))


def heartbeat_is_fresh(value: str | bytes | None, *, now: float) -> bool:
    if value is None:
        return False
    try:
        recorded_at = float(value.decode() if isinstance(value, bytes) else value)
    except (TypeError, ValueError):
        return False
    age = now - recorded_at
    return 0 <= age <= TASKIQ_HEARTBEAT_MAX_AGE_SECONDS


def check_web() -> bool:
    port = int(os.environ.get("APP_PORT", "5000"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            healthy = int(response.status) == 200
            if not healthy:
                sys.stderr.write(f"Web health endpoint returned HTTP {response.status}\n")
            return healthy
    except (OSError, ValueError) as error:
        sys.stderr.write(f"Web health check failed: {type(error).__name__}\n")
        return False


async def check_taskiq() -> bool:
    redis = Redis.from_url(
        AppConfig.get().redis.dsn,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        deployment_id = taskiq_deployment_id()
        value = await redis.get(taskiq_heartbeat_key())
        if not heartbeat_is_fresh(value, now=time.time()):
            sys.stderr.write(
                f"Taskiq heartbeat is missing or stale for deployment {deployment_id}\n"
            )
            return False

        stream_entries = int(await redis.xlen(taskiq_queue_name()))
        legacy_backlog = await legacy_taskiq_backlog(redis)
        stream_limit = taskiq_stream_max_entries()
        if stream_entries + legacy_backlog > stream_limit:
            sys.stderr.write(
                "Taskiq stream backlog is "
                f"{stream_entries + legacy_backlog} "
                f"(current={stream_entries}, legacy={legacy_backlog}); "
                f"limit is {stream_limit}\n"
            )
            return False

        sys.stdout.write(
            f"Taskiq pipeline healthy: deployment={deployment_id} "
            f"stream_entries={stream_entries} legacy_backlog={legacy_backlog}\n"
        )
        return True
    except (OSError, RedisError, ValueError) as error:
        sys.stderr.write(f"Taskiq health check failed: {type(error).__name__}\n")
        return False
    finally:
        await redis.aclose()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"web", "taskiq"}:
        return 2
    healthy = check_web() if sys.argv[1] == "web" else asyncio.run(check_taskiq())
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
