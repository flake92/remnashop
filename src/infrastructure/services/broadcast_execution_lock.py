import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.application.common import BroadcastExecutionLock

_BROADCAST_LOCK_DOMAIN = b"remnashop:broadcast-execution:v1\0"
_LOCK_RETRY_SECONDS = 0.5


def broadcast_advisory_lock_key(task_id: UUID) -> int:
    """Map a broadcast UUID to PostgreSQL's signed 64-bit advisory key."""
    digest = hashlib.sha256(_BROADCAST_LOCK_DOMAIN + task_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class PostgresBroadcastExecutionLock(BroadcastExecutionLock):
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @staticmethod
    async def _release(connection: AsyncConnection, lock_key: int) -> None:
        released = await connection.scalar(
            text("SELECT pg_advisory_unlock(CAST(:lock_key AS BIGINT))"),
            {"lock_key": lock_key},
        )
        await connection.commit()
        if released is not True:
            raise RuntimeError("PostgreSQL broadcast advisory lock was not owned")

    @asynccontextmanager
    async def hold(self, task_id: UUID) -> AsyncIterator[None]:
        lock_key = broadcast_advisory_lock_key(task_id)
        connection = await self.engine.connect()
        safe_to_return_to_pool = False
        try:
            # The engine has both server statement_timeout=60s and asyncpg
            # command_timeout=30s.  A single blocking pg_advisory_lock call
            # would therefore fail during an ordinary long broadcast. Poll the
            # session-level try-lock instead, but never return a successful
            # "busy" result to Taskiq: the reclaimed entry waits until it owns
            # the lock and can re-read persisted state.
            while True:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(CAST(:lock_key AS BIGINT))"),
                    {"lock_key": lock_key},
                )
                # End every short acquisition transaction before sleeping and
                # before Telegram IO. Session-level locks survive COMMIT.
                await connection.commit()
                if acquired is True:
                    break
                if acquired is not False:
                    raise RuntimeError("PostgreSQL returned an invalid advisory lock result")
                await asyncio.sleep(_LOCK_RETRY_SECONDS)

            try:
                yield
            finally:
                release_task = asyncio.create_task(self._release(connection, lock_key))
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:
                    # Do not return a still-locked physical connection to the pool.
                    await release_task
                    raise
                safe_to_return_to_pool = True
        finally:
            try:
                if not safe_to_return_to_pool:
                    # Covers cancellation during lock acquisition/commit and any
                    # unlock failure. Closing the physical session releases the lock.
                    await connection.invalidate()
            finally:
                await connection.close()
