from dataclasses import dataclass

from fastapi import HTTPException, status

from src.application.common import Interactor
from src.application.common.dao import UserDao
from src.application.common.dao.auth import AuthSessionDao
from src.application.dto import UserDto


@dataclass
class RefreshSessionDto:
    refresh_token: str


class RefreshSession(Interactor[RefreshSessionDto, UserDto]):
    required_permission = None

    def __init__(self, user_dao: UserDao, auth_session: AuthSessionDao) -> None:
        self.user_dao = user_dao
        self.auth_session = auth_session

    async def _execute(self, actor: UserDto, data: RefreshSessionDto) -> UserDto:
        token_record = await self.auth_session.get_and_revoke_refresh_token(data.refresh_token)
        if token_record is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )
        user = await self.user_dao.get_by_id(token_record.user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        if token_record.token_version != user.token_version:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )
        if user.is_blocked:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is blocked")
        return user
