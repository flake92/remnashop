from adaptix import Retort
from aiogram.types import CallbackQuery
from aiogram_dialog import DialogManager, StartMode
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier
from src.application.use_cases.promocode.commands.manage import DeletePromocode
from src.application.use_cases.promocode.queries.get import GetPromocode
from src.core.constants import USER_KEY
from src.telegram.routers.dashboard.promocodes.handlers import (
    EDIT_PROMO_KEY,
    RETURN_TO_GIFTS_KEY,
)
from src.telegram.states import DashboardGifts, DashboardPromocodes
from src.telegram.utils import is_double_click

from .getters import GIFT_ID_KEY, GIFT_PAGE_KEY


async def on_gift_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    dialog_manager.dialog_data[GIFT_ID_KEY] = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    await dialog_manager.switch_to(DashboardGifts.DETAIL)


async def on_page_next(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    dialog_manager.dialog_data[GIFT_PAGE_KEY] = dialog_manager.dialog_data.get(GIFT_PAGE_KEY, 0) + 1


async def on_page_prev(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    page = dialog_manager.dialog_data.get(GIFT_PAGE_KEY, 0)
    dialog_manager.dialog_data[GIFT_PAGE_KEY] = max(page - 1, 0)


@inject
async def on_gift_edit(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    get_promocode: FromDishka[GetPromocode],
    retort: FromDishka[Retort],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    promo = await get_promocode(user, int(dialog_manager.dialog_data[GIFT_ID_KEY]))
    if promo is None:
        return
    await dialog_manager.start(
        DashboardPromocodes.CONFIGURATOR,
        data={EDIT_PROMO_KEY: retort.dump(promo), RETURN_TO_GIFTS_KEY: True},
        mode=StartMode.RESET_STACK,
    )


@inject
async def on_gift_delete(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    delete_promocode: FromDishka[DeletePromocode],
    notifier: FromDishka[Notifier],
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    gift_id = int(dialog_manager.dialog_data[GIFT_ID_KEY])

    if not is_double_click(dialog_manager, key=f"gift_delete_{gift_id}", cooldown=10):
        await notifier.notify_user(user, i18n_key="ntf-common.double-click-confirm")
        return

    await delete_promocode(user, gift_id)
    logger.info(f"{user.log} Deleted gift code id='{gift_id}'")
    dialog_manager.dialog_data.pop(GIFT_ID_KEY, None)
    await notifier.notify_user(user, i18n_key="ntf-gift.deleted")
    await dialog_manager.switch_to(DashboardGifts.MAIN)
