from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from src.application.common.dao.auth import RefreshTokenRecord
from src.application.dto import UserDto
from src.application.use_cases.auth.commands.session import RefreshSession, RefreshSessionDto


def make_interactor(*, stored_version: int, current_version: int) -> tuple[RefreshSession, UserDto]:
    user = UserDto(id=7, name="User", token_version=current_version)
    user_dao = MagicMock()
    user_dao.get_by_id = AsyncMock(return_value=user)
    auth_session = MagicMock()
    auth_session.get_and_revoke_refresh_token = AsyncMock(
        return_value=RefreshTokenRecord(user_id=7, token_version=stored_version)
    )
    return RefreshSession(user_dao=user_dao, auth_session=auth_session), user


@pytest.mark.asyncio
async def test_refresh_session_accepts_current_token_version() -> None:
    interactor, user = make_interactor(stored_version=3, current_version=3)

    result = await interactor.system(RefreshSessionDto(refresh_token="token"))

    assert result is user


@pytest.mark.asyncio
async def test_refresh_session_rejects_invalidated_token_version() -> None:
    interactor, _ = make_interactor(stored_version=2, current_version=3)

    with pytest.raises(HTTPException) as raised:
        await interactor.system(RefreshSessionDto(refresh_token="token"))

    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert raised.value.detail == "Invalid or expired refresh token"
