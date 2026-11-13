from typing import Any, cast

from adaptix import Retort
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import EventPublisher, Notifier
from src.application.common.dao import PlanDao, SubscriptionDao
from src.application.dto import MessagePayloadDto, TelegramUserDto
from src.application.events import ErrorEvent
from src.application.use_cases.promocode.commands.activate import (
    ActivatePromocode,
    ActivatePromocodeDto,
)
from src.application.use_cases.promocode.queries.gift_outcome import (
    GiftOutcome,
    resolve_gift_outcome,
)
from src.application.use_cases.promocode.queries.validate import (
    ValidatePromocode,
    ValidatePromocodeDto,
)
from src.core.config import AppConfig
from src.core.constants import USER_KEY
from src.core.enums import PromocodeRewardType
from src.core.exceptions import (
    PromocodeAlreadyActivatedError,
    PromocodeExpiredError,
    PromocodeNotAvailableError,
    PromocodeNotFoundError,
)
from src.telegram.states import MainMenu
from src.telegram.utils import is_double_click

PENDING_PROMO_KEY = "pending_promo_code"
PENDING_PROMO_DTO_KEY = "pending_promo_dto"
PENDING_PROMO_REPLACE_KEY = "pending_promo_replace"
PENDING_GIFT_KEY = "pending_gift_outcome"


def _date(value: Any) -> str:
    return value.strftime("%d.%m.%Y") if value is not None else ""


def gift_outcome_data(outcome: GiftOutcome) -> dict[str, Any]:
    """The confirm-screen view of a gift: what the recipient's subscription becomes."""
    return {
        "gift_kind": outcome.kind.value,
        "gift_plan": outcome.plan_name,
        "gift_days": outcome.gift_days,
        "gift_expire": "" if outcome.is_unlimited else _date(outcome.expire_at),
        "gift_current_plan": outcome.current_plan_name or "",
        "gift_left_days": outcome.remaining_days,
        "gift_credit_days": outcome.credit_days,
    }


@inject
async def on_promocode_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    validate_promocode: FromDishka[ValidatePromocode],
    subscription_dao: FromDishka[SubscriptionDao],
    notifier: FromDishka[Notifier],
    plan_dao: FromDishka[PlanDao],
    retort: FromDishka[Retort],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    code = (message.text or "").strip().upper()

    if not code:
        return

    try:
        promo = await validate_promocode(user, ValidatePromocodeDto(code=code, user=user))
    except PromocodeNotFoundError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.not-found")
        return
    except PromocodeAlreadyActivatedError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.already-activated")
        return
    except PromocodeExpiredError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.expired")
        return
    except PromocodeNotAvailableError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.not-available")
        return

    will_replace = False
    dialog_manager.dialog_data.pop(PENDING_GIFT_KEY, None)
    if promo.is_gift:
        # A gift never replaces the subscription blindly: show exactly what it becomes.
        current = await subscription_dao.get_current(user.id)
        outcome = await resolve_gift_outcome(
            promo=promo, subscription=current, plan_dao=plan_dao, retort=retort
        )
        dialog_manager.dialog_data[PENDING_GIFT_KEY] = gift_outcome_data(outcome)
    elif promo.reward_type == PromocodeRewardType.SUBSCRIPTION:
        current = await subscription_dao.get_current(user.id)
        will_replace = current is not None

    logger.info(f"{user.log} Promocode validated, pending confirmation")

    dialog_manager.dialog_data[PENDING_PROMO_KEY] = promo.code
    dialog_manager.dialog_data[PENDING_PROMO_DTO_KEY] = {
        "code": promo.code,
        "reward_type": promo.reward_type.value,
        "reward": promo.reward,
    }
    dialog_manager.dialog_data[PENDING_PROMO_REPLACE_KEY] = will_replace


@inject
async def on_promocode_confirm(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    activate_promocode: FromDishka[ActivatePromocode],
    notifier: FromDishka[Notifier],
    event_publisher: FromDishka[EventPublisher],
    config: FromDishka[AppConfig],
    subscription_dao: FromDishka[SubscriptionDao],
) -> None:
    if is_double_click(dialog_manager, key="promo_confirm"):
        return

    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    code = dialog_manager.dialog_data.get(PENDING_PROMO_KEY)

    if not code:
        return

    try:
        promo = await activate_promocode(user, ActivatePromocodeDto(code=code, user=user))
    except PromocodeAlreadyActivatedError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.already-activated")
        return
    except PromocodeExpiredError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.expired")
        return
    except PromocodeNotFoundError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.not-found")
        return
    except PromocodeNotAvailableError:
        await notifier.notify_user(user, i18n_key="ntf-promocode.not-available")
        return
    except Exception as exc:
        logger.exception(f"{user.log} Promocode activation failed unexpectedly")
        await notifier.notify_user(user, i18n_key="ntf-promocode.activation-failed")
        await event_publisher.publish(
            ErrorEvent(
                **config.build.data,
                telegram_id=user.telegram_id,
                username=user.username,
                name=user.name,
                exception=exc,
            )
        )
        return

    logger.info(f"{user.log} Activated promocode")
    if promo.is_gift:
        current = await subscription_dao.get_current(user.id)
        expire = current.expire_at if current else None
        await notifier.notify_user(
            user,
            payload=MessagePayloadDto(
                i18n_key="ntf-promocode.gift-activated",
                i18n_kwargs={
                    "expire": 0
                    if expire is None or expire.year >= 2099
                    else expire.strftime("%d.%m.%Y"),
                },
            ),
        )
    else:
        await notifier.notify_user(user, i18n_key="ntf-promocode.activated")
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK)


async def getter_promocode(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if dialog_manager.start_data and not dialog_manager.dialog_data.get(PENDING_PROMO_KEY):
        start_data = cast(dict[str, Any], dialog_manager.start_data)
        prefill_dto = start_data.get("prefill_dto")
        if prefill_dto:
            dialog_manager.dialog_data[PENDING_PROMO_KEY] = prefill_dto["code"]
            dialog_manager.dialog_data[PENDING_PROMO_DTO_KEY] = prefill_dto
            dialog_manager.dialog_data[PENDING_PROMO_REPLACE_KEY] = start_data.get(
                "prefill_replace", False
            )

    promo_data: dict[str, Any] = cast(
        dict[str, Any], dialog_manager.dialog_data.get(PENDING_PROMO_DTO_KEY, {})
    )
    reward_type = promo_data.get("reward_type", "")
    gift: dict[str, Any] = cast(
        dict[str, Any], dialog_manager.dialog_data.get(PENDING_GIFT_KEY) or {}
    )
    return {
        "is_gift": bool(gift),
        "gift_kind": "",
        "gift_plan": "",
        "gift_days": 0,
        "gift_expire": "",
        "gift_current_plan": "",
        "gift_left_days": 0,
        "gift_credit_days": 0,
        **gift,
        "has_promo": bool(promo_data),
        "promo_code": promo_data.get("code", dialog_manager.dialog_data.get(PENDING_PROMO_KEY, "")),
        "promo_reward_type": reward_type,
        "promo_reward": promo_data.get("reward") or 0,
        "show_reset_warning": reward_type
        in {PromocodeRewardType.TRAFFIC.value, PromocodeRewardType.DEVICES.value},
        "will_replace_subscription": bool(
            dialog_manager.dialog_data.get(PENDING_PROMO_REPLACE_KEY)
        ),
    }
