from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from src.application.common.dao.user_merge import UserMergePaymentOperationConflictError
from src.infrastructure.database.dao.user_merge import UserMergeDaoImpl


def _user(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        email=None,
        telegram_id=None,
        is_email_verified=False,
        current_subscription_id=None,
    )


@pytest.mark.asyncio
async def test_plan_reports_payment_operation_identity_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dao = UserMergeDaoImpl(SimpleNamespace())  # type: ignore[arg-type]
    moved = {
        "payment_operations": 2,
        "payment_operation_duplicates": 1,
    }
    monkeypatch.setattr(dao, "_lock_users", AsyncMock(return_value=(_user(11), _user(22))))
    monkeypatch.setattr(dao, "_collect_moved_counts", AsyncMock(return_value=moved))

    plan = await dao.plan(11, 22)

    assert plan.moved == moved
    assert plan.conflicts == [
        "Payment idempotency key collision between source and target (1)"
    ]


class UpdateSession:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.statements: list[object] = []

    async def execute(self, statement: object) -> SimpleNamespace:
        self.statements.append(statement)
        return SimpleNamespace(rowcount=self.rowcount)


class MergeSession:
    def __init__(self, source: SimpleNamespace) -> None:
        self.source = source
        self.merged_owner_at_flush: list[int | None] = []

    async def flush(self) -> None:
        self.merged_owner_at_flush.append(self.source.merged_into_user_id)


@pytest.mark.asyncio
async def test_payment_operations_transfer_in_one_atomic_update() -> None:
    session = UpdateSession(rowcount=3)
    dao = UserMergeDaoImpl(session)  # type: ignore[arg-type]
    moved = {"payment_operations": 99}

    await dao._move_payment_operations(11, 22, moved)

    assert moved["payment_operations"] == 3
    assert len(session.statements) == 1
    sql = str(
        session.statements[0].compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "UPDATE PAYMENT_OPERATIONS SET USER_ID=22" in sql
    assert "PAYMENT_OPERATIONS.USER_ID = 11" in sql


@pytest.mark.asyncio
async def test_merge_moves_payment_operations_before_marking_source_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=11,
        email="source@example.com",
        pending_email="pending@example.com",
        email_verification_code_hash="verification",
        email_verification_expires_at=None,
        password_reset_code_hash="reset",
        password_reset_expires_at=None,
        password_hash="password",
        is_email_verified=True,
        telegram_id=111,
        current_subscription_id=101,
        token_version=0,
        is_blocked=False,
        merged_into_user_id=None,
        merged_at=None,
    )
    target = SimpleNamespace(
        id=22,
        email=None,
        pending_email=None,
        email_verification_code_hash=None,
        email_verification_expires_at=None,
        password_reset_code_hash=None,
        password_reset_expires_at=None,
        password_hash=None,
        is_email_verified=False,
        telegram_id=None,
        current_subscription_id=None,
        token_version=0,
    )
    session = MergeSession(source)
    dao = UserMergeDaoImpl(session)  # type: ignore[arg-type]
    transfer_states: list[int | None] = []

    async def move_payment_operations(
        source_user_id: int,
        target_user_id: int,
        moved: dict[str, int],
    ) -> None:
        assert (source_user_id, target_user_id) == (11, 22)
        transfer_states.append(source.merged_into_user_id)
        moved["payment_operations"] = 2

    monkeypatch.setattr(dao, "_move_simple_fk", AsyncMock(return_value=0))
    monkeypatch.setattr(dao, "_move_payment_operations", move_payment_operations)
    monkeypatch.setattr(dao, "_move_referrals", AsyncMock())
    monkeypatch.setattr(dao, "_move_promocode_activations", AsyncMock())
    monkeypatch.setattr(dao, "_move_oauth_providers", AsyncMock())
    moved = {"payment_operations": 2}

    await dao._merge_records(source, target, moved)  # type: ignore[arg-type]

    assert transfer_states == [None]
    assert session.merged_owner_at_flush == [None, 22]
    assert source.merged_into_user_id == 22


@pytest.mark.asyncio
async def test_merge_recheck_fails_closed_on_payment_operation_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dao = UserMergeDaoImpl(SimpleNamespace())  # type: ignore[arg-type]
    count_duplicates = AsyncMock(return_value=2)
    monkeypatch.setattr(dao, "_count_payment_operation_duplicates", count_duplicates)

    with pytest.raises(
        UserMergePaymentOperationConflictError,
        match=r"Payment idempotency key collision.*\(2\)",
    ):
        await dao._assert_no_payment_operation_collisions(11, 22)

    count_duplicates.assert_awaited_once_with(11, 22)
