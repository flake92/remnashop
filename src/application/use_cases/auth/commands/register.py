from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from src.application.common import Interactor
from src.application.common.dao import UserDao
from src.application.common.password_hasher import PasswordHasher
from src.application.dto import UserDto
from src.application.use_cases.user.commands.web_registration import (
    RegisterWebUser,
    RegisterWebUserDto,
)
from src.core.config import AppConfig
from src.core.enums import AuthType
from src.core.utils.time import datetime_now


@dataclass
class RegisterEmailUserDto:
    email: str
    password: str
    name: Optional[str] = None
    referral_code: Optional[str] = None
    # Explicit opt-in to news e-mail, asked on the registration form.
    email_news_consent: bool = False


class RegisterEmailUser(Interactor[RegisterEmailUserDto, UserDto]):
    required_permission = None

    def __init__(
        self,
        config: AppConfig,
        user_dao: UserDao,
        password_hasher: PasswordHasher,
        register_web_user: RegisterWebUser,
    ) -> None:
        self.config = config
        self.user_dao = user_dao
        self.password_hasher = password_hasher
        self.register_web_user = register_web_user

    async def _execute(self, actor: UserDto, data: RegisterEmailUserDto) -> UserDto:
        if await self.user_dao.get_by_email(data.email):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")

        referral_code = data.referral_code
        if referral_code and not await self.user_dao.get_by_referral_code(referral_code):
            referral_code = None

        new_user = UserDto(
            telegram_id=None,
            auth_type=AuthType.EMAIL,
            email=data.email,
            password_hash=self.password_hasher.hash(data.password),
            username=None,
            name=data.name or data.email.split("@")[0],
            language=self.config.default_locale,
            email_news_enabled=data.email_news_consent,
            email_news_enabled_at=datetime_now() if data.email_news_consent else None,
        )

        try:
            return await self.register_web_user.system(
                RegisterWebUserDto(user=new_user, referral_code=referral_code)
            )
        except IntegrityError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User with this email already exists",
            ) from e
