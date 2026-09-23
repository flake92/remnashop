import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Awaitable, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.application.common import (
    EmailDeliveryRunBusyError,
    EmailDeliveryRunLock,
    EmailDeliveryRunLockLostError,
)

RESERVE_SEND_SLOT_SCRIPT = """
local time_parts = redis.call('TIME')
local now_ms = (tonumber(time_parts[1]) * 1000)
    + math.floor(tonumber(time_parts[2]) / 1000)
local interval_ms = tonumber(ARGV[1])
local next_slot_ms = tonumber(redis.call('GET', KEYS[1]) or 0)
if next_slot_ms > now_ms then
  return next_slot_ms - now_ms
end
local following_slot_ms = now_ms + interval_ms
local ttl_ms = math.max(interval_ms * 2, 1)
redis.call('PSETEX', KEYS[1], ttl_ms, tostring(following_slot_ms))
return 0
"""


class RedisEmailDeliveryRunLock(EmailDeliveryRunLock):
    # One run stops claiming before the next minute tick. This longer lease also
    # covers the finite worst-case SMTP socket-operation time. A crashed worker
    # becomes recoverable without allowing scheduled runs to pile up.
    LEASE_SECONDS = 600
    RENEW_INTERVAL_SECONDS = 60
    KEY = "email:subscription-delivery-run:v1"
    # Deliberately global within the shared Redis, rather than scoped to a
    # worker/run, so every process using the SMTP account observes one pacing
    # timeline and successive scheduler invocations cannot burst at the edge.
    RATE_KEY = "email:subscription-delivery-rate:v1"

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def _renew_lease(
        self,
        *,
        lock: Any,
        owner_task: asyncio.Task[Any],
        failure: asyncio.Future[EmailDeliveryRunLockLostError],
    ) -> None:
        while True:
            await asyncio.sleep(self.RENEW_INTERVAL_SECONDS)
            try:
                if not await lock.owned() or not await lock.extend(
                    self.LEASE_SECONDS,
                    replace_ttl=True,
                ):
                    raise EmailDeliveryRunLockLostError(
                        "Reminder delivery lease could not be renewed"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lease_failure = (
                    exc
                    if isinstance(exc, EmailDeliveryRunLockLostError)
                    else EmailDeliveryRunLockLostError("Reminder delivery lease renewal failed")
                )
                if not failure.done():
                    failure.set_result(lease_failure)
                owner_task.cancel()
                return

    @staticmethod
    async def _release_lock(lock: Any, *, body_failed: bool) -> None:
        try:
            if await lock.owned():
                await lock.release()
            elif not body_failed:
                raise EmailDeliveryRunLockLostError(
                    "Reminder delivery lease expired before release"
                )
        except RedisError as exc:
            # Cleanup must never replace the actual delivery failure. On a
            # successful body, however, an unverifiable/lost lease remains a
            # first-class failure rather than a false success.
            if not body_failed:
                raise EmailDeliveryRunLockLostError("Reminder delivery lease was lost") from exc

    async def wait_for_send_slot(
        self,
        *,
        rate_per_minute: int,
        max_wait_seconds: float,
    ) -> bool:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        if max_wait_seconds < 0:
            return False

        # Ceiling keeps the actual start rate at or below the configured
        # provider contract even when 60 seconds is not evenly divisible.
        interval_ms = math.ceil(60_000 / rate_per_minute)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_seconds
        while True:
            # Do not reserve a future slot: a process paused after such a
            # reservation could wake after a later process and create a real
            # SMTP-start burst. Redis grants only a slot that is due now; all
            # contenders re-check after sleeping against the same server clock.
            wait_ms = int(
                await cast(
                    Awaitable[Any],
                    self.redis.eval(
                        RESERVE_SEND_SLOT_SCRIPT,
                        1,
                        self.RATE_KEY,
                        interval_ms,
                    ),
                )
            )
            if wait_ms <= 0:
                return True
            wait_seconds = wait_ms / 1000
            remaining_seconds = deadline - loop.time()
            if wait_seconds > remaining_seconds:
                return False
            await asyncio.sleep(wait_seconds)

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("Delivery lock requires an asyncio task")
        lock = self.redis.lock(
            self.KEY,
            timeout=self.LEASE_SECONDS,
            blocking_timeout=0,
        )
        acquired = await lock.acquire(blocking=False)
        if not acquired:
            raise EmailDeliveryRunBusyError("Reminder delivery is already running")
        lease_failure = asyncio.get_running_loop().create_future()
        renewal_task = asyncio.create_task(
            self._renew_lease(
                lock=lock,
                owner_task=owner_task,
                failure=lease_failure,
            ),
            name="subscription-email-delivery-lock-renewal",
        )
        body_failed = False
        try:
            yield
        except asyncio.CancelledError as exc:
            body_failed = True
            if lease_failure.done():
                raise lease_failure.result() from exc
            raise
        except BaseException:
            body_failed = True
            raise
        finally:
            renewal_task.cancel()
            # return_exceptions consumes the renewal task's intentional
            # cancellation without swallowing a cancellation of this owner
            # that happens concurrently during cleanup.
            await asyncio.gather(renewal_task, return_exceptions=True)
            await self._release_lock(lock, body_failed=body_failed)
            if not body_failed and lease_failure.done():
                raise lease_failure.result()
