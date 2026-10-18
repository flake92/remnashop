"""Container health checks: ``python -m src.infrastructure.healthcheck web|taskiq``.

Docker runs this as a fresh process on every interval, so only the standard
library is imported at module level. The ``web`` check is one HTTP request. The
``taskiq`` check imports only the Redis client: the application config and the
taskiq package pull in the whole application (about 5 s of CPU on a 1-vCPU
host), which made every probe time out during startup on small servers. The
key, queue and Redis settings are therefore derived here from the environment
exactly as ``src.infrastructure.taskiq.health_state`` and ``RedisConfig`` do;
tests keep the two in lockstep.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Mirrors of src.infrastructure.taskiq.health_state (checked by tests).
TASKIQ_HEARTBEAT_MAX_AGE_SECONDS = 150
TASKIQ_QUEUE_PREFIX = "taskiq"
LEGACY_TASKIQ_QUEUE_NAME = TASKIQ_QUEUE_PREFIX
TASKIQ_CONSUMER_GROUP_NAME = "taskiq"
TASKIQ_HEARTBEAT_KEY_PREFIX = "taskiq_pipeline_heartbeat"
DEFAULT_TASKIQ_STREAM_MAX_ENTRIES = 100_000


def taskiq_deployment_id() -> str:
    configured = os.environ.get("TASKIQ_DEPLOYMENT_ID", "local").strip()
    if not configured or configured.casefold() in {"change_me", "replace_me"}:
        raise ValueError("TASKIQ_DEPLOYMENT_ID must be a non-placeholder value")
    return hashlib.sha256(configured.encode("utf-8")).hexdigest()[:24]


def taskiq_heartbeat_key() -> str:
    return f"{TASKIQ_HEARTBEAT_KEY_PREFIX}:{taskiq_deployment_id()}"


def taskiq_queue_name() -> str:
    return f"{TASKIQ_QUEUE_PREFIX}:{taskiq_deployment_id()}"


def taskiq_stream_max_entries() -> int:
    value = int(os.environ.get("TASKIQ_STREAM_MAX_ENTRIES", str(DEFAULT_TASKIQ_STREAM_MAX_ENTRIES)))
    if value < 1:
        raise ValueError("TASKIQ_STREAM_MAX_ENTRIES must be greater than zero")
    return value


def redis_connection_settings() -> dict[str, object]:
    """The REDIS_* settings RedisConfig reads, with the same defaults."""
    password = os.environ.get("REDIS_PASSWORD") or None
    return {
        "host": os.environ.get("REDIS_HOST", "remnashop-redis"),
        "port": int(os.environ.get("REDIS_PORT", "6379")),
        "db": int(os.environ.get("REDIS_NAME", "0")),
        "password": password,
    }


def redis_client() -> Redis:
    from redis.asyncio import Redis  # noqa: PLC0415

    return Redis(
        **redis_connection_settings(),  # type: ignore[arg-type]
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


async def legacy_taskiq_backlog(redis: Redis) -> int:
    """Count only pending/unread legacy work, not old acknowledged history."""
    from redis.exceptions import ResponseError  # noqa: PLC0415

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
    from redis.exceptions import RedisError  # noqa: PLC0415

    try:
        redis = redis_client()
    except ValueError as error:
        sys.stderr.write(f"Taskiq health check failed: {type(error).__name__}\n")
        return False
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
