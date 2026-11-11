from datetime import timedelta
from typing import Any, cast

from adaptix import Retort
from aiogram_dialog import DialogManager
from aiogram_dialog.api.exceptions import UnknownIntent
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import TranslatorRunner
from src.application.common.dao import PaymentGatewayDao, PlanDao, SettingsDao, SubscriptionDao
from src.application.dto import PaymentGatewayDto, PlanDto, PriceDetailsDto, TelegramUserDto
from src.application.services import PricingService
from src.application.use_cases.plan.queries.match import (
    MatchPlan,
    MatchPlanDto,
    resolve_renew_plan,
)
from src.application.use_cases.subscription.change_credit import quote_change_credit
from src.application.use_cases.user.queries.plans import GetAvailablePlans
from src.core.config import AppConfig
from src.core.enums import PurchaseType
from src.core.utils.time import datetime_now
from src.core.utils.i18n_helpers import (
    i18n_format_days,
    i18n_format_device_limit,
    i18n_format_expire_time,
    i18n_format_traffic_limit,
)
from src.telegram.routers.subscription.gift_flow import applies_discount, gift_getter_data


async def _change_quote_data(
    *,
    purchase_type: Any,
    user_id: int,
    plan_id: int,
    days: int,
    subscription_dao: SubscriptionDao,
    plan_dao: PlanDao,
    settings_dao: SettingsDao,
) -> dict[str, Any]:
    """What the customer gets when switching plans: shown on the confirm screen.

    Uses the same function as fulfillment, so the screen and the result always match.
    """
    data: dict[str, Any] = {"change_mode": "OFF", "credit_days": 0, "left_days": 0, "new_expire": ""}
    if purchase_type != PurchaseType.CHANGE:
        return data
    now = datetime_now()
    subscription = await subscription_dao.get_current(user_id)
    quote = await quote_change_credit(
        subscription=subscription,
        new_plan_id=plan_id,
        new_days=days,
        plan_dao=plan_dao,
        settings_dao=settings_dao,
        now=now,
    )
    if not quote.enabled:
        return data
    data["change_mode"] = "CREDIT" if quote.credit else "NONE"
    if quote.credit:
        data["credit_days"] = quote.credit.days
        data["left_days"] = int(quote.credit.remaining_days)
    if days > 0:
        data["new_expire"] = (now + timedelta(days=days + quote.days)).strftime("%d.%m.%Y")
    return data


def _get_gateway_title(i18n: TranslatorRunner, gateway: PaymentGatewayDto) -> str:
    if gateway.settings and gateway.settings.display_name:
        return gateway.settings.display_name

    return i18n.get("gateway-type", gateway_type=gateway.type)


@inject
async def subscription_getter(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    subscription_dao: FromDishka[SubscriptionDao],
    **kwargs: Any,
) -> dict[str, Any]:
    current_subscription = await subscription_dao.get_current(user.id)
    has_active = bool(current_subscription and not current_subscription.is_trial)
    is_unlimited = current_subscription.is_unlimited if current_subscription else False
    return {
        "has_active_subscription": has_active,
        "is_not_unlimited": not is_unlimited,
        "is_grace": bool(current_subscription and current_subscription.is_grace),
        "is_grace_indefinite": bool(
            current_subscription and current_subscription.is_grace_indefinite
        ),
        "grace_until": (
            i18n_format_expire_time(current_subscription.grace_until)
            if current_subscription and current_subscription.grace_until
            else None
        ),
    }


@inject
async def plan_getter(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    i18n: FromDishka[TranslatorRunner],
    plan_dao: FromDishka[PlanDao],
    subscription_dao: FromDishka[SubscriptionDao],
    match_plan: FromDishka[MatchPlan],
    settings_dao: FromDishka[SettingsDao],
    **kwargs: Any,
) -> dict[str, Any]:
    plan_id: int = dialog_manager.start_data["plan_id"]  # type: ignore[call-overload, index, assignment]
    plan = await plan_dao.get_by_id(plan_id)

    if not plan:
        raise ValueError(f"Plan with id '{plan_id}' not found")

    current_subscription = await subscription_dao.get_current(user.id)

    if current_subscription:
        matched_plan = await match_plan.system(
            MatchPlanDto(plan_snapshot=current_subscription.plan_snapshot, plans=[plan])
        )

        renewal_plan = resolve_renew_plan(
            current_subscription.plan_snapshot,
            [plan],
            matched_plan,
        ).plan

        if renewal_plan and not current_subscription.is_unlimited:
            purchase_type = PurchaseType.RENEW
        else:
            purchase_type = PurchaseType.CHANGE
    else:
        purchase_type = PurchaseType.NEW

    dialog_manager.dialog_data["only_single_plan"] = True
    dialog_manager.dialog_data["purchase_type"] = purchase_type

    return {
        "plan_id": [plan.id],
        "name": i18n.get_or_raw(plan.name),
        "description": i18n.get_or_raw(plan.description) if plan.description else False,
        "purchase_type": purchase_type,
        "change_mode": "ON" if (await settings_dao.get()).extra.change_credit else "OFF",
    }


@inject
async def plans_getter(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    i18n: FromDishka[TranslatorRunner],
    get_available_plans: FromDishka[GetAvailablePlans],
    **kwargs: Any,
) -> dict[str, Any]:
    plans = await get_available_plans.system(user)

    formatted_plans = [
        {
            "id": plan.id,
            "name": i18n.get_or_raw(plan.name),
        }
        for plan in plans
    ]

    return {
        "plans": formatted_plans,
    }


@inject
async def duration_getter(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    retort: FromDishka[Retort],
    i18n: FromDishka[TranslatorRunner],
    settings_dao: FromDishka[SettingsDao],
    pricing_service: FromDishka[PricingService],
    **kwargs: Any,
) -> dict[str, Any]:
    raw_plan = dialog_manager.dialog_data.get(PlanDto.__name__)

    if not raw_plan:
        raise UnknownIntent("PlanDto not found in subscription dialog data")

    plan = retort.load(raw_plan, PlanDto)
    settings = await settings_dao.get()
    currency = settings.default_currency
    only_single_plan = dialog_manager.dialog_data.get("only_single_plan", False)
    dialog_manager.dialog_data["is_free"] = False
    durations = []

    discounted = applies_discount(dialog_manager, plan)
    for duration in plan.durations:
        key, kw = i18n_format_days(duration.days)
        price = pricing_service.calculate(
            user, duration.get_price(currency), currency, apply_discount=discounted
        )
        durations.append(
            {
                "days": duration.days,
                "period": i18n.get(key, **kw),
                "final_amount": price.final_amount,
                "discount_percent": price.discount_percent,
                "original_amount": price.original_amount,
                "currency": currency.symbol,
            }
        )

    plan_is_modified = 1 if dialog_manager.dialog_data.get("plan_is_modified", False) else 0

    return {
        "plan": i18n.get_or_raw(plan.name),
        "description": i18n.get_or_raw(plan.description) if plan.description else False,
        "type": plan.type,
        "devices": i18n_format_device_limit(plan.device_limit),
        "traffic": i18n_format_traffic_limit(plan.traffic_limit),
        "durations": durations,
        "period": 0,
        "final_amount": 0,
        "currency": "",
        "only_single_plan": only_single_plan,
        "discount_percent": pricing_service.get_effective_discount(user) if discounted else 0,
        "is_personal_discount": (
            pricing_service.is_largest_discount_personal(user) if discounted else False
        ),
        "plan_is_modified": plan_is_modified,
        **gift_getter_data(dialog_manager),
    }


@inject
async def payment_method_getter(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    retort: FromDishka[Retort],
    i18n: FromDishka[TranslatorRunner],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    pricing_service: FromDishka[PricingService],
    **kwargs: Any,
) -> dict[str, Any]:
    raw_plan = dialog_manager.dialog_data.get(PlanDto.__name__)

    if not raw_plan:
        raise UnknownIntent("PlanDto not found in subscription dialog data")

    plan = retort.load(raw_plan, PlanDto)
    gateways = await payment_gateway_dao.get_active()
    selected_duration = dialog_manager.dialog_data["selected_duration"]
    only_single_duration = dialog_manager.dialog_data.get("only_single_duration", False)
    duration = plan.get_duration(selected_duration)

    if not duration:
        raise ValueError(f"Duration '{selected_duration}' not found in plan '{plan.name}'")

    discounted = applies_discount(dialog_manager, plan)
    payment_methods = []
    for gateway in gateways:
        raw_price = duration.get_price(gateway.currency)
        price = pricing_service.calculate(
            user, raw_price, gateway.currency, apply_discount=discounted
        )
        payment_methods.append(
            {
                "gateway_type": gateway.type,
                "gateway_title": _get_gateway_title(i18n, gateway),
                "final_amount": price.final_amount,
                "original_amount": price.original_amount,
                "discount_percent": price.discount_percent,
                "currency": gateway.currency.symbol,
            }
        )

    key, kw = i18n_format_days(duration.days)

    plan_is_modified = 1 if dialog_manager.dialog_data.get("plan_is_modified", False) else 0

    return {
        "plan": i18n.get_or_raw(plan.name),
        "description": i18n.get_or_raw(plan.description) if plan.description else False,
        "type": plan.type,
        "devices": i18n_format_device_limit(plan.device_limit),
        "traffic": i18n_format_traffic_limit(plan.traffic_limit),
        "period": i18n.get(key, **kw),
        "payment_methods": payment_methods,
        "final_amount": 0,
        "currency": "",
        "only_single_duration": only_single_duration,
        "discount_percent": pricing_service.get_effective_discount(user) if discounted else 0,
        "is_personal_discount": (
            pricing_service.is_largest_discount_personal(user) if discounted else False
        ),
        "plan_is_modified": plan_is_modified,
        **gift_getter_data(dialog_manager),
    }


@inject
async def confirm_getter(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    retort: FromDishka[Retort],
    i18n: FromDishka[TranslatorRunner],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    pricing_service: FromDishka[PricingService],
    subscription_dao: FromDishka[SubscriptionDao],
    plan_dao: FromDishka[PlanDao],
    settings_dao: FromDishka[SettingsDao],
    **kwargs: Any,
) -> dict[str, Any]:
    raw_plan = dialog_manager.dialog_data.get(PlanDto.__name__)

    if not raw_plan:
        raise UnknownIntent("PlanDto not found in subscription dialog data")

    plan = retort.load(raw_plan, PlanDto)
    selected_duration = dialog_manager.dialog_data["selected_duration"]
    only_single_duration = dialog_manager.dialog_data.get("only_single_duration", False)
    only_single_plan = dialog_manager.dialog_data.get("only_single_plan", False)
    is_free = dialog_manager.dialog_data.get("is_free", False)
    selected_payment_method = dialog_manager.dialog_data["selected_payment_method"]
    purchase_type = dialog_manager.dialog_data["purchase_type"]
    payment_gateway = await payment_gateway_dao.get_by_type(selected_payment_method)
    duration = plan.get_duration(selected_duration)

    if not duration:
        raise ValueError(f"Duration '{selected_duration}' not found in plan '{plan.name}'")

    if not payment_gateway:
        raise ValueError(f"Not found PaymentGateway by selected type '{selected_payment_method}'")

    result_url = dialog_manager.dialog_data["payment_url"]
    pricing_data = dialog_manager.dialog_data["final_pricing"]
    pricing = retort.load(pricing_data, PriceDetailsDto)

    key, kw = i18n_format_days(duration.days)
    gateways = await payment_gateway_dao.get_active()

    plan_is_modified = 1 if dialog_manager.dialog_data.get("plan_is_modified", False) else 0
    change_data = await _change_quote_data(
        purchase_type=purchase_type,
        user_id=user.id,
        plan_id=plan.id,
        days=duration.days,
        subscription_dao=subscription_dao,
        plan_dao=plan_dao,
        settings_dao=settings_dao,
    )

    return {
        **gift_getter_data(dialog_manager),
        "purchase_type": purchase_type,
        "plan": i18n.get_or_raw(plan.name),
        "description": i18n.get_or_raw(plan.description) if plan.description else False,
        "type": plan.type,
        "devices": i18n_format_device_limit(plan.device_limit),
        "traffic": i18n_format_traffic_limit(plan.traffic_limit),
        "period": i18n.get(key, **kw),
        "payment_method": selected_payment_method,
        "payment_method_title": _get_gateway_title(i18n, payment_gateway),
        "final_amount": pricing.final_amount,
        "discount_percent": pricing.discount_percent,
        "original_amount": pricing.original_amount,
        "is_personal_discount": pricing_service.is_largest_discount_personal(user),
        "currency": payment_gateway.currency.symbol,
        "url": result_url,
        "only_single_gateway": len(gateways) == 1,
        "only_single_duration": only_single_duration,
        "only_single_plan": only_single_plan,
        "is_free": is_free,
        "plan_is_modified": plan_is_modified,
        **change_data,
    }


@inject
async def getter_connect(
    dialog_manager: DialogManager,
    config: AppConfig,
    user: TelegramUserDto,
    subscription_dao: FromDishka[SubscriptionDao],
    settings_dao: FromDishka[SettingsDao],
    **kwargs: Any,
) -> dict[str, Any]:
    current_subscription = await subscription_dao.get_current(user.id)

    if not current_subscription:
        raise ValueError(f"User '{user.telegram_id}' has no active subscription after purchase")

    settings = await settings_dao.get()

    return {
        "is_mini_app": config.bot.is_mini_app,
        "is_mini_app_reserve": config.bot.is_mini_app and settings.extra.mini_app_reserve,
        "connection_url": config.bot.mini_app_url or current_subscription.url,
        "subscription_url": current_subscription.url,
        "connectable": True,
    }


@inject
async def success_payment_getter(
    dialog_manager: DialogManager,
    config: AppConfig,
    user: TelegramUserDto,
    subscription_dao: FromDishka[SubscriptionDao],
    settings_dao: FromDishka[SettingsDao],
    i18n: FromDishka[TranslatorRunner],
    **kwargs: Any,
) -> dict[str, Any]:
    start_data = cast(dict[str, Any], dialog_manager.start_data)
    purchase_type: PurchaseType = start_data["purchase_type"]
    subscription = await subscription_dao.get_current(user.id)

    if not subscription:
        raise ValueError(f"User '{user.telegram_id}' has no active subscription after purchase")

    settings = await settings_dao.get()

    return {
        "purchase_type": purchase_type,
        "plan_name": i18n.get_or_raw(subscription.plan_snapshot.name),
        "traffic_limit": i18n_format_traffic_limit(subscription.traffic_limit),
        "device_limit": i18n_format_device_limit(subscription.device_limit),
        "expire_time": i18n_format_expire_time(subscription.expire_at),
        "added_duration": i18n_format_days(subscription.plan_snapshot.duration),
        "is_mini_app": config.bot.is_mini_app,
        "is_mini_app_reserve": config.bot.is_mini_app and settings.extra.mini_app_reserve,
        "connection_url": config.bot.mini_app_url or subscription.url,
        "subscription_url": subscription.url,
        "connectable": True,
    }
