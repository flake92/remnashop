"""What redeeming a gift code does to the recipient's subscription.

One pure function decides the outcome, so the confirmation shown before
activation (bot and web preview) and the activation itself always agree.

Rules:
- no subscription: a new one on the gift plan for the gift term;
- trial, lapsed or grace subscription: the gift term starts now on the gift plan;
- the same paid plan: the gift term is added to the remaining time;
- a different paid plan: the remaining time is converted into days of the gift
  plan at the price per day the customer paid (the plan-change credit), then the
  gift term is added. Without prices to compare, the remaining days are kept;
- an unlimited gift plan makes the subscription unlimited.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Optional, Sequence

from adaptix import Retort

from src.application.common import Interactor
from src.application.common.dao import PlanDao, SubscriptionDao
from src.application.common.policy import Permission
from src.application.dto import PlanSnapshotDto, PromocodeDto, SubscriptionDto, UserDto
from src.application.use_cases.promocode.queries.validate import (
    ValidatePromocode,
    ValidatePromocodeDto,
)
from src.application.use_cases.subscription.change_credit import change_credit
from src.core.utils.converters import days_to_datetime
from src.core.utils.time import datetime_now

UNLIMITED_YEAR = 2099


class GiftOutcomeKind(StrEnum):
    NEW = "NEW"
    RESTART = "RESTART"
    EXTEND = "EXTEND"
    CONVERT = "CONVERT"
    KEEP_DAYS = "KEEP_DAYS"
    UNLIMITED = "UNLIMITED"


@dataclass(frozen=True)
class GiftOutcome:
    kind: GiftOutcomeKind
    plan_name: str
    gift_days: int
    expire_at: datetime
    current_plan_name: Optional[str] = None
    remaining_days: int = 0
    credit_days: int = 0

    @property
    def is_unlimited(self) -> bool:
        return self.kind == GiftOutcomeKind.UNLIMITED


def _keeps_remaining(subscription: SubscriptionDto) -> bool:
    return (
        not subscription.is_trial
        and subscription.grace_until is None
        and subscription.is_active
        and subscription.plan_snapshot is not None
        and not subscription.plan_snapshot.is_trial
    )


def compute_gift_outcome(
    *,
    subscription: Optional[SubscriptionDto],
    gift_plan: PlanSnapshotDto,
    current_durations: Optional[Sequence],
    gift_durations: Optional[Sequence],
    now: datetime,
) -> GiftOutcome:
    days = gift_plan.duration
    name = gift_plan.name
    if days == 0:
        return GiftOutcome(GiftOutcomeKind.UNLIMITED, name, 0, days_to_datetime(0))
    if subscription is None:
        return GiftOutcome(GiftOutcomeKind.NEW, name, days, now + timedelta(days=days))
    if not _keeps_remaining(subscription):
        return GiftOutcome(GiftOutcomeKind.RESTART, name, days, now + timedelta(days=days))

    current = subscription.plan_snapshot
    assert current is not None  # guaranteed by _keeps_remaining
    remaining = max(subscription.expire_at - now, timedelta(0))
    remaining_days = remaining.days
    unlimited_now = current.duration == 0 or subscription.expire_at.year >= UNLIMITED_YEAR
    if current.id == gift_plan.id or unlimited_now:
        return GiftOutcome(
            GiftOutcomeKind.EXTEND,
            name,
            days,
            max(subscription.expire_at, now) + timedelta(days=days),
            current_plan_name=current.name,
            remaining_days=remaining_days,
            credit_days=remaining_days,
        )

    credit = (
        change_credit(
            old_expire_at=subscription.expire_at,
            now=now,
            old_durations=current_durations,
            new_durations=gift_durations,
            new_days=days,
            old_days=current.duration,
        )
        if current_durations and gift_durations
        else None
    )
    if credit is None:
        # No prices to compare: keep the remaining time as it is.
        return GiftOutcome(
            GiftOutcomeKind.KEEP_DAYS,
            name,
            days,
            max(subscription.expire_at, now) + timedelta(days=days),
            current_plan_name=current.name,
            remaining_days=remaining_days,
            credit_days=remaining_days,
        )
    return GiftOutcome(
        GiftOutcomeKind.CONVERT,
        name,
        days,
        now + timedelta(days=credit.days + days),
        current_plan_name=current.name,
        remaining_days=remaining_days,
        credit_days=credit.days,
    )


async def resolve_gift_outcome(
    *,
    promo: PromocodeDto,
    subscription: Optional[SubscriptionDto],
    plan_dao: PlanDao,
    retort: Retort,
    now: Optional[datetime] = None,
) -> GiftOutcome:
    if not promo.plan_snapshot:
        raise ValueError(f"Gift code id='{promo.id}' has no plan")
    gift_plan = retort.load(promo.plan_snapshot, PlanSnapshotDto)
    current = subscription.plan_snapshot if subscription else None
    current_plan = await plan_dao.get_by_id(current.id) if current else None
    gift_source = await plan_dao.get_by_id(gift_plan.id)
    return compute_gift_outcome(
        subscription=subscription,
        gift_plan=gift_plan,
        current_durations=current_plan.durations if current_plan else None,
        gift_durations=gift_source.durations if gift_source else None,
        now=now or datetime_now(),
    )


@dataclass(frozen=True)
class PreviewGiftDto:
    code: str
    user: UserDto


@dataclass(frozen=True)
class GiftPreviewDto:
    promo: PromocodeDto
    outcome: Optional[GiftOutcome]


class PreviewGift(Interactor[PreviewGiftDto, GiftPreviewDto]):
    """Validate a code without redeeming it and, for a gift, say what it will do."""

    required_permission = Permission.PUBLIC

    def __init__(
        self,
        validate_promocode: ValidatePromocode,
        subscription_dao: SubscriptionDao,
        plan_dao: PlanDao,
        retort: Retort,
    ) -> None:
        self.validate_promocode = validate_promocode
        self.subscription_dao = subscription_dao
        self.plan_dao = plan_dao
        self.retort = retort

    async def _execute(self, actor: UserDto, data: PreviewGiftDto) -> GiftPreviewDto:
        promo = await self.validate_promocode(
            actor, ValidatePromocodeDto(code=data.code, user=data.user)
        )
        if not promo.is_gift:
            return GiftPreviewDto(promo=promo, outcome=None)
        subscription = await self.subscription_dao.get_current(data.user.id)
        outcome = await resolve_gift_outcome(
            promo=promo, subscription=subscription, plan_dao=self.plan_dao, retort=self.retort
        )
        return GiftPreviewDto(promo=promo, outcome=outcome)
