from dataclasses import dataclass
from uuid import uuid4

from loguru import logger

from src.application.common import Interactor, Notifier
from src.application.common.dao import UserDao
from src.application.common.notification_channels import NotificationCategory
from src.application.common.policy import Permission
from src.application.dto import MessagePayloadDto, UserDto
from src.core.exceptions import PermissionDeniedError


@dataclass(frozen=True)
class SendMessageToUserDto:
    user_id: int
    payload: MessagePayloadDto


class SendMessageToUser(Interactor[SendMessageToUserDto, bool]):
    required_permission = Permission.USER_EDITOR

    def __init__(
        self,
        user_dao: UserDao,
        notifier: Notifier,
    ) -> None:
        self.user_dao = user_dao
        self.notifier = notifier

    async def _execute(self, actor: UserDto, data: SendMessageToUserDto) -> bool:
        target_user = await self.user_dao.get_by_id(data.user_id)
        if not target_user:
            raise ValueError(f"User '{data.user_id}' not found")

        if not actor.role > target_user.role:
            logger.warning(
                f"{actor.log} denied editing user '{target_user.id}': "
                f"target role '{target_user.role}' >= actor role '{actor.role}'"
            )
            raise PermissionDeniedError()

        # Queued first: a web-only user has no Telegram chat, and a Telegram
        # failure must not lose the copies in the feed, e-mail and push.
        queued = await self.notifier.copy_to_channels(
            [target_user],
            data.payload,
            category=NotificationCategory.SERVICE,
            dedupe_prefix=f"direct:{uuid4().hex}",
        )
        message = await self.notifier.notify_user(user=target_user, payload=data.payload)

        if message or queued:
            logger.info(f"{actor.log} Sent message to user '{data.user_id}'")
            return True

        logger.warning(f"{actor.log} Failed to send message to user '{data.user_id}'")
        return False
