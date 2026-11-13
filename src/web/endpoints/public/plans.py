from decimal import Decimal

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter
from redis.asyncio import Redis

from src.application.common.dao import PlanDao, SettingsDao
from src.core.constants import PUBLIC_LANDING_PLANS_CACHE_TTL_SECONDS
from src.core.enums import Currency, PlanAvailability
from src.web.schemas import PublicPlanLandingListResponse, PublicPlanLandingResponse
from src.web.schemas.plans import PublicPlanDurationResponse

from ._common import _normalize_decimal_str

router = APIRouter(tags=["Public - Plans"])

_CACHE_KEY = "cache:public_landing_plans:v2"


@router.get("/plans/public", response_model=PublicPlanLandingListResponse)
@inject
async def get_public_landing_plans(
    plan_dao: FromDishka[PlanDao],
    settings_dao: FromDishka[SettingsDao],
    redis: FromDishka[Redis],
) -> PublicPlanLandingListResponse:
    # the switch is read on every request: the plan list is cached for hours
    change_credit_enabled = (await settings_dao.get()).extra.change_credit
    cached = await redis.get(_CACHE_KEY)
    if cached is not None:
        payload = PublicPlanLandingListResponse.model_validate_json(cached)
        payload.change_credit_enabled = change_credit_enabled
        return payload

    plans = await plan_dao.filter_by_availability(PlanAvailability.ALL)

    result: list[PublicPlanLandingResponse] = []
    for plan in plans:
        if not plan.is_active or plan.is_trial or not plan.public_code:
            continue

        rub_duration_candidates: list[tuple[int, Decimal]] = []
        for duration in plan.durations:
            if duration.days <= 0:
                continue

            rub_price = next((p.price for p in duration.prices if p.currency == Currency.RUB), None)
            if rub_price is not None:
                rub_duration_candidates.append((duration.days, rub_price))

        if not rub_duration_candidates:
            continue

        max_duration_days, max_duration_price = max(
            rub_duration_candidates,
            key=lambda item: item[0],
        )
        monthly_from = (max_duration_price * Decimal(30)) / Decimal(max_duration_days)

        result.append(
            PublicPlanLandingResponse(
                public_code=plan.public_code,
                name=plan.name,
                description=plan.description,
                traffic_limit=plan.traffic_limit,
                device_limit=plan.device_limit,
                monthly_from_rub=_normalize_decimal_str(monthly_from),
                max_duration_days=max_duration_days,
                max_duration_price_rub=_normalize_decimal_str(max_duration_price),
                durations=[
                    PublicPlanDurationResponse(days=d, price_rub=_normalize_decimal_str(p))
                    for d, p in sorted(rub_duration_candidates)
                ],
            )
        )

    payload = PublicPlanLandingListResponse(
        plans=result, change_credit_enabled=change_credit_enabled
    )
    await redis.setex(_CACHE_KEY, PUBLIC_LANDING_PLANS_CACHE_TTL_SECONDS, payload.model_dump_json())
    return payload
