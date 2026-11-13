from aiogram.enums import ButtonStyle
from aiogram_dialog import Dialog, StartMode, Window
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Format
from magic_filter import F

from src.core.enums import BannerName
from src.telegram.keyboards import main_menu_button
from src.telegram.states import Dashboard, DashboardGifts
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import Button, CopyText, ListGroup, Row, Start, SwitchTo

from .getters import getter_gift_detail, getter_gifts_main
from .handlers import (
    on_gift_delete,
    on_gift_edit,
    on_gift_select,
    on_gift_toggle_anonymous,
    on_page_next,
    on_page_prev,
)

gifts_main = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-gifts-main"),
    ListGroup(
        Row(
            Button(
                text=I18nFormat(
                    "btn-gifts.item",
                    code=F["item"]["code"],
                    status=F["item"]["status"],
                    created_at=F["item"]["created_at"],
                ),
                id="gift_item",
                on_click=on_gift_select,
            ),
        ),
        id="gifts_list",
        item_id_getter=lambda item: item["id"],
        items="gifts",
    ),
    Row(
        Button(
            text=I18nFormat("btn-common.prev"),
            id="page_prev",
            on_click=on_page_prev,
            when=F["has_prev"],
        ),
        Button(
            text=I18nFormat("btn-common.next"),
            id="page_next",
            on_click=on_page_next,
            when=F["has_next"],
        ),
    ),
    Row(
        Start(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=Dashboard.MAIN,
            mode=StartMode.RESET_STACK,
        ),
        *main_menu_button,
    ),
    IgnoreUpdate(),
    state=DashboardGifts.MAIN,
    getter=getter_gifts_main,
)

gift_detail = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-gift-details", F["found"]),
    I18nFormat("msg-gift-not-found", ~F["found"]),
    Row(
        CopyText(
            text=I18nFormat("btn-gifts.copy-code"),
            copy_text=Format("{code}"),
            when=F["found"],
        ),
    ),
    Row(
        Button(
            text=I18nFormat("btn-gifts.edit"),
            id="gift_edit",
            on_click=on_gift_edit,
            when=F["found"] & F["can_manage"],
        ),
        Button(
            text=I18nFormat("btn-gifts.anonymous-toggle", anonymous=F["anonymous"]),
            id="gift_anonymous",
            on_click=on_gift_toggle_anonymous,
            when=F["found"] & F["can_manage"],
        ),
    ),
    Row(
        Button(
            text=I18nFormat("btn-gifts.delete"),
            id="gift_delete",
            on_click=on_gift_delete,
            style=Style(ButtonStyle.DANGER),
            when=F["found"] & F["can_manage"],
        ),
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back_list",
            state=DashboardGifts.MAIN,
        ),
        *main_menu_button,
    ),
    IgnoreUpdate(),
    state=DashboardGifts.DETAIL,
    getter=getter_gift_detail,
)

router = Dialog(gifts_main, gift_detail)
