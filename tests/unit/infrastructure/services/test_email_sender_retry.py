import smtplib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.exceptions import EmailDeliveryError
from src.infrastructure.services.email_sender import SmtpEmailSender


async def _inline_to_thread(function, **kwargs):
    return function(**kwargs)


def _sender() -> SmtpEmailSender:
    return SmtpEmailSender(SimpleNamespace(email=SimpleNamespace()))


async def test_transient_smtp_authentication_failure_retries_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _sender()
    sender._send_sync = Mock(
        side_effect=[smtplib.SMTPAuthenticationError(454, b"temporary auth failure"), None]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )
    monkeypatch.setattr("src.infrastructure.services.email_sender.asyncio.sleep", sleep)

    await sender.send(to="user@example.com", subject="subject", body="body")

    assert sender._send_sync.call_count == 2
    sleep.assert_awaited_once()


async def test_persistent_smtp_authentication_failure_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _sender()
    sender._send_sync = Mock(
        side_effect=smtplib.SMTPAuthenticationError(535, b"invalid credentials")
    )
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(EmailDeliveryError):
        await sender.send(to="user@example.com", subject="subject", body="body")

    assert sender._send_sync.call_count == 2
