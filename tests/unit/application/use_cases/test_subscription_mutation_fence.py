from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from src.application.dto import RemnaSubscriptionDto
from src.application.use_cases.remnawave.commands.synchronization import (
    SyncRemnaUser,
    SyncRemnaUserDto,
)
from src.infrastructure.services.remnawave import RemnawaveImpl


class _UnitOfWork:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> "_UnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _MutationLock:
    def __init__(self) -> None:
        self.user_ids: list[int] = []

    def hold(self, user_id: int) -> "_MutationLock":
        self.user_ids.append(user_id)
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_panel_sync_always_uses_fenced_snapshot_with_equal_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_id = UUID("00000000-0000-0000-0000-000000000042")
    old_time = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
    stale = SimpleNamespace(
        uuid=remote_id,
        telegram_id=42,
        updated_at=old_time,
        expire_at=old_time + timedelta(days=10),
    )
    latest = SimpleNamespace(
        uuid=remote_id,
        telegram_id=42,
        updated_at=old_time,
        expire_at=old_time + timedelta(days=30),
    )
    local_subscription = SimpleNamespace(
        expire_at=stale.expire_at,
        changed_data={},
    )
    user = SimpleNamespace(id=7, log="user", remna_name="user")

    monkeypatch.setattr(
        RemnaSubscriptionDto,
        "from_remna_user",
        classmethod(lambda cls, value: SimpleNamespace(expire_at=value.expire_at)),
    )

    def apply_sync(target: SimpleNamespace, source: SimpleNamespace) -> SimpleNamespace:
        target.expire_at = source.expire_at
        target.changed_data = {"expire_at": source.expire_at}
        return target

    remnawave = SimpleNamespace(
        get_user_by_uuid=AsyncMock(return_value=latest),
        apply_sync=Mock(side_effect=apply_sync),
    )
    subscription_dao = SimpleNamespace(
        get_current=AsyncMock(return_value=local_subscription),
        update=AsyncMock(),
    )
    lock = _MutationLock()
    use_case = SyncRemnaUser(
        _UnitOfWork(),  # type: ignore[arg-type]
        SimpleNamespace(
            get_by_remna_uuid=AsyncMock(return_value=user),
            get_by_telegram_id=AsyncMock(),
        ),
        subscription_dao,
        SimpleNamespace(default_locale="ru"),
        remnawave,
        SimpleNamespace(),
        lock,  # type: ignore[arg-type]
    )

    changed = await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="system"),
        SyncRemnaUserDto(remna_user=stale, creating=False),
    )

    assert changed is True
    assert lock.user_ids == [7]
    assert local_subscription.expire_at == latest.expire_at
    remnawave.apply_sync.assert_called_once()
    assert remnawave.apply_sync.call_args.args[1].expire_at == latest.expire_at


@pytest.mark.asyncio
async def test_remnawave_full_update_has_lower_level_mutation_fence() -> None:
    lock = _MutationLock()
    response = SimpleNamespace(username="user", uuid=UUID(int=42), telegram_id=42)
    sdk = SimpleNamespace(users=SimpleNamespace(update_user=AsyncMock(return_value=response)))
    remnawave = RemnawaveImpl(sdk, lock)  # type: ignore[arg-type]
    remnawave._build_update_request = Mock(return_value=SimpleNamespace(username="user"))  # type: ignore[method-assign]

    result = await remnawave.update_user(  # type: ignore[arg-type]
        user=SimpleNamespace(id=7),
        uuid=UUID(int=42),
        subscription=SimpleNamespace(),
    )

    assert result is response
    assert lock.user_ids == [7]
