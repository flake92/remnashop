from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiohttp import ClientConnectorCertificateError, ClientConnectorError
from python_socks import ProxyConnectionError

from src.application.dto import MessagePayloadDto, TempUserDto
from src.infrastructure.services import notification
from src.infrastructure.services.notification import (
    NotificationService,
    _is_telegram_unreachable_error,
    _send_with_connect_retry,
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
    monkeypatch.setattr(notification.asyncio, "sleep", sleep)
    return sleep


def test_only_unestablished_connections_count_as_unreachable() -> None:
    assert _is_telegram_unreachable_error(_connect_failure())
    assert _is_telegram_unreachable_error(_network_error(ProxyConnectionError("proxy down")))
    # the request may already have reached Telegram: never resend
    assert not _is_telegram_unreachable_error(_network_error(TimeoutError()))
    assert not _is_telegram_unreachable_error(_network_error(None))
    # not transient
    assert not _is_telegram_unreachable_error(
        _network_error(ClientConnectorCertificateError(_KEY, ValueError("bad cert")))
    )
    assert not _is_telegram_unreachable_error(
        TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked by the user")
    )


async def test_connect_failure_is_retried_until_delivery(sleeps: AsyncMock) -> None:
    send = AsyncMock(side_effect=[_connect_failure(), _connect_failure(), "message"])

    assert await _send_with_connect_retry(send, "user") == "message"

    assert send.await_count == 3
    assert [call.args[0] for call in sleeps.await_args_list] == [1.0, 3.0]


async def test_retries_are_bounded_and_the_last_error_is_raised(sleeps: AsyncMock) -> None:
    send = AsyncMock(side_effect=[_connect_failure() for _ in range(5)])

    with pytest.raises(TelegramNetworkError):
        await _send_with_connect_retry(send, "user")

    assert send.await_count == 3
    assert sleeps.await_count == 2


@pytest.mark.parametrize(
    "error",
    [
        _network_error(TimeoutError()),
        TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked by the user"),
        RuntimeError("bug"),
    ],
)
async def test_other_errors_are_not_retried(sleeps: AsyncMock, error: Exception) -> None:
    send = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        await _send_with_connect_retry(send, "user")

    assert send.await_count == 1
    sleeps.assert_not_awaited()


async def test_retry_stops_when_the_time_budget_is_spent(
    sleeps: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notification, "TELEGRAM_CONNECT_RETRY_BUDGET", 0.5)
    send = AsyncMock(side_effect=[_connect_failure(), "message"])

    with pytest.raises(TelegramNetworkError):
        await _send_with_connect_retry(send, "user")

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
