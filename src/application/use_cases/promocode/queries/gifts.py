"""Operator views of paid gift codes (the bot dashboard)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.application.common import Interactor
from src.application.common.dao import PromocodeDao, TransactionDao, UserDao
from src.application.common.policy import Permission
from src.application.dto import PromocodeDto, TransactionDto, UserDto


@dataclass(frozen=True)
class GetGiftListDto:
    limit: int = 10
    offset: int = 0


@dataclass(frozen=True)
class GiftListDto:
    gifts: list[PromocodeDto]
    total: int
    redeemed_ids: frozenset[int]


class GetGiftList(Interactor[GetGiftListDto, GiftListDto]):
    """Every paid gift code, newest purchase first."""

    required_permission = Permission.VIEW_PROMOCODE

    def __init__(self, promocode_dao: PromocodeDao) -> None:
        self.promocode_dao = promocode_dao

    async def _execute(self, actor: UserDto, data: GetGiftListDto) -> GiftListDto:
        gifts = await self.promocode_dao.get_gift_list(limit=data.limit, offset=data.offset)
        redeemed: set[int] = set()
        for gift in gifts:
            if gift.id is not None and await self.promocode_dao.get_activations_count(gift.id):
                redeemed.add(gift.id)
        return GiftListDto(
            gifts=gifts,
            total=await self.promocode_dao.get_gift_count(),
            redeemed_ids=frozenset(redeemed),
        )


@dataclass(frozen=True)
class GiftDetailsDto:
    promo: PromocodeDto
    buyer: Optional[UserDto]
    transaction: Optional[TransactionDto]
    recipient: Optional[UserDto]
    activated_at: Optional[datetime]


class GetGiftDetails(Interactor[int, Optional[GiftDetailsDto]]):
    """One gift code with its buyer, payment and recipient."""

    required_permission = Permission.VIEW_PROMOCODE

    def __init__(
        self,
        promocode_dao: PromocodeDao,
        user_dao: UserDao,
        transaction_dao: TransactionDao,
    ) -> None:
        self.promocode_dao = promocode_dao
        self.user_dao = user_dao
        self.transaction_dao = transaction_dao

    async def _execute(self, actor: UserDto, promocode_id: int) -> Optional[GiftDetailsDto]:
        promo = await self.promocode_dao.get_by_id(promocode_id)
        if promo is None or not promo.is_gift or promo.id is None:
            return None

        buyer = (
            await self.user_dao.get_by_id(promo.gift_buyer_id)
            if promo.gift_buyer_id is not None
            else None
        )
        transaction = (
            await self.transaction_dao.get_by_payment_id(promo.gift_payment_id)
            if promo.gift_payment_id is not None
            else None
        )
        activations = await self.promocode_dao.get_activations(promo.id)
        first = activations[0] if activations else None
        recipient = await self.user_dao.get_by_id(first.user_id) if first else None
        return GiftDetailsDto(
            promo=promo,
            buyer=buyer,
            transaction=transaction,
            recipient=recipient,
            activated_at=first.activated_at if first else None,
        )
