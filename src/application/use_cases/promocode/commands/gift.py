import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from adaptix import Retort
from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import PromocodeDao
from src.application.common.uow import UnitOfWork
from src.application.dto import GiftDto, PromocodeDto, TransactionDto, UserDto
from src.core.enums import PromocodeAvailability, PromocodeRewardType, PurchaseType
from src.core.utils.time import datetime_now

# How long a bought gift code stays redeemable.
GIFT_CODE_TTL: Final[timedelta] = timedelta(days=365)

# No 0/O, 1/I/L: the code is read aloud and retyped by people.
_GIFT_ALPHABET: Final[str] = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_GIFT_GROUPS: Final[int] = 3
_GIFT_GROUP_LENGTH: Final[int] = 4
_GIFT_CODE_ATTEMPTS: Final[int] = 5


def generate_gift_code() -> str:
    groups = (
        "".join(secrets.choice(_GIFT_ALPHABET) for _ in range(_GIFT_GROUP_LENGTH))
        for _ in range(_GIFT_GROUPS)
    )
    return "GIFT-" + "-".join(groups)


@dataclass(frozen=True)
class IssueGiftDto:
    buyer: UserDto
    transaction: TransactionDto


class IssueGift(Interactor[IssueGiftDto, PromocodeDto]):
    """Turn a paid GIFT transaction into a single-use subscription code.

    Idempotent per payment: a replayed fulfillment returns the code that was
    already issued instead of minting a second one.
    """

    required_permission = None

    def __init__(self, uow: UnitOfWork, promocode_dao: PromocodeDao, retort: Retort) -> None:
        self.uow = uow
        self.promocode_dao = promocode_dao
        self.retort = retort

    async def _execute(self, actor: UserDto, data: IssueGiftDto) -> PromocodeDto:
        transaction = data.transaction
        if transaction.purchase_type != PurchaseType.GIFT:
            raise ValueError(f"Transaction '{transaction.payment_id}' is not a gift purchase")

        existing = await self.promocode_dao.get_by_gift_payment_id(transaction.payment_id)
        if existing is not None:
            logger.info(f"{actor.log} Gift for payment '{transaction.payment_id}' already issued")
            return existing

        plan_snapshot = self.retort.dump(transaction.plan_snapshot)
        last_error: Exception | None = None
        for _ in range(_GIFT_CODE_ATTEMPTS):
            try:
                # The unit of work rolls back when the flush fails, so the session
                # is usable again for the lookup below.
                async with self.uow:
                    promo = await self.promocode_dao.create(
                        PromocodeDto(
                            code=generate_gift_code(),
                            is_active=True,
                            reward_type=PromocodeRewardType.SUBSCRIPTION,
                            plan_snapshot=plan_snapshot,
                            availability=PromocodeAvailability.ALL,
                            expires_at=datetime_now() + GIFT_CODE_TTL,
                            max_activations=1,
                            is_reusable=False,
                            gift_payment_id=transaction.payment_id,
                            gift_buyer_id=data.buyer.id,
                        )
                    )
                    await self.uow.commit()
            except ValueError as e:
                # A code collision or a concurrent replay of the same payment.
                last_error = e
                concurrent = await self.promocode_dao.get_by_gift_payment_id(transaction.payment_id)
                if concurrent is not None:
                    return concurrent
                continue
            logger.info(
                f"{actor.log} Issued gift code id='{promo.id}' "
                f"for payment '{transaction.payment_id}'"
            )
            return promo

        raise RuntimeError(
            f"Could not issue a unique gift code for payment '{transaction.payment_id}'"
        ) from last_error


@dataclass(frozen=True)
class GetUserGiftsDto:
    user: UserDto
    limit: int = 100


class GetUserGifts(Interactor[GetUserGiftsDto, list[GiftDto]]):
    required_permission = None

    def __init__(self, promocode_dao: PromocodeDao) -> None:
        self.promocode_dao = promocode_dao

    async def _execute(self, actor: UserDto, data: GetUserGiftsDto) -> list[GiftDto]:
        return await self.promocode_dao.get_gifts_by_buyer(data.user.id, limit=data.limit)
