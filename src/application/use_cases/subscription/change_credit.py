"""Credit for the unused part of a subscription when the customer switches plans.

Formula (the support bot uses the same one):

    credit_days = floor(remaining_days * rate(old_plan, D) / rate(new_plan, D))

    remaining_days  time left on the current subscription, in days (fractional)
    D               duration of the new purchase, in days
    rate(plan, D)   price per day of `plan` for duration D in the reference currency (RUB):
                    price(plan, D) / D; if the plan has no duration D, the duration closest
                    to D is used (price of that duration / its days)

New expiry = now + D + credit_days. With the same duration on both plans this is just
remaining_days * price(old, D) / price(new, D). No credit for trials, unlimited or lifetime
subscriptions, a lifetime purchase, an expired/disabled subscription or a free old plan.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Optional, Sequence

from src.core.enums import Currency

REFERENCE_CURRENCY = Currency.RUB
SECONDS_PER_DAY = Decimal(86400)


@dataclass(frozen=True)
class ChangeCredit:
    days: int
    remaining_days: Decimal
    old_rate: Decimal
    new_rate: Decimal


def daily_rate(durations: Sequence, days: int, currency: Currency = REFERENCE_CURRENCY) -> Optional[Decimal]:
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


def change_credit(
    *,
    old_expire_at: datetime,
    now: datetime,
    old_durations: Sequence,
    new_durations: Sequence,
    new_days: int,
) -> Optional[ChangeCredit]:
    if new_days <= 0:
        return None
    remaining = Decimal((old_expire_at - now).total_seconds()) / SECONDS_PER_DAY
    if remaining <= 0:
        return None
    old_rate = daily_rate(old_durations, new_days)
    new_rate = daily_rate(new_durations, new_days)
    if not old_rate or not new_rate or old_rate <= 0 or new_rate <= 0:
        return None
    days = int((remaining * old_rate / new_rate).to_integral_value(rounding=ROUND_FLOOR))
    if days <= 0:
        return None
    return ChangeCredit(days=days, remaining_days=remaining, old_rate=old_rate, new_rate=new_rate)
