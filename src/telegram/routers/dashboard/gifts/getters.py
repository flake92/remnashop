from datetime import datetime
from typing import Any, Optional

from adaptix import Retort
from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common import TranslatorRunner
from src.application.common.policy import Permission, PermissionPolicy
from src.application.dto import PlanSnapshotDto, PromocodeDto, UserDto
from src.application.use_cases.promocode.queries.gifts import (
    GetGiftDetails,
    GetGiftList,
    GetGiftListDto,
)
from src.core.constants import TIMEZONE, USER_KEY
from src.core.utils.i18n_helpers import (
    i18n_format_days,
    i18n_format_device_limit,
    i18n_format_traffic_limit,
)
from src.core.utils.time import datetime_now

GIFT_PAGE_KEY = "gift_page"
GIFT_ID_KEY = "gift_id"
PAGE_SIZE = 10


def format_moment(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    return value.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")


def gift_status(promo: PromocodeDto, redeemed: bool) -> str:
    if redeemed:
        return "ACTIVATED"
    if not promo.is_active:
        return "INACTIVE"
    if promo.expires_at is not None and promo.expires_at <= datetime_now():
        return "EXPIRED"
    return "AVAILABLE"


def _party(prefix: str, user: Optional[UserDto]) -> dict[str, Any]:
    return {
        f"{prefix}_exists": 1 if user else 0,
        f"{prefix}_name": user.name if user else "—",
        f"{prefix}_telegram_id": (user.telegram_id or 0) if user else 0,
        f"{prefix}_username": (user.username or 0) if user else 0,
        f"{prefix}_email": (user.email or "—") if user else "—",
        f"{prefix}_user_id": user.id if user and user.id is not None else 0,
    }


@inject
async def getter_gifts_main(
    dialog_manager: DialogManager,
    get_gift_list: FromDishka[GetGiftList],
    **kwargs: Any,
) -> dict[str, Any]:
    user = dialog_manager.middleware_data[USER_KEY]
    page = dialog_manager.dialog_data.get(GIFT_PAGE_KEY, 0)
    result = await get_gift_list(user, GetGiftListDto(limit=PAGE_SIZE, offset=page * PAGE_SIZE))
    return {
        "gifts": [
            {
                "id": gift.id,
                "code": gift.code,
                "status": gift_status(gift, gift.id in result.redeemed_ids),
                "created_at": format_moment(gift.created_at),
            }
            for gift in result.gifts
        ],
        "total": result.total,
        "has_next": (page + 1) * PAGE_SIZE < result.total,
        "has_prev": page > 0,
    }


@inject
async def getter_gift_detail(
    dialog_manager: DialogManager,
    get_gift_details: FromDishka[GetGiftDetails],
    retort: FromDishka[Retort],
    i18n: FromDishka[TranslatorRunner],
    **kwargs: Any,
) -> dict[str, Any]:
    user = dialog_manager.middleware_data[USER_KEY]
    gift_id = int(dialog_manager.dialog_data[GIFT_ID_KEY])
    details = await get_gift_details(user, gift_id)
    if details is None:
        return {"found": 0, "can_manage": False, "code": "—", "anonymous": 0}

    promo = details.promo
    plan = retort.load(promo.plan_snapshot, PlanSnapshotDto) if promo.plan_snapshot else None
    transaction = details.transaction
    duration_key, duration_kw = i18n_format_days(plan.duration if plan else 0)
    traffic_key, traffic_kw = i18n_format_traffic_limit(plan.traffic_limit if plan else 0)
    devices_key, devices_kw = i18n_format_device_limit(plan.device_limit if plan else 0)

    return {
        "found": 1,
        "can_manage": PermissionPolicy.has_permission(user, Permission.MANAGE_PROMOCODE),
        "code": promo.code,
        "status": gift_status(promo, details.activated_at is not None),
        "is_active": 1 if promo.is_active else 0,
        "anonymous": 1 if promo.gift_anonymous else 0,
        "created_at": format_moment(promo.created_at),
        "expires_at": format_moment(promo.expires_at),
        "activated_at": format_moment(details.activated_at),
        "plan_name": i18n.get_or_raw(plan.name) if plan else "—",
        "plan_duration": i18n.get(duration_key, **duration_kw),
        "plan_traffic": i18n.get(traffic_key, **traffic_kw),
        "plan_devices": i18n.get(devices_key, **devices_kw),
        "payment_id": str(promo.gift_payment_id or "—"),
        "payment_amount": str(transaction.pricing.final_amount) if transaction else "—",
        "payment_currency": transaction.currency.symbol if transaction else "",
        "payment_gateway": transaction.gateway_type.value if transaction else "—",
        "payment_status": transaction.status.value if transaction else "—",
        "payment_created_at": format_moment(transaction.created_at if transaction else None),
        **_party("buyer", details.buyer),
        **_party("recipient", details.recipient),
    }
