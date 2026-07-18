from types import TracebackType
from unittest.mock import AsyncMock

import pytest

from src.application.common.dao.user_merge import UserMergeTargetConflictError
from src.application.use_cases.user.commands.merge import (
    MergeUsers,
    MergeUsersConflictError,
    MergeUsersDto,
)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()


@pytest.mark.asyncio
async def test_redirecting_already_merged_source_is_exposed_as_http_conflict() -> None:
    uow = FakeUnitOfWork()
    dao = AsyncMock()
    dao.plan.side_effect = UserMergeTargetConflictError(
        "Source user '11' is already merged into user '33' and cannot be redirected to user '22'"
    )
    use_case = MergeUsers(uow, dao)

    with pytest.raises(MergeUsersConflictError, match="cannot be redirected") as exc_info:
        await use_case.system(
            MergeUsersDto(
                source_user_id=11,
                target_user_id=22,
                reason="recover account",
                dry_run=False,
            )
        )

    assert exc_info.value.status_code == 409
    uow.commit.assert_not_awaited()
    uow.rollback.assert_awaited_once()
    dao.merge.assert_not_awaited()
