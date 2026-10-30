from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import User as AiogramUser
from aiogram_dialog.api.exceptions import UnknownIntent

from src.application.common import BotService, EventPublisher, Notifier
from src.application.common.dao import UserDao
from src.application.use_cases.misc.commands.navigation import RedirectMenu
from src.core.constants import CONFIG_KEY, CONTAINER_KEY
from src.telegram.middlewares.error import ErrorMiddleware


async def test_stale_dialog_context_is_recovered_without_rethrowing() -> None:
    bot_service = SimpleNamespace(get_support_url=lambda: "https://example.com/support")
    event_publisher = SimpleNamespace(publish=AsyncMock())
    notifier = SimpleNamespace(notify_user=AsyncMock())
    redirect_menu = SimpleNamespace(system=AsyncMock())
    user_dao = SimpleNamespace(
        get_by_telegram_id=AsyncMock(return_value=SimpleNamespace(is_privileged=False))
    )
    dependencies = {
        BotService: bot_service,
        EventPublisher: event_publisher,
        Notifier: notifier,
        RedirectMenu: redirect_menu,
        UserDao: user_dao,
    }
    container = SimpleNamespace(
        get=AsyncMock(side_effect=lambda dependency: dependencies[dependency])
    )
    aiogram_user = AiogramUser(id=123, is_bot=False, first_name="Test")
    event = SimpleNamespace(
        exception=UnknownIntent("Context not found for intent id: stale"),
        update=SimpleNamespace(message=None),
    )
    handler = AsyncMock()

    result = await ErrorMiddleware().middleware_logic(
        handler,
        event,
        {
            CONFIG_KEY: SimpleNamespace(build=SimpleNamespace(data={})),
            CONTAINER_KEY: container,
            "event_from_user": aiogram_user,
        },
    )

    assert result is None
    handler.assert_not_awaited()
    redirect_menu.system.assert_awaited_once_with(aiogram_user.id)
    notifier.notify_user.assert_awaited_once()
    event_publisher.publish.assert_not_awaited()


async def test_telegram_outage_is_logged_without_replies_or_admin_alert() -> None:
    event_publisher = SimpleNamespace(publish=AsyncMock())
    notifier = SimpleNamespace(notify_user=AsyncMock())
    redirect_menu = SimpleNamespace(system=AsyncMock())
    dependencies = {
        BotService: SimpleNamespace(get_support_url=lambda: "https://example.com/support"),
        EventPublisher: event_publisher,
        Notifier: notifier,
        RedirectMenu: redirect_menu,
        UserDao: SimpleNamespace(get_by_telegram_id=AsyncMock()),
    }
    container = SimpleNamespace(
        get=AsyncMock(side_effect=lambda dependency: dependencies[dependency])
    )
    event = SimpleNamespace(
        exception=TelegramNetworkError(
            method=GetChatMember(chat_id=1, user_id=123),
            message="ClientConnectorError: Cannot connect to host api.telegram.org:443",
        ),
        update=SimpleNamespace(message=None),
    )

    result = await ErrorMiddleware().middleware_logic(
        AsyncMock(),
        event,
        {
            CONFIG_KEY: SimpleNamespace(build=SimpleNamespace(data={})),
            CONTAINER_KEY: container,
            "event_from_user": AiogramUser(id=123, is_bot=False, first_name="Test"),
        },
    )

    assert result is None
    redirect_menu.system.assert_not_awaited()
    notifier.notify_user.assert_not_awaited()
    event_publisher.publish.assert_not_awaited()


async def test_unreachable_chat_ends_without_redirect_loop() -> None:
    # Blocking a user whose chat is gone redirected them to the menu, which
    # failed with the same error and looped until the DB pool was exhausted.
    event_publisher = SimpleNamespace(publish=AsyncMock())
    notifier = SimpleNamespace(notify_user=AsyncMock())
    redirect_menu = SimpleNamespace(system=AsyncMock())
    dependencies = {
        BotService: SimpleNamespace(get_support_url=lambda: "https://example.com/support"),
        EventPublisher: event_publisher,
        Notifier: notifier,
        RedirectMenu: redirect_menu,
        UserDao: SimpleNamespace(get_by_telegram_id=AsyncMock()),
    }
    container = SimpleNamespace(
        get=AsyncMock(side_effect=lambda dependency: dependencies[dependency])
    )
    event = SimpleNamespace(
        exception=TelegramBadRequest(
            method=GetChatMember(chat_id=1, user_id=123),
            message="Bad Request: chat not found",
        ),
        update=SimpleNamespace(message=None),
    )

    result = await ErrorMiddleware().middleware_logic(
        AsyncMock(),
        event,
        {
            CONFIG_KEY: SimpleNamespace(build=SimpleNamespace(data={})),
            CONTAINER_KEY: container,
            "event_from_user": AiogramUser(id=123, is_bot=False, first_name="Test"),
        },
    )

    assert result is None
    redirect_menu.system.assert_not_awaited()
    notifier.notify_user.assert_not_awaited()
    event_publisher.publish.assert_not_awaited()
