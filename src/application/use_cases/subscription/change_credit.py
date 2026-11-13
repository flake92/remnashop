"""Credit for the unused part of a subscription when the customer switches plans.

Formula (the way support recalculated by hand; the support bot shows the same numbers):

    credit_days = floor(remaining_days * rate(old_plan, D_old) / rate(new_plan, D_new))

    remaining_days  time left on the current subscription, in days (fractional; referral
                    bonus days are part of it and are valued the same way)
    D_old           duration the customer bought last time (subscription plan snapshot)
    D_new           duration of the new purchase
    rate(plan, D)   price per day in RUB: price(plan, D) / D; if the plan no longer has
                    duration D, the duration closest to D is used

So the unused days are valued at what a day cost the customer, and that value is turned
into days of the new plan. New expiry = now + D_new + credit_days. No credit for trials,
unlimited or lifetime subscriptions, a lifetime purchase, an expired/disabled subscription
or a free old plan.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Optional, Sequence

from src.core.enums import Currency, SubscriptionStatus

REFERENCE_CURRENCY = Currency.RUB
SECONDS_PER_DAY = Decimal(86400)


@dataclass(frozen=True)
class ChangeCredit:
    days: int
    remaining_days: Decimal
    old_rate: Decimal
    new_rate: Decimal


def daily_rate(
    durations: Sequence, days: int, currency: Currency = REFERENCE_CURRENCY
) -> Optional[Decimal]:
    """Price per day for `days`, falling back to the closest priced duration."""
    priced = []
    for duration in durations:
        if duration.days <= 0:
            continue
        price = next((p.price for p in duration.prices if p.currency == currency), None)
        if price is None:
            continue
        priced.append((abs(duration.days - days), duration.days, Decimal(price)))
    if not priced:
        return None
    _, tier_days, price = min(priced)
    return price / Decimal(tier_days)


@dataclass(frozen=True)
class ChangeCreditQuote:
    enabled: bool  # admin switch «Перерасчёт при смене тарифа»
    credit: Optional[ChangeCredit]

    @property
    def days(self) -> int:
        return self.credit.days if self.credit else 0


async def quote_change_credit(
    *,
    subscription,  # SubscriptionDto (current) or None
    new_plan_id: int,
    new_days: int,
    plan_dao,
    settings_dao,
    now: datetime,
) -> ChangeCreditQuote:
    """Single source of truth for the confirm screen and for fulfillment."""
    settings = await settings_dao.get()
    if not settings.extra.change_credit:
        return ChangeCreditQuote(enabled=False, credit=None)

    old = subscription.plan_snapshot if subscription else None
    if (
        old is None
        or subscription.is_trial
        or old.is_trial
        or old.duration == 0
        or new_days == 0
        or subscription.grace_until is not None
        or subscription.status not in {SubscriptionStatus.ACTIVE, SubscriptionStatus.LIMITED}
        or subscription.expire_at is None
        or subscription.expire_at.year >= 2099
    ):
        return ChangeCreditQuote(enabled=True, credit=None)

    old_plan = await plan_dao.get_by_id(old.id)
    new_plan = await plan_dao.get_by_id(new_plan_id)
    if not old_plan or not new_plan:
        return ChangeCreditQuote(enabled=True, credit=None)
    return ChangeCreditQuote(
        enabled=True,
        credit=change_credit(
            old_expire_at=subscription.expire_at,
            now=now,
            old_durations=old_plan.durations,
            new_durations=new_plan.durations,
            new_days=new_days,
            old_days=old.duration,
        ),
    )


def change_credit(
    *,
    old_expire_at: datetime,
    now: datetime,
    old_durations: Sequence,
    new_durations: Sequence,
    new_days: int,
    old_days: int,
) -> Optional[ChangeCredit]:
    if new_days <= 0 or old_days <= 0:
        return None
    remaining = Decimal((old_expire_at - now).total_seconds()) / SECONDS_PER_DAY
    if remaining <= 0:
        return None
    old_rate = daily_rate(old_durations, old_days)
    new_rate = daily_rate(new_durations, new_days)
    if not old_rate or not new_rate or old_rate <= 0 or new_rate <= 0:
        return None
    days = int((remaining * old_rate / new_rate).to_integral_value(rounding=ROUND_FLOOR))
    if days <= 0:
        return None
    return ChangeCredit(days=days, remaining_days=remaining, old_rate=old_rate, new_rate=new_rate)
