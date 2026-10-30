from collections.abc import Sequence
from typing import Optional, Protocol, Union, runtime_checkable

from aiogram.types import Message

from src.application.dto import MessagePayloadDto, TempUserDto, UserDto
from src.core.enums import Role
from src.core.types import NotificationType

from .notification_channels import NotificationCategory


@runtime_checkable
class Notifier(Protocol):
    async def notify_user(
        self,
        user: Union[TempUserDto, UserDto],
        payload: Optional[MessagePayloadDto] = None,
        i18n_key: Optional[str] = None,
    ) -> Optional[Message]: ...

    async def notify_admins(
        self,
        payload: MessagePayloadDto,
        roles: list[Role] = [Role.OWNER, Role.DEV, Role.ADMIN],
    ) -> None: ...

    async def notify_system(
        self,
        payload: MessagePayloadDto,
        roles: list[Role] = [Role.OWNER, Role.DEV, Role.ADMIN],
        notification_type: Optional[NotificationType] = None,
    ) -> None: ...

    async def delete_notification(self, chat_id: int, message_id: int) -> None: ...

    async def copy_to_channels(
        self,
        users: Sequence[UserDto],
        payload: MessagePayloadDto,
        *,
        category: NotificationCategory,
        dedupe_prefix: str,
        skip_email: bool = False,
        source: Optional[str] = None,
    ) -> int:
        """Queue e-mail and Web Push copies; never raises."""
        ...
