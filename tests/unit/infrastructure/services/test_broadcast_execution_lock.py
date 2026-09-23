from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from src.infrastructure.services import broadcast_execution_lock as lock_module
from src.infrastructure.services.broadcast_execution_lock import (
    PostgresBroadcastExecutionLock,
    broadcast_advisory_lock_key,
)


def test_broadcast_advisory_key_is_stable_signed_int64() -> None:
    task_id = UUID("4d833f0a-e97a-4af6-9a40-7202f3f0bb17")

    first = broadcast_advisory_lock_key(task_id)
    second = broadcast_advisory_lock_key(task_id)

    assert first == second
    assert -(2**63) <= first < 2**63
    assert first != broadcast_advisory_lock_key(UUID(int=task_id.int + 1))


@pytest.mark.asyncio
async def test_broadcast_lock_waits_without_open_transaction_and_unlocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.scalar = AsyncMock(side_effect=[False, True, True])
    connection.commit = AsyncMock()
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    engine = MagicMock()
    engine.connect = AsyncMock(return_value=connection)
    lock = PostgresBroadcastExecutionLock(engine)
    task_id = UUID("4d833f0a-e97a-4af6-9a40-7202f3f0bb17")
    monkeypatch.setattr(lock_module, "_LOCK_RETRY_SECONDS", 0)

    async with lock.hold(task_id):
        assert connection.scalar.await_count == 2
        acquire_sql = str(connection.scalar.await_args_list[0].args[0])
        assert "pg_try_advisory_lock" in acquire_sql
        assert connection.commit.await_count == 2

    release_sql = str(connection.scalar.await_args.args[0])
    assert "pg_advisory_unlock" in release_sql
    assert connection.commit.await_count == 3
    connection.invalidate.assert_not_awaited()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_unlock_failure_invalidates_connection_before_pool_return() -> None:
    connection = MagicMock()
    connection.scalar = AsyncMock(side_effect=[True, RuntimeError("connection lost")])
    connection.commit = AsyncMock()
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    engine = MagicMock()
    engine.connect = AsyncMock(return_value=connection)
    lock = PostgresBroadcastExecutionLock(engine)

    with pytest.raises(RuntimeError, match="connection lost"):
        async with lock.hold(UUID(int=7)):
            pass

    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
