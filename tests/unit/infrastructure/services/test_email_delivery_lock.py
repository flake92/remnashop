import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from src.application.common import (
    EmailDeliveryRunBusyError,
    EmailDeliveryRunLockLostError,
)
from src.infrastructure.services.email_delivery_lock import RedisEmailDeliveryRunLock


@pytest.mark.asyncio
async def test_email_delivery_lock_is_non_blocking_and_owned_on_release() -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.owned = AsyncMock(return_value=True)
    lock.release = AsyncMock()
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)

    async with delivery_lock.hold():
        pass

    redis.lock.assert_called_once_with(
        RedisEmailDeliveryRunLock.KEY,
        timeout=RedisEmailDeliveryRunLock.LEASE_SECONDS,
        blocking_timeout=0,
    )
    lock.acquire.assert_awaited_once_with(blocking=False)
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_email_delivery_lock_skips_overlapping_run() -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=False)
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)

    with pytest.raises(EmailDeliveryRunBusyError):
        async with delivery_lock.hold():
            raise AssertionError("busy lock must not enter the protected body")


@pytest.mark.asyncio
async def test_email_delivery_lock_never_reports_success_after_lease_loss() -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.owned = AsyncMock(return_value=False)
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)

    with pytest.raises(EmailDeliveryRunLockLostError):
        async with delivery_lock.hold():
            pass

    lock.release.assert_not_called()


@pytest.mark.asyncio
async def test_email_delivery_lock_cleanup_does_not_mask_body_failure() -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.owned = AsyncMock(side_effect=RedisError("redis unavailable"))
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)

    with pytest.raises(RuntimeError, match="delivery failed"):
        async with delivery_lock.hold():
            raise RuntimeError("delivery failed")


@pytest.mark.asyncio
async def test_email_delivery_lock_cleanup_failure_is_visible_after_success() -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.owned = AsyncMock(side_effect=RedisError("redis unavailable"))
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)

    with pytest.raises(EmailDeliveryRunLockLostError):
        async with delivery_lock.hold():
            pass


@pytest.mark.asyncio
async def test_rate_slots_use_redis_time_and_survive_run_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=[0, 750, 0, 750])
    delivery_lock = RedisEmailDeliveryRunLock(redis)
    sleep = AsyncMock()
    monkeypatch.setattr(
        "src.infrastructure.services.email_delivery_lock.asyncio.sleep",
        sleep,
    )

    assert (
        await delivery_lock.wait_for_send_slot(
            rate_per_minute=60,
            max_wait_seconds=2,
        )
        is True
    )
    assert (
        await delivery_lock.wait_for_send_slot(
            rate_per_minute=60,
            max_wait_seconds=2,
        )
        is True
    )
    assert (
        await delivery_lock.wait_for_send_slot(
            rate_per_minute=60,
            max_wait_seconds=0.5,
        )
        is False
    )

    first = redis.eval.await_args_list[0]
    assert "redis.call('TIME')" in first.args[0]
    assert "PSETEX" in first.args[0]
    assert first.args[1:] == (
        1,
        RedisEmailDeliveryRunLock.RATE_KEY,
        1000,
    )
    sleep.assert_awaited_once_with(0.75)
    # Waiting never books capacity in advance: the contender has to return to
    # Redis and win the due slot after its sleep.
    assert redis.eval.await_count == 4


@pytest.mark.asyncio
async def test_long_running_delivery_renews_run_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.owned = AsyncMock(return_value=True)
    renewed = asyncio.Event()

    async def extend(*args: object, **kwargs: object) -> bool:
        assert args == (RedisEmailDeliveryRunLock.LEASE_SECONDS,)
        assert kwargs == {"replace_ttl": True}
        renewed.set()
        return True

    lock.extend = AsyncMock(side_effect=extend)
    lock.release = AsyncMock()
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)
    monkeypatch.setattr(delivery_lock, "RENEW_INTERVAL_SECONDS", 0)

    async with delivery_lock.hold():
        await renewed.wait()

    assert lock.extend.await_count >= 1
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_lease_renewal_failure_interrupts_run_and_stays_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.owned = AsyncMock(return_value=True)
    lock.extend = AsyncMock(side_effect=RedisError("redis unavailable"))
    lock.release = AsyncMock()
    redis.lock.return_value = lock
    delivery_lock = RedisEmailDeliveryRunLock(redis)
    monkeypatch.setattr(delivery_lock, "RENEW_INTERVAL_SECONDS", 0)

    with pytest.raises(EmailDeliveryRunLockLostError, match="renewal failed"):
        async with delivery_lock.hold():
            await asyncio.Event().wait()
