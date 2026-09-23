from dataclasses import dataclass

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import SettingsDao, UserDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import UserDto
from src.application.use_cases.blacklist.limits import (
    MAX_BLACKLIST_IDS_PER_SOURCE,
    MAX_PERSISTED_BLACKLIST_IDS,
    MAX_TELEGRAM_USER_ID,
)
from src.core.exceptions import PermissionDeniedError, UserNotFoundError


@dataclass(frozen=True)
class BlockUsersResult:
    blocked_users: int
    blocked_ids: int
    already_blocked: int


class BlockUsersByIds(Interactor[list[int], BlockUsersResult]):
    required_permission = Permission.BLACKLIST

    def __init__(self, uow: UnitOfWork, user_dao: UserDao, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.user_dao = user_dao
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, telegram_ids: list[int]) -> BlockUsersResult:
        unique_ids = list(dict.fromkeys(telegram_ids))  # deduplicate, preserve order
        if len(unique_ids) > MAX_BLACKLIST_IDS_PER_SOURCE:
            raise ValueError(f"Cannot block more than {MAX_BLACKLIST_IDS_PER_SOURCE} IDs at once")
        if any(user_id < 1 or user_id > MAX_TELEGRAM_USER_ID for user_id in unique_ids):
            raise ValueError("Telegram user IDs must be positive signed 64-bit integers")

        existing_users = await self.user_dao.get_by_telegram_ids(unique_ids)
        existing_map = {u.telegram_id: u for u in existing_users}

        protected = [u for u in existing_users if not actor.role > u.role]
        protected_ids = {u.telegram_id for u in protected}

        if protected:
            logger.warning(
                f"{actor.log} Bulk block: skipping {len(protected)} user(s) with role >= actor role"
            )

        skipped = sum(
            1 for u in existing_users if u.is_blocked and u.telegram_id not in protected_ids
        )
        to_block = [
            tid
            for tid in unique_ids
            if tid in existing_map and not existing_map[tid].is_blocked and tid not in protected_ids
        ]
        unknown_ids = [tid for tid in unique_ids if tid not in existing_map]

        blocked_existing = 0
        blocked_ids = 0
        already_in_list = 0

        async with self.uow:
            if to_block:
                blocked_existing = await self.user_dao.block_by_telegram_ids(to_block)

            if unknown_ids:
                settings = await self.settings_dao.get()
                existing_set = set(settings.blacklist.blocked_ids)
                new_ids = [tid for tid in unknown_ids if tid not in existing_set]
                already_in_list = len(unknown_ids) - len(new_ids)
                if new_ids:
                    combined_blocked_ids = list(
                        dict.fromkeys(settings.blacklist.blocked_ids + new_ids)
                    )
                    if len(combined_blocked_ids) > MAX_PERSISTED_BLACKLIST_IDS:
                        raise ValueError(
                            "Blacklist storage limit exceeded; remove obsolete IDs first"
                        )
                    settings.blacklist.blocked_ids = combined_blocked_ids
                    await self.settings_dao.update(settings)
                blocked_ids = len(new_ids)

            await self.uow.commit()

        logger.info(
            f"{actor.log} Bulk block: {blocked_existing} existing blocked, "
            f"{blocked_ids} added to blocked_ids, {skipped + already_in_list} already blocked, "
            f"{len(protected)} skipped by role"
        )
        return BlockUsersResult(
            blocked_users=blocked_existing,
            blocked_ids=blocked_ids,
            already_blocked=skipped + already_in_list,
        )


@dataclass(frozen=True)
class SetBotBlockedStatusDto:
    user_id: int
    is_blocked: bool


class SetBotBlockedStatus(Interactor[SetBotBlockedStatusDto, None]):
    required_permission = None

    def __init__(self, uow: UnitOfWork, user_dao: UserDao) -> None:
        self.uow = uow
        self.user_dao = user_dao

    async def _execute(self, actor: UserDto, data: SetBotBlockedStatusDto) -> None:
        async with self.uow:
            await self.user_dao.set_bot_blocked_status(data.user_id, data.is_blocked)
            await self.uow.commit()

        logger.info(f"Set bot blocked status for user_id='{data.user_id}' to '{data.is_blocked}'")


class ToggleUserBlockedStatus(Interactor[int, None]):
    required_permission = Permission.USER_EDITOR

    def __init__(self, uow: UnitOfWork, user_dao: UserDao) -> None:
        self.uow = uow
        self.user_dao = user_dao

    async def _execute(self, actor: UserDto, user_id: int) -> None:
        target_user = await self.user_dao.get_by_id(user_id)
        if target_user is None:
            raise UserNotFoundError(user_id)

        if not actor.role > target_user.role:
            logger.warning(
                f"{actor.log} Attempted to toggle block for user '{user_id}' "
                f"with role '{target_user.role}' — insufficient role"
            )
            raise PermissionDeniedError

        async with self.uow:
            await self.user_dao.toggle_blocked_status(target_user.id)
            await self.uow.commit()

        logger.info(f"{actor.log} Toggled user '{user_id}' blocked status")


class ClearBlockedIds(Interactor[None, int]):
    required_permission = Permission.BLACKLIST

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, data: None) -> int:
        settings = await self.settings_dao.get()
        count = len(settings.blacklist.blocked_ids)

        if count > 0:
            async with self.uow:
                settings.blacklist.blocked_ids = []
                await self.settings_dao.update(settings)
                await self.uow.commit()
            logger.info(f"{actor.log} Cleared {count} blocked IDs from settings")

        return count


class UnblockAllUsers(Interactor[None, int]):
    required_permission = Permission.UNBLOCK_ALL

    def __init__(self, uow: UnitOfWork, user_dao: UserDao) -> None:
        self.uow = uow
        self.user_dao = user_dao

    async def _execute(self, actor: UserDto, data: None) -> int:
        blocked_count = await self.user_dao.count_blocked()

        if blocked_count > 0:
            async with self.uow:
                await self.user_dao.unblock_all()
                await self.uow.commit()

            logger.warning(f"{actor.log} Unblocked all '{blocked_count}' users")
        else:
            logger.info(f"{actor.log} Attempted to unblock all, but blacklist is empty")

        return blocked_count
