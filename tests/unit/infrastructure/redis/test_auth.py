from unittest.mock import AsyncMock, MagicMock

import pytest

from src.infrastructure.redis.auth import (
    CONSUME_REFRESH_TOKEN_SCRIPT,
    INCREMENT_WITH_TTL_SCRIPT,
    RELEASE_LOCK_SCRIPT,
    REVOKE_ALL_REFRESH_TOKENS_SCRIPT,
    STORE_REFRESH_TOKEN_SCRIPT,
    RedisAuthRepository,
    _refresh_token_digest,
)


@pytest.mark.asyncio
async def test_password_reset_request_reservation_is_atomic_and_expiring() -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    repository = RedisAuthRepository(redis)

    reserved = await repository.reserve_password_reset_request("identity-hash", 60)

    assert reserved is True
    redis.set.assert_awaited_once_with("password_reset_request:identity-hash", "1", ex=60, nx=True)


@pytest.mark.asyncio
async def test_password_reset_attempt_increment_uses_atomic_ttl_script() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=3)
    repository = RedisAuthRepository(redis)

    attempts = await repository.increment_password_reset_attempts("identity-hash", 900)

    assert attempts == 3
    redis.eval.assert_awaited_once_with(
        INCREMENT_WITH_TTL_SCRIPT, 1, "password_reset_attempts:identity-hash", 900
    )


@pytest.mark.asyncio
async def test_password_reset_lock_is_owned_and_expiring() -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    repository = RedisAuthRepository(redis)

    acquired = await repository.acquire_password_reset_lock("identity-hash", "token", 15)

    assert acquired is True
    redis.set.assert_awaited_once_with("password_reset_lock:identity-hash", "token", ex=15, nx=True)


@pytest.mark.asyncio
async def test_password_reset_lock_release_only_deletes_owned_lock() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    repository = RedisAuthRepository(redis)

    await repository.release_password_reset_lock("identity-hash", "token")

    redis.eval.assert_awaited_once_with(
        RELEASE_LOCK_SCRIPT, 1, "password_reset_lock:identity-hash", "token"
    )


@pytest.mark.asyncio
async def test_refresh_token_is_stored_as_digest_with_token_version() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    repository = RedisAuthRepository(redis)

    await repository.store_refresh_token("raw-secret", user_id=7, token_version=4, ttl=60)

    digest = _refresh_token_digest("raw-secret")
    redis.eval.assert_awaited_once_with(
        STORE_REFRESH_TOKEN_SCRIPT,
        2,
        f"refresh:{digest}",
        "user_tokens:7",
        60,
        "v1:7:4",
        digest,
    )
    assert "raw-secret" not in str(redis.mock_calls)


@pytest.mark.asyncio
async def test_refresh_token_is_consumed_once_and_returns_bound_version() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=b"v1:7:4")
    repository = RedisAuthRepository(redis)

    record = await repository.get_and_revoke_refresh_token("raw-secret")

    digest = _refresh_token_digest("raw-secret")
    assert record is not None
    assert (record.user_id, record.token_version) == (7, 4)
    redis.eval.assert_awaited_once_with(
        CONSUME_REFRESH_TOKEN_SCRIPT,
        1,
        f"refresh:{digest}",
        "user_tokens:",
        digest,
    )


@pytest.mark.asyncio
async def test_single_refresh_token_revoke_removes_value_and_index_atomically() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=b"v1:7:4")
    repository = RedisAuthRepository(redis)

    await repository.revoke_refresh_token("raw-secret")

    digest = _refresh_token_digest("raw-secret")
    redis.eval.assert_awaited_once_with(
        CONSUME_REFRESH_TOKEN_SCRIPT,
        1,
        f"refresh:{digest}",
        "user_tokens:",
        digest,
    )


@pytest.mark.asyncio
async def test_legacy_refresh_token_is_revoked_but_not_accepted() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=[None, b"7"])
    repository = RedisAuthRepository(redis)

    record = await repository.get_and_revoke_refresh_token("legacy-secret")

    assert record is None
    assert redis.eval.await_args_list[1].args == (
        CONSUME_REFRESH_TOKEN_SCRIPT,
        1,
        "refresh:legacy-secret",
        "user_tokens:",
        "legacy-secret",
    )


@pytest.mark.asyncio
async def test_revoke_all_refresh_tokens_is_one_atomic_script() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=2)
    repository = RedisAuthRepository(redis)

    await repository.revoke_all_user_tokens(7)

    redis.eval.assert_awaited_once_with(
        REVOKE_ALL_REFRESH_TOKENS_SCRIPT,
        1,
        "user_tokens:7",
        "refresh:",
    )
