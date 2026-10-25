from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from aiohttp import ClientConnectorCertificateError, ClientConnectorError, ServerDisconnectedError
from python_socks import ProxyConnectionError

from src.application.dto import MessagePayloadDto, TempUserDto
from src.infrastructure.services import telegram_retry
from src.infrastructure.services.bot import BotService
from src.infrastructure.services.notification import NotificationService
from src.infrastructure.services.telegram_retry import (
    TELEGRAM_CONNECT_RETRY_DELAYS,
    call_with_connect_retry,
    is_telegram_unreachable_error,
)

_KEY = SimpleNamespace(host="api.telegram.org", port=443, ssl=True)


def _network_error(cause: BaseException | None) -> TelegramNetworkError:
    error = TelegramNetworkError(
        method=SendMessage(chat_id=1, text="x"),
        message="HTTP Client says - transport failure",
    )
    error.__cause__ = cause
    return error


def _connect_failure() -> TelegramNetworkError:
    # what aiogram raised in production: reset during the TLS handshake
    return _network_error(ClientConnectorError(_KEY, ConnectionResetError()))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_retry.asyncio, "sleep", sleep)
    return sleep


def test_only_broken_connections_count_as_unreachable() -> None:
    assert is_telegram_unreachable_error(_connect_failure())
    assert is_telegram_unreachable_error(_network_error(ProxyConnectionError("proxy down")))
    # a pooled keep-alive connection dropped by the router tunnel restart
    assert is_telegram_unreachable_error(_network_error(ServerDisconnectedError()))
    # the request may already have reached Telegram: never resend
    assert not is_telegram_unreachable_error(_network_error(TimeoutError()))
    assert not is_telegram_unreachable_error(_network_error(None))
    # not transient
    assert not is_telegram_unreachable_error(
        _network_error(ClientConnectorCertificateError(_KEY, ValueError("bad cert")))
    )
    assert not is_telegram_unreachable_error(
        TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked by the user")
    )
    assert not is_telegram_unreachable_error(
        TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "Too Many Requests", 5)
    )


def test_retry_window_outlasts_a_router_tunnel_restart() -> None:
    # PassWall2 leaves Telegram unreachable for 15-25 s when the router switches nodes
    assert sum(TELEGRAM_CONNECT_RETRY_DELAYS) >= 40
    assert telegram_retry.TELEGRAM_CONNECT_RETRY_BUDGET > sum(TELEGRAM_CONNECT_RETRY_DELAYS)


async def test_connect_failure_is_retried_until_delivery(sleeps: AsyncMock) -> None:
    send = AsyncMock(side_effect=[_connect_failure(), _connect_failure(), "message"])

    assert await call_with_connect_retry(send, "user") == "message"

    assert send.await_count == 3
    assert [call.args[0] for call in sleeps.await_args_list] == [1.0, 3.0]


async def test_retries_are_bounded_and_the_last_error_is_raised(sleeps: AsyncMock) -> None:
    attempts = len(TELEGRAM_CONNECT_RETRY_DELAYS) + 1
    send = AsyncMock(side_effect=[_connect_failure() for _ in range(attempts + 2)])

    with pytest.raises(TelegramNetworkError):
        await call_with_connect_retry(send, "user")

    assert send.await_count == attempts
    assert [call.args[0] for call in sleeps.await_args_list] == list(
        TELEGRAM_CONNECT_RETRY_DELAYS
    )


@pytest.mark.parametrize(
    "error",
    [
        _network_error(TimeoutError()),
        TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked by the user"),
        TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "Too Many Requests", 5),
        RuntimeError("bug"),
    ],
)
async def test_other_errors_are_not_retried(sleeps: AsyncMock, error: Exception) -> None:
    send = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        await call_with_connect_retry(send, "user")

    assert send.await_count == 1
    sleeps.assert_not_awaited()


async def test_retry_stops_when_the_time_budget_is_spent(
    sleeps: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telegram_retry, "TELEGRAM_CONNECT_RETRY_BUDGET", 0.5)
    send = AsyncMock(side_effect=[_connect_failure(), "message"])

    with pytest.raises(TelegramNetworkError):
        await call_with_connect_retry(send, "user")

    assert send.await_count == 1
    sleeps.assert_not_awaited()


async def test_notification_survives_a_short_telegram_outage(sleeps: AsyncMock) -> None:
    service = object.__new__(NotificationService)
    sent = SimpleNamespace(message_id=7)
    service.bot = SimpleNamespace(send_message=AsyncMock(side_effect=[_connect_failure(), sent]))
    service._prepare_reply_markup = lambda *_: None
    service._get_translated_text = lambda **_: "test"

    message = await service._send_message(
        TempUserDto(telegram_id=123, name="Test"),
        MessagePayloadDto(i18n_key="raw-message", delete_after=None),
    )

    assert message is sent
    assert service.bot.send_message.await_count == 2


async def test_channel_check_survives_a_short_telegram_outage(sleeps: AsyncMock) -> None:
    member = SimpleNamespace(status="member")
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=[_connect_failure(), member]))
    service = BotService(bot, SimpleNamespace())  # type: ignore[arg-type]

    assert await service.get_chat_member("@channel", 123) is member
    assert bot.get_chat_member.await_count == 2
