from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import SettingsDao, UserDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import BlacklistSourceDto, UserDto
from src.application.use_cases.blacklist.limits import MAX_PERSISTED_BLACKLIST_IDS
from src.application.use_cases.blacklist.queries.fetch import FetchBlacklistIds
from src.core.exceptions import BlacklistSourceAlreadyExistsError
from src.core.utils.validators import is_valid_public_https_url


@dataclass(frozen=True)
class AddBlacklistSourceDto:
    url: str
    name: Optional[str] = None


class AddBlacklistSource(Interactor[AddBlacklistSourceDto, BlacklistSourceDto]):
    required_permission = Permission.BLACKLIST

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, data: AddBlacklistSourceDto) -> BlacklistSourceDto:
        if not is_valid_public_https_url(data.url):
            raise ValueError("Blacklist source must use a public HTTPS URL")
        settings = await self.settings_dao.get()
        sources = settings.blacklist.sources

        if any(s.url == data.url for s in sources):
            raise BlacklistSourceAlreadyExistsError(
                f"Blacklist source with URL '{data.url}' already exists"
            )

        new_id = max((s.id for s in sources), default=0) + 1
        source = BlacklistSourceDto(id=new_id, url=data.url, name=data.name)
        settings.blacklist.sources = [*sources, source]

        async with self.uow:
            await self.settings_dao.update(settings)
            await self.uow.commit()

        logger.info(f"{actor.log} Added blacklist source id='{new_id}'")
        return source


class RemoveBlacklistSource(Interactor[int, bool]):
    required_permission = Permission.BLACKLIST

    def __init__(self, uow: UnitOfWork, settings_dao: SettingsDao) -> None:
        self.uow = uow
        self.settings_dao = settings_dao

    async def _execute(self, actor: UserDto, source_id: int) -> bool:
        settings = await self.settings_dao.get()
        sources = settings.blacklist.sources
        new_sources = [s for s in sources if s.id != source_id]

        if len(new_sources) == len(sources):
            return False

        settings.blacklist.sources = new_sources
        async with self.uow:
            await self.settings_dao.update(settings)
            await self.uow.commit()

        logger.info(f"{actor.log} Removed blacklist source '{source_id}'")
        return True


@dataclass(frozen=True)
class SyncResult:
    sources_synced: int
    blocked_users: int
    blocked_ids: int
    already_blocked: int


class SyncBlacklistSources(Interactor[None, SyncResult]):
    required_permission = Permission.BLACKLIST

    def __init__(
        self,
        uow: UnitOfWork,
        user_dao: UserDao,
        settings_dao: SettingsDao,
        fetch_blacklist_ids: FetchBlacklistIds,
    ) -> None:
        self.uow = uow
        self.user_dao = user_dao
        self.settings_dao = settings_dao
        self.fetch_blacklist_ids = fetch_blacklist_ids

    async def _execute(self, actor: UserDto, data: None) -> SyncResult:
        settings = await self.settings_dao.get()
        active_sources = settings.blacklist.sources

        if not active_sources:
            return SyncResult(sources_synced=0, blocked_users=0, blocked_ids=0, already_blocked=0)

        total_blocked_users = 0
        total_blocked_ids = 0
        total_already_blocked = 0
        synced = 0

        for source in active_sources:
            ids = await self.fetch_blacklist_ids.system(source.url)

            if not ids:
                logger.info(f"{actor.log} Synced blacklist source id='{source.id}': 0 IDs fetched")
                synced += 1
                continue

            existing_users = await self.user_dao.get_by_telegram_ids(ids)
            existing_map = {u.telegram_id: u for u in existing_users}
            to_block = [
                tid for tid in ids if tid in existing_map and not existing_map[tid].is_blocked
            ]
            already_blocked_users = [
                tid for tid in ids if tid in existing_map and existing_map[tid].is_blocked
            ]
            unknown_ids = [tid for tid in ids if tid not in existing_map]

            current = await self.settings_dao.get()
            existing_set = set(current.blacklist.blocked_ids)
            new_unknown = [tid for tid in unknown_ids if tid not in existing_set]
            already_in_list = [tid for tid in unknown_ids if tid in existing_set]

            async with self.uow:
                blocked = 0
                if to_block:
                    blocked = await self.user_dao.block_by_telegram_ids(to_block)

                if new_unknown:
                    combined_blocked_ids = list(
                        dict.fromkeys(current.blacklist.blocked_ids + new_unknown)
                    )
                    if len(combined_blocked_ids) > MAX_PERSISTED_BLACKLIST_IDS:
                        raise ValueError(
                            "Blacklist storage limit exceeded; remove obsolete sources or IDs"
                        )
                    current.blacklist.blocked_ids = combined_blocked_ids
                    await self.settings_dao.update(current)

                await self.uow.commit()

            total_blocked_users += blocked
            total_blocked_ids += len(new_unknown)
            total_already_blocked += len(already_blocked_users) + len(already_in_list)
            logger.info(
                f"{actor.log} Synced blacklist source id='{source.id}': "
                f"{len(ids)} IDs fetched, {blocked} users blocked, "
                f"{len(new_unknown)} added to blocked_ids"
            )
            synced += 1

        logger.warning(
            f"{actor.log} Blacklist sync complete: {synced} sources, "
            f"{total_blocked_users} users blocked, {total_blocked_ids} IDs added"
        )
        return SyncResult(
            sources_synced=synced,
            blocked_users=total_blocked_users,
            blocked_ids=total_blocked_ids,
            already_blocked=total_already_blocked,
        )
