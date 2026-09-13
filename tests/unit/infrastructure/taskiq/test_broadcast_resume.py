import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.application.dto import (
    BroadcastDto,
    BroadcastMessageDto,
    MessagePayloadDto,
    UserDto,
)
from src.core.enums import BroadcastAudience, BroadcastMessageStatus, BroadcastStatus
from src.infrastructure.taskiq.tasks import broadcast as broadcast_tasks
from src.infrastructure.taskiq.tasks.broadcast import (
    _pending_broadcast_users,
    _run_delete_broadcast_task,
    _run_send_broadcast_task,
)


class _SerializedBroadcastLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.entered = 0

    @asynccontextmanager
    async def hold(self, _task_id):
        async with self._lock:
            self.entered += 1
            yield


def _broadcast(*, status: BroadcastStatus, messages=None) -> BroadcastDto:
    return BroadcastDto(
        id=17,
        task_id=uuid4(),
        status=status,
        audience=BroadcastAudience.ALL,
        payload=MessagePayloadDto(i18n_key="test-broadcast"),
        messages=list(messages or []),
    )


def test_replayed_broadcast_sends_only_not_yet_attempted_recipients() -> None:
    users = [cast(UserDto, SimpleNamespace(id=user_id)) for user_id in (1, 2, 3)]
    messages = [
        cast(
            BroadcastMessageDto,
            SimpleNamespace(user_id=1, status=BroadcastMessageStatus.SENT),
        ),
        cast(
            BroadcastMessageDto,
            SimpleNamespace(user_id=2, status=BroadcastMessageStatus.FAILED),
        ),
        cast(
            BroadcastMessageDto,
            SimpleNamespace(user_id=3, status=BroadcastMessageStatus.PENDING),
        ),
    ]

    pending = _pending_broadcast_users(users, messages)

    assert [user.id for user in pending] == [3]


@pytest.mark.asyncio
async def test_concurrent_send_replay_waits_then_observes_completed_state() -> None:
    current = _broadcast(status=BroadcastStatus.PROCESSING)
    message = BroadcastMessageDto(
        id=31,
        user_id=1,
        user_telegram_id=1001,
        status=BroadcastMessageStatus.PENDING,
    )
    user = cast(
        UserDto,
        SimpleNamespace(id=1, telegram_id=1001, log="user-1"),
    )
    release_send = asyncio.Event()
    send_started = asyncio.Event()

    async def notify_user(*_args, **_kwargs):
        send_started.set()
        await release_send.wait()
        return SimpleNamespace(message_id=501)

    async def finish(data):
        current.status = data.status

    lock = _SerializedBroadcastLock()
    broadcast_dao = SimpleNamespace(get_by_task_id=AsyncMock(return_value=current))
    audience = SimpleNamespace(system=AsyncMock(return_value=[user]))
    initialize = SimpleNamespace(system=AsyncMock(return_value=[message]))
    update = SimpleNamespace(system=AsyncMock())
    finish_broadcast = SimpleNamespace(system=AsyncMock(side_effect=finish))
    notifier = SimpleNamespace(notify_user=AsyncMock(side_effect=notify_user))

    first = asyncio.create_task(
        _run_send_broadcast_task(
            current,
            None,
            broadcast_dao,
            audience,
            initialize,
            update,
            finish_broadcast,
            notifier,
            lock,
        )
    )
    await send_started.wait()
    second = asyncio.create_task(
        _run_send_broadcast_task(
            current,
            None,
            broadcast_dao,
            audience,
            initialize,
            update,
            finish_broadcast,
            notifier,
            lock,
        )
    )
    await asyncio.sleep(0)

    assert not second.done()
    notifier.notify_user.assert_awaited_once()

    release_send.set()
    await asyncio.gather(first, second)

    assert lock.entered == 2
    notifier.notify_user.assert_awaited_once()
    finish_broadcast.system.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_delete_replay_uses_fresh_deleted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = BroadcastMessageDto(
        id=41,
        user_id=2,
        user_telegram_id=1002,
        message_id=601,
        status=BroadcastMessageStatus.SENT,
    )
    current = _broadcast(status=BroadcastStatus.COMPLETED, messages=[message])
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def delete_message(**_kwargs):
        delete_started.set()
        await release_delete.wait()
        return True

    async def finish(data):
        current.status = data.status

    monkeypatch.setattr(broadcast_tasks, "BATCH_DELAY", 0)
    lock = _SerializedBroadcastLock()
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=delete_message))
    broadcast_dao = SimpleNamespace(get_by_task_id=AsyncMock(return_value=current))
    bulk_update = SimpleNamespace(system=AsyncMock())
    finish_broadcast = SimpleNamespace(system=AsyncMock(side_effect=finish))
    notifier = SimpleNamespace(notify_admins=AsyncMock())

    first = asyncio.create_task(
        _run_delete_broadcast_task(
            current,
            bot,
            broadcast_dao,
            bulk_update,
            finish_broadcast,
            notifier,
            lock,
        )
    )
    await delete_started.wait()
    second = asyncio.create_task(
        _run_delete_broadcast_task(
            current,
            bot,
            broadcast_dao,
            bulk_update,
            finish_broadcast,
            notifier,
            lock,
        )
    )
    await asyncio.sleep(0)

    assert not second.done()
    bot.delete_message.assert_awaited_once()

    release_delete.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == (1, 1, 0)
    assert second_result == (1, 1, 0)
    assert lock.entered == 2
    bot.delete_message.assert_awaited_once()
    finish_broadcast.system.assert_awaited_once()
    notifier.notify_admins.assert_awaited_once()
