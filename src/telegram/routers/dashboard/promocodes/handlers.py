import re
from datetime import datetime, timedelta
from typing import Optional

from adaptix import Retort
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier
from src.application.dto import PlanSnapshotDto, PromocodeDto
from src.application.use_cases.promocode.commands.manage import (
    CreatePromocode,
    CreatePromocodeDto,
    DeletePromocode,
    UpdatePromocode,
)
from src.application.use_cases.promocode.queries.generate import GeneratePromocodeCode
from src.application.use_cases.promocode.queries.get import GetPromocode
from src.application.use_cases.user.queries.plans import GetAvailablePlans
from src.core.constants import TIMEZONE, USER_KEY
from src.core.enums import PromocodeAvailability, PromocodeRewardType
from src.core.utils.time import datetime_now
from src.telegram.states import DashboardPromocodes
from src.telegram.utils import is_double_click

PROMO_PLAN_ID_KEY = "promo_plan_id"

_DISCOUNT_TYPES = {
    PromocodeRewardType.PERSONAL_DISCOUNT,
    PromocodeRewardType.PURCHASE_DISCOUNT,
}


# Codes are rendered inside HTML messages and shared via `promo_<code>` deeplinks, whose
# start parameter only allows A-Za-z0-9_- (Telegram Bot API). Restricting input to this set
# prevents both HTML-parse crashes (e.g. "I<3VPN") and broken deeplinks.
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_safe_promo_code(code: str) -> bool:
    return bool(_SAFE_CODE_RE.match(code))


def is_promo_complete(promo: PromocodeDto) -> bool:
    if not promo.code:
        return False
    if promo.reward_type == PromocodeRewardType.SUBSCRIPTION:
        return promo.plan_snapshot is not None
    return promo.reward is not None


PROMO_PAGE_KEY = "promo_page"
PAGE_SIZE = 10

_PROMO_KEY = PromocodeDto.__name__


def _load(dialog_manager: DialogManager, retort: Retort) -> PromocodeDto:
    return retort.load(dialog_manager.dialog_data[_PROMO_KEY], PromocodeDto)


def _save(dialog_manager: DialogManager, retort: Retort, promo: PromocodeDto) -> None:
    dialog_manager.dialog_data[_PROMO_KEY] = retort.dump(promo)


@inject
async def on_promo_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    get_promocode: FromDishka[GetPromocode],
    retort: FromDishka[Retort],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    promo_id = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    promo = await get_promocode(user, promo_id)
    if not promo:
        return
    dialog_manager.dialog_data[_PROMO_KEY] = retort.dump(promo)
    dialog_manager.dialog_data["is_edit"] = True
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_create_promo(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    generate_code: FromDishka[GeneratePromocodeCode],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    promo = PromocodeDto(
        code=await generate_code(user),
        is_active=True,
        reward_type=PromocodeRewardType.DURATION,
        reward=None,  # reward unset until the admin enters a value
        availability=PromocodeAvailability.ALL,
    )
    _save(dialog_manager, retort, promo)
    dialog_manager.dialog_data["is_edit"] = False
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_promo_confirm(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
    create_promocode: FromDishka[CreatePromocode],
    update_promocode: FromDishka[UpdatePromocode],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    promo = _load(dialog_manager, retort)
    is_edit: bool = dialog_manager.dialog_data.get("is_edit", False)

    if not is_promo_complete(promo):
        await notifier.notify_user(user, i18n_key="ntf-promocode.fields-required")
        return

    if not is_double_click(dialog_manager, key=f"promo_confirm_{promo.id}", cooldown=10):
        await notifier.notify_user(user, i18n_key="ntf-common.double-click-confirm")
        return

    if not is_edit:
        try:
            await create_promocode(
                user,
                CreatePromocodeDto(
                    code=promo.code,
                    reward_type=promo.reward_type,
                    reward=promo.reward,
                    plan_snapshot=promo.plan_snapshot,
                    availability=promo.availability,
                    expires_at=promo.expires_at,
                    max_activations=promo.max_activations,
                ),
            )
        except ValueError:
            await notifier.notify_user(user, i18n_key="ntf-promocode.code-exists")
            return
        logger.info(f"{user.log} Created promocode")
        await notifier.notify_user(user, i18n_key="ntf-promocode.created")
    else:
        await update_promocode(user, promo)
        logger.info(f"{user.log} Updated promocode id='{promo.id}'")
        await notifier.notify_user(user, i18n_key="ntf-promocode.updated")

    await dialog_manager.start(state=DashboardPromocodes.MAIN, mode=StartMode.RESET_STACK)


@inject
async def on_toggle_active(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
) -> None:
    promo = _load(dialog_manager, retort)
    promo.is_active = not promo.is_active
    _save(dialog_manager, retort, promo)


@inject
async def on_toggle_reusable(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
) -> None:
    promo = _load(dialog_manager, retort)
    promo.is_reusable = not promo.is_reusable
    _save(dialog_manager, retort, promo)


@inject
async def on_delete_promo(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    delete_promocode: FromDishka[DeletePromocode],
    notifier: FromDishka[Notifier],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    promo = _load(dialog_manager, retort)

    if is_double_click(dialog_manager, key=f"promo_delete_{promo.id}", cooldown=10):
        await delete_promocode(user, promo.id)
        dialog_manager.dialog_data.pop(_PROMO_KEY, None)
        await notifier.notify_user(user, i18n_key="ntf-promocode.deleted")
        await dialog_manager.start(state=DashboardPromocodes.MAIN, mode=StartMode.RESET_STACK)
        return

    await notifier.notify_user(user, i18n_key="ntf-common.double-click-confirm")


@inject
async def on_code_regenerate(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    generate_code: FromDishka[GeneratePromocodeCode],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    promo = _load(dialog_manager, retort)
    promo.code = await generate_code(user)
    _save(dialog_manager, retort, promo)


@inject
async def on_code_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user = dialog_manager.middleware_data[USER_KEY]
    code = (message.text or "").strip()
    if not (3 <= len(code) <= 16):
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    if not is_safe_promo_code(code):
        await notifier.notify_user(user, i18n_key="ntf-promocode.invalid-code")
        return
    promo = _load(dialog_manager, retort)
    promo.code = code
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_type_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
) -> None:
    reward_type = PromocodeRewardType(dialog_manager.item_id)  # type: ignore[attr-defined]
    promo = _load(dialog_manager, retort)
    promo.reward_type = reward_type
    if reward_type == PromocodeRewardType.SUBSCRIPTION:
        promo.reward = None
    else:
        # Reward is type-specific (days / devices / GB / %), so reset on type change.
        promo.plan_snapshot = None
        promo.reward = None
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_reward_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user = dialog_manager.middleware_data[USER_KEY]
    reward_text = (message.text or "").strip()
    if not reward_text.isdigit():
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    promo = _load(dialog_manager, retort)
    reward = int(reward_text)
    if promo.reward_type in _DISCOUNT_TYPES and not 1 <= reward <= 100:
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    promo.reward = reward
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_plan_open(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    get_available_plans: FromDishka[GetAvailablePlans],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    plans = await get_available_plans.system(user)
    if not plans:
        await notifier.notify_user(user, i18n_key="ntf-promocode.plans-empty")
        return
    await dialog_manager.switch_to(DashboardPromocodes.PLAN)


@inject
async def on_plan_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    plan_id = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    dialog_manager.dialog_data[PROMO_PLAN_ID_KEY] = plan_id
    await dialog_manager.switch_to(DashboardPromocodes.PLAN_DURATION)


@inject
async def on_plan_duration_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    get_available_plans: FromDishka[GetAvailablePlans],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    plan_id = dialog_manager.dialog_data.get(PROMO_PLAN_ID_KEY)
    days = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    plans = await get_available_plans.system(user)
    plan = next((p for p in plans if p.id == plan_id), None)
    if plan is None or plan.get_duration(days) is None:
        return
    snapshot = PlanSnapshotDto.from_plan(plan, days)
    promo = _load(dialog_manager, retort)
    promo.plan_snapshot = retort.dump(snapshot)
    promo.reward = None
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_availability_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
) -> None:
    availability = PromocodeAvailability(dialog_manager.item_id)  # type: ignore[attr-defined]
    promo = _load(dialog_manager, retort)
    promo.availability = availability
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


def _parse_expires_at(text: str, created_at: Optional[datetime]) -> Optional[datetime]:
    if text.isdigit():
        return (created_at or datetime_now()) + timedelta(days=int(text))
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            moment = datetime.strptime(text, fmt)  # noqa: DTZ007 (tz applied below)
        except ValueError:
            continue
        if fmt == "%d.%m.%Y":
            moment = moment.replace(hour=23, minute=59, second=59)
        return moment.replace(tzinfo=TIMEZONE)
    return None


@inject
async def on_expires_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user = dialog_manager.middleware_data[USER_KEY]
    promo = _load(dialog_manager, retort)
    expires = _parse_expires_at((message.text or "").strip(), promo.created_at)
    if expires is None or expires <= datetime_now():
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    promo.expires_at = expires
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_expires_reset(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
) -> None:
    promo = _load(dialog_manager, retort)
    promo.expires_at = None
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_max_activations_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
    notifier: FromDishka[Notifier],
) -> None:
    dialog_manager.show_mode = ShowMode.EDIT
    user = dialog_manager.middleware_data[USER_KEY]
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await notifier.notify_user(user, i18n_key="ntf-common.invalid-value")
        return
    promo = _load(dialog_manager, retort)
    promo.max_activations = int(text)
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_max_activations_reset(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    retort: FromDishka[Retort],
) -> None:
    promo = _load(dialog_manager, retort)
    promo.max_activations = None
    _save(dialog_manager, retort, promo)
    await dialog_manager.switch_to(DashboardPromocodes.CONFIGURATOR)


@inject
async def on_page_next(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    page = dialog_manager.dialog_data.get(PROMO_PAGE_KEY, 0)
    dialog_manager.dialog_data[PROMO_PAGE_KEY] = page + 1


@inject
async def on_page_prev(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    page = dialog_manager.dialog_data.get(PROMO_PAGE_KEY, 0)
    dialog_manager.dialog_data[PROMO_PAGE_KEY] = max(0, page - 1)
