import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

from loguru import logger
from redis.asyncio import Redis
from taskiq import AsyncResultBackend, SmartRetryMiddleware
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from src.core.config import AppConfig
from src.infrastructure.taskiq.health_state import (
    LEGACY_TASKIQ_QUEUE_NAME,
    TASKIQ_CONSUMER_GROUP_NAME,
    taskiq_queue_name,
)
from src.infrastructure.taskiq.middlewares import ErrorMiddleware

TASKIQ_QUEUE_NAME = taskiq_queue_name()
# A running task refreshes its pending-entry idle time well before Taskiq Redis
# is allowed to recover work from a dead worker. This keeps long broadcasts,
# imports and backups from being executed concurrently after ten minutes.
TASKIQ_PENDING_IDLE_TIMEOUT_MS = 15 * 60 * 1000
TASKIQ_PENDING_LEASE_REFRESH_SECONDS = 60

REFRESH_PENDING_LEASE_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1)
if #pending == 0 then
  return 0
end
if pending[1][2] ~= ARGV[2] then
  return -1
end
local claimed = redis.call(
  'XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0, ARGV[3], 'JUSTID'
)
if #claimed == 0 then
  return 0
end
return 1
"""

ACK_AND_DELETE_OWNED_ENTRY_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1)
if #pending == 0 then
  return {0, 0}
end
if pending[1][2] ~= ARGV[2] then
  return {-1, 0}
end
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[3])
if acknowledged == 0 then
  return {0, 0}
end
local deleted = redis.call('XDEL', KEYS[1], ARGV[3])
return {acknowledged, deleted}
"""


class AckDeletingRedisStreamBroker(RedisStreamBroker):
    """Redis stream broker with renewable in-flight leases and bounded storage.

    taskiq-redis acknowledges a stream entry but leaves it in the stream forever.
    Trimming the stream on publish is unsafe because Redis may trim pending work.
    This broker instead deletes exactly one entry only after Taskiq acknowledges it.
    While a task is running, its pending-entry idle clock is refreshed; if the
    worker dies, refreshes stop and the normal XAUTOCLAIM recovery path takes over.
    """

    def __init__(
        self,
        *args: Any,
        pending_lease_refresh_seconds: int = TASKIQ_PENDING_LEASE_REFRESH_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.pending_lease_refresh_seconds = pending_lease_refresh_seconds
        self._pending_lease_tasks: set[asyncio.Task[None]] = set()

    async def _refresh_pending_lease(self, message_id: str, queue_name: str) -> None:
        while True:
            await asyncio.sleep(self.pending_lease_refresh_seconds)
            try:
                async with Redis(connection_pool=self.connection_pool) as redis_conn:
                    refreshed = int(
                        await cast(
                            Awaitable[Any],
                            redis_conn.eval(
                                REFRESH_PENDING_LEASE_SCRIPT,
                                1,
                                queue_name,
                                self.consumer_group_name,
                                self.consumer_name,
                                message_id,
                            ),
                        )
                    )
                if refreshed != 1:
                    logger.warning(
                        "Taskiq entry lease is no longer owned: queue={} message_id={}",
                        queue_name,
                        message_id,
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient Redis failure must not cancel the application task.
                # If refreshes remain unavailable past idle_timeout, another worker
                # may recover the entry, which is preferable to losing the work.
                logger.exception("Failed to refresh Taskiq pending-entry lease")

    def _ack_generator(
        self,
        id: str,
        queue_name: str,
    ) -> Callable[[], Awaitable[None]]:
        lease_task = asyncio.create_task(self._refresh_pending_lease(id, queue_name))
        self._pending_lease_tasks.add(lease_task)
        lease_task.add_done_callback(self._pending_lease_tasks.discard)

        async def _ack() -> None:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task

            async with Redis(connection_pool=self.connection_pool) as redis_conn:
                acknowledged, deleted = await cast(
                    Awaitable[Any],
                    redis_conn.eval(
                        ACK_AND_DELETE_OWNED_ENTRY_SCRIPT,
                        1,
                        queue_name,
                        self.consumer_group_name,
                        self.consumer_name,
                        id,
                    ),
                )

            if acknowledged == -1:
                logger.warning(
                    "Taskiq entry ownership changed before acknowledgement: queue={}",
                    queue_name,
                )
            elif not acknowledged:
                logger.warning("Taskiq entry was already acknowledged: queue={}", queue_name)
            elif not deleted:
                logger.warning("Acknowledged Taskiq entry was already absent: queue={}", queue_name)

        return _ack

    async def shutdown(self) -> None:
        lease_tasks = tuple(self._pending_lease_tasks)
        for task in lease_tasks:
            task.cancel()
        if lease_tasks:
            await asyncio.gather(*lease_tasks, return_exceptions=True)
        await super().shutdown()


def create_broker(config: AppConfig) -> RedisStreamBroker:
    result_backend: AsyncResultBackend[Any] = RedisAsyncResultBackend(
        redis_url=config.redis.dsn,
        keep_results=False,
        result_ex_time=3600,
    )

    broker = AckDeletingRedisStreamBroker(
        url=config.redis.dsn,
        queue_name=TASKIQ_QUEUE_NAME,
        consumer_group_name=TASKIQ_CONSUMER_GROUP_NAME,
        # Keep consuming the pre-deployment-scoping stream until it is empty.
        # Existing consumer-group pending entries are recovered through
        # XAUTOCLAIM; unread entries are delivered with ``>``. Starting newly
        # created groups at ``0`` is essential, otherwise Redis would skip
        # legacy entries that were published before this version started.
        consumer_id="0",
        additional_streams={LEGACY_TASKIQ_QUEUE_NAME: ">"},
        idle_timeout=TASKIQ_PENDING_IDLE_TIMEOUT_MS,
    ).with_result_backend(result_backend)

    return broker


broker = create_broker(config=AppConfig.get())

broker.with_middlewares(
    *(
        ErrorMiddleware(),
        SmartRetryMiddleware(
            default_retry_count=5,
            default_delay=15,
            use_jitter=True,
            use_delay_exponent=True,
            max_delay_exponent=120,
        ),
    )
)
