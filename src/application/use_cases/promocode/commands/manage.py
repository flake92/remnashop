from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import PromocodeDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import PromocodeDto, UserDto
from src.core.enums import PromocodeAvailability, PromocodeRewardType


@dataclass(frozen=True)
class CreatePromocodeDto:
    code: str
    reward_type: PromocodeRewardType
    reward: Optional[int] = None
    plan_snapshot: Optional[dict] = None
    availability: PromocodeAvailability = PromocodeAvailability.ALL
    expires_at: Optional[datetime] = None
    max_activations: Optional[int] = None
    is_reusable: bool = False


class CreatePromocode(Interactor[CreatePromocodeDto, PromocodeDto]):
    required_permission = Permission.MANAGE_PROMOCODE

    def __init__(self, uow: UnitOfWork, promocode_dao: PromocodeDao) -> None:
        self.uow = uow
        self.promocode_dao = promocode_dao

    # Reward types that are meaningless without a positive reward amount. SUBSCRIPTION
    # carries a plan_snapshot instead of a numeric reward, so it is excluded.
    _REWARD_REQUIRED = {
        PromocodeRewardType.DURATION,
        PromocodeRewardType.TRAFFIC,
        PromocodeRewardType.DEVICES,
        PromocodeRewardType.PERSONAL_DISCOUNT,
        PromocodeRewardType.PURCHASE_DISCOUNT,
    }

    # Discount rewards are percentages, so they are bounded to 1..100.
    _DISCOUNT_TYPES = {
        PromocodeRewardType.PERSONAL_DISCOUNT,
        PromocodeRewardType.PURCHASE_DISCOUNT,
    }

    async def _execute(self, actor: UserDto, data: CreatePromocodeDto) -> PromocodeDto:
        if data.reward_type in self._REWARD_REQUIRED:
            # DURATION/DEVICES accept 0 as "unlimited" (permanent subscription / unlimited
            # devices); other reward types require a strictly positive amount.
            unlimited_zero = {PromocodeRewardType.DURATION, PromocodeRewardType.DEVICES}
            min_reward = 0 if data.reward_type in unlimited_zero else 1
            if data.reward is None or data.reward < min_reward:
                raise ValueError(
                    f"Reward must be >= {min_reward} for reward type '{data.reward_type}'"
                )

        if data.reward_type in self._DISCOUNT_TYPES:
            # Discount is a percentage; reject anything outside 1..100.
            if data.reward is None or not 1 <= data.reward <= 100:
                raise ValueError(
                    f"Discount must be between 1 and 100 for reward type '{data.reward_type}'"
                )

        existing = await self.promocode_dao.get_by_code(data.code)
        if existing:
            raise ValueError(f"Promocode with code '{data.code}' already exists")

        promo = PromocodeDto(
            code=data.code,
            is_active=True,
            reward_type=data.reward_type,
            reward=data.reward,
            plan_snapshot=data.plan_snapshot,
            availability=data.availability,
            expires_at=data.expires_at,
            max_activations=data.max_activations,
            is_reusable=data.is_reusable,
        )

        async with self.uow:
            created = await self.promocode_dao.create(promo)
            await self.uow.commit()

        logger.info(f"{actor.log} Created promocode id='{created.id}'")
        return created


class UpdatePromocode(Interactor[PromocodeDto, Optional[PromocodeDto]]):
    required_permission = Permission.MANAGE_PROMOCODE

    def __init__(self, uow: UnitOfWork, promocode_dao: PromocodeDao) -> None:
        self.uow = uow
        self.promocode_dao = promocode_dao

    async def _execute(self, actor: UserDto, data: PromocodeDto) -> Optional[PromocodeDto]:
        async with self.uow:
            updated = await self.promocode_dao.update(data.as_fully_changed())
            await self.uow.commit()
        if updated:
            logger.info(f"{actor.log} Updated promocode id={data.id}")
        return updated


class DeletePromocode(Interactor[int, bool]):
    required_permission = Permission.MANAGE_PROMOCODE

    def __init__(self, uow: UnitOfWork, promocode_dao: PromocodeDao) -> None:
        self.uow = uow
        self.promocode_dao = promocode_dao

    async def _execute(self, actor: UserDto, promocode_id: int) -> bool:
        async with self.uow:
            deleted = await self.promocode_dao.delete(promocode_id)
            await self.uow.commit()
        if deleted:
            logger.info(f"{actor.log} Deleted promocode id={promocode_id}")
        return deleted
