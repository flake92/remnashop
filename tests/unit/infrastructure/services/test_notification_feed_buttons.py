from types import SimpleNamespace
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.infrastructure.services.notification import NotificationService


def _feed_buttons(keyboard: InlineKeyboardMarkup) -> list[list[dict[str, str]]]:
    def translate(markup: InlineKeyboardMarkup, _locale: Any) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [button.model_copy(update={"text": f"tr:{button.text}"}) for button in row]
                for row in markup.inline_keyboard
            ]
        )

    service = SimpleNamespace(_translate_keyboard_text=translate)
    return NotificationService._feed_buttons(service, keyboard, "ru")  # type: ignore[arg-type]


def test_support_button_opens_the_cabinet_page_instead_of_the_bot() -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="btn-goto.contact-support", url="https://t.me/support")]
        ]
    )

    assert _feed_buttons(keyboard) == [[{"text": "tr:btn-goto.contact-support", "url": "/support"}]]


def test_other_url_buttons_keep_their_address() -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="news", url="https://example.org/news")]]
    )

    assert _feed_buttons(keyboard) == [[{"text": "tr:news", "url": "https://example.org/news"}]]
