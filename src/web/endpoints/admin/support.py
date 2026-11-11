"""Read-only plan-change quote for the support bot.

The support bot must tell a customer exactly what a plan change gives them. It asks
here instead of guessing the customer's plan from panel data: the quote uses the
customer's real subscription and the same function the confirm screen and the
fulfillment use (`quote_change_credit`). Authenticated by APP_SUPPORT_QUOTE_KEY,
which grants nothing else.
"""

from datetime import timedelta
from typing import Optional

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, Query, Security, status

from src.application.common.dao import PlanDao, SettingsDao, SubscriptionDao, UserDao
from src.application.use_cases.subscription.change_credit import quote_change_credit
from src.core.enums import Currency, PlanAvailability, SubscriptionStatus
from src.core.utils.time import datetime_now
from src.web.dependencies import require_support_quote_key
from src.web.schemas.admin import (
    SupportChangeOptionResponse,
    SupportChangeQuoteResponse,
    SupportSubscriptionResponse,
)

router = APIRouter(prefix="/support", tags=["Admin - Support"])


def _not_eligible_reason(subscription, enabled: bool) -> Optional[str]:  # type: ignore[no-untyped-def]
    if not enabled:
        return "switch_off"
    if subscription is None:
        return "no_subscription"
    snapshot = subscription.plan_snapshot
    if subscription.is_trial or (snapshot and snapshot.is_trial):
        return "trial"
    if snapshot is None or snapshot.duration == 0 or subscription.expire_at.year >= 2099:
        return "unlimited"
    if subscription.grace_until is not None or subscription.status not in {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.LIMITED,
    }:
        return "not_active"
    if subscription.expire_at <= datetime_now():
        return "expired"
    return None


@router.get("/change-quote", response_model=SupportChangeQuoteResponse)
@inject
async def change_quote(
    user_dao: FromDishka[UserDao],
    subscription_dao: FromDishka[SubscriptionDao],
    plan_dao: FromDishka[PlanDao],
    settings_dao: FromDishka[SettingsDao],
    telegram_id: int = Query(...),
    devices: Optional[int] = Query(default=None, ge=0),
    _: None = Security(require_support_quote_key),
) -> SupportChangeQuoteResponse:
    user = await user_dao.get_by_telegram_id(telegram_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    now = datetime_now()
    settings = await settings_dao.get()
    enabled = settings.extra.change_credit
    subscription = await subscription_dao.get_current(user.id)
    current = None
    current_plan_id = None
    if subscription is not None:
        snapshot = subscription.plan_snapshot
        current_plan_id = snapshot.id if snapshot else None
        remaining = (subscription.expire_at - now).total_seconds() / 86400
        current = SupportSubscriptionResponse(
            plan_id=current_plan_id,
            plan_name=snapshot.name if snapshot else None,
            device_limit=subscription.device_limit,
            status=str(subscription.status),
            is_trial=bool(subscription.is_trial or (snapshot and snapshot.is_trial)),
            expire_at=subscription.expire_at,
            remaining_days=round(max(remaining, 0.0), 2),
        )

    plans = [
        p
        for p in await plan_dao.filter_by_availability(PlanAvailability.ALL)
        if p.is_active and not p.is_trial and (devices is None or p.device_limit == devices)
    ]
    options: list[SupportChangeOptionResponse] = []
    for plan in sorted(plans, key=lambda p: (p.device_limit, p.order_index)):
        for duration in sorted(plan.durations, key=lambda d: d.days):
            if duration.days <= 0:
                continue
            price = next((x.price for x in duration.prices if x.currency == Currency.RUB), None)
            if price is None:
                continue
            renew = (
                subscription is not None
                and current_plan_id == plan.id
                and not (current and current.is_trial)
            )
            if renew:
                # same plan in the Telegram bot = renewal: days are simply added on top
                base = max(subscription.expire_at, now) if subscription.grace_until is None else now
                credit_days = 0
                new_expire = base + timedelta(days=duration.days)
            else:
                quote = await quote_change_credit(
                    subscription=subscription,
                    new_plan_id=plan.id,
                    new_days=duration.days,
                    plan_dao=plan_dao,
                    settings_dao=settings_dao,
                    now=now,
                )
                credit_days = quote.days
                new_expire = now + timedelta(days=duration.days + credit_days)
            options.append(
                SupportChangeOptionResponse(
                    plan_id=plan.id,
                    plan_name=plan.name,
                    public_code=plan.public_code,
                    device_limit=plan.device_limit,
                    days=duration.days,
                    price_rub=str(price),
                    purchase_type="RENEW" if renew else ("CHANGE" if subscription else "NEW"),
                    credit_days=credit_days,
                    new_expire_at=new_expire,
                )
            )

    return SupportChangeQuoteResponse(
        enabled=enabled,
        not_eligible_reason=_not_eligible_reason(subscription, enabled),
        subscription=current,
        options=options,
    )
