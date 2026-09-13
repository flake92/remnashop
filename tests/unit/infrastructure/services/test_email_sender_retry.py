import asyncio
import smtplib
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr, ValidationError

from src.core.config.email import EmailConfig
from src.core.exceptions import EmailDeliveryError, EmailDeliveryRateDeferredError
from src.infrastructure.services.email_sender import SmtpEmailSender


async def _inline_to_thread(function, **kwargs):
    return function(**kwargs)


def _rate_limiter(*, available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        wait_for_send_slot=AsyncMock(return_value=available),
    )


def _sender() -> SmtpEmailSender:
    return SmtpEmailSender(
        SimpleNamespace(email=SimpleNamespace()),  # type: ignore[arg-type]
        _rate_limiter(),  # type: ignore[arg-type]
    )


def _smtp_config(
    *,
    port: int,
    use_ssl: bool,
    use_tls: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        email=SimpleNamespace(
            host="smtp.example.org",
            port=port,
            use_ssl=use_ssl,
            use_tls=use_tls,
            from_name="Clean Pay",
            from_email="notice@example.org",
            username=SecretStr("smtp-user"),
            password=SecretStr("smtp-password"),
        )
    )


def test_email_config_rejects_ambiguous_or_implicit_plaintext_smtp() -> None:
    with pytest.raises(ValidationError, match="cannot both be true"):
        EmailConfig(enabled=True, use_tls=True, use_ssl=True)

    with pytest.raises(ValidationError, match="EMAIL_ALLOW_INSECURE_SMTP"):
        EmailConfig(enabled=True, use_tls=False, use_ssl=False)


def test_email_config_plaintext_requires_explicit_dev_only_override() -> None:
    default_config = EmailConfig()
    assert default_config.allow_insecure_smtp is False

    local_sink = EmailConfig(
        enabled=True,
        use_tls=False,
        use_ssl=False,
        allow_insecure_smtp=True,
    )
    assert local_sink.allow_insecure_smtp is True


async def test_transient_smtp_authentication_failure_retries_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = _rate_limiter()
    sender = SmtpEmailSender(
        SimpleNamespace(email=SimpleNamespace()),  # type: ignore[arg-type]
        limiter,  # type: ignore[arg-type]
    )
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
    assert limiter.wait_for_send_slot.await_count == 2
    sleep.assert_awaited_once()


def test_smtp_sender_preserves_stable_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []

    class FakeSmtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeSmtp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def ehlo(self) -> None:
            return None

        def starttls(self, *, context: ssl.SSLContext) -> None:
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
            return None

        def login(self, username: str, password: str) -> None:
            assert (username, password) == ("smtp-user", "smtp-password")

        def send_message(self, message: object) -> None:
            sent.append(message)

    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.smtplib.SMTP",
        FakeSmtp,
    )
    config = SimpleNamespace(
        email=SimpleNamespace(
            host="smtp.example.org",
            port=587,
            use_ssl=False,
            use_tls=True,
            from_name="Clean Pay",
            from_email="notice@example.org",
            username=SecretStr("smtp-user"),
            password=SecretStr("smtp-password"),
        )
    )
    sender = SmtpEmailSender(
        config,  # type: ignore[arg-type]
        _rate_limiter(),  # type: ignore[arg-type]
    )

    sender._send_sync(
        to="user@example.org",
        subject="subject",
        body="body",
        message_id="<subscription-expiration-42@example.org>",
    )

    assert len(sent) == 1
    message = sent[0]
    assert message["Message-ID"] == "<subscription-expiration-42@example.org>"  # type: ignore[index]


def test_implicit_tls_uses_default_verified_ssl_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[ssl.SSLContext] = []
    constructor_contexts: list[ssl.SSLContext] = []
    real_create_default_context = ssl.create_default_context

    def create_default_context() -> ssl.SSLContext:
        context = real_create_default_context()
        contexts.append(context)
        return context

    class FakeSmtpSsl:
        def __init__(
            self,
            host: str,
            port: int,
            *,
            timeout: int,
            context: ssl.SSLContext,
        ) -> None:
            assert (host, port, timeout) == ("smtp.example.org", 465, 20)
            constructor_contexts.append(context)

        def __enter__(self) -> "FakeSmtpSsl":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            assert (username, password) == ("smtp-user", "smtp-password")

        def send_message(self, message: object) -> None:
            assert message

    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.ssl.create_default_context",
        create_default_context,
    )
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.smtplib.SMTP_SSL",
        FakeSmtpSsl,
    )
    sender = SmtpEmailSender(
        _smtp_config(port=465, use_ssl=True, use_tls=False),  # type: ignore[arg-type]
        _rate_limiter(),  # type: ignore[arg-type]
    )

    sender._send_sync(
        to="user@example.org",
        subject="subject",
        body="body",
        message_id=None,
    )

    assert len(contexts) == 1
    assert constructor_contexts == contexts
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True


def test_starttls_uses_same_default_verified_ssl_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[ssl.SSLContext] = []
    starttls_contexts: list[ssl.SSLContext] = []
    real_create_default_context = ssl.create_default_context

    def create_default_context() -> ssl.SSLContext:
        context = real_create_default_context()
        contexts.append(context)
        return context

    class FakeSmtp:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            assert (host, port, timeout) == ("smtp.example.org", 587, 20)

        def __enter__(self) -> "FakeSmtp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def ehlo(self) -> None:
            return None

        def starttls(self, *, context: ssl.SSLContext) -> None:
            starttls_contexts.append(context)

        def login(self, username: str, password: str) -> None:
            assert (username, password) == ("smtp-user", "smtp-password")

        def send_message(self, message: object) -> None:
            assert message

    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.ssl.create_default_context",
        create_default_context,
    )
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.smtplib.SMTP",
        FakeSmtp,
    )
    sender = SmtpEmailSender(
        _smtp_config(port=587, use_ssl=False, use_tls=True),  # type: ignore[arg-type]
        _rate_limiter(),  # type: ignore[arg-type]
    )

    sender._send_sync(
        to="user@example.org",
        subject="subject",
        body="body",
        message_id=None,
    )

    assert len(contexts) == 1
    assert starttls_contexts == contexts
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True


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

    with pytest.raises(EmailDeliveryError) as raised:
        await sender.send(to="user@example.com", subject="subject", body="body")

    assert raised.value.code == "SMTP_AUTH_REJECTED"
    assert raised.value.retryable is False
    assert sender._send_sync.call_count == 1


async def test_temporary_provider_limit_keeps_safe_retryable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _sender()
    sender._send_sync = Mock(side_effect=smtplib.SMTPDataError(450, b"private provider response"))
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )

    with pytest.raises(EmailDeliveryError) as raised:
        await sender.send(to="user@example.com", subject="subject", body="body")

    assert raised.value.code == "SMTP_RATE_LIMITED"
    assert raised.value.retryable is True
    assert "private" not in str(raised.value)


def test_sender_reuses_one_authenticated_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = 0
    sent_messages: list[object] = []

    class FakeSmtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal constructor_calls
            constructor_calls += 1

        def ehlo(self) -> None:
            return None

        def starttls(self, *, context: ssl.SSLContext) -> None:
            assert context.check_hostname is True

        def login(self, username: str, password: str) -> None:
            assert (username, password) == ("smtp-user", "smtp-password")

        def send_message(self, message: object) -> None:
            sent_messages.append(message)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.smtplib.SMTP",
        FakeSmtp,
    )
    sender = SmtpEmailSender(
        _smtp_config(port=587, use_ssl=False, use_tls=True),  # type: ignore[arg-type]
        _rate_limiter(),  # type: ignore[arg-type]
    )

    sender._send_sync(to="first@example.org", subject="one", body="one", message_id=None)
    sender._send_sync(to="second@example.org", subject="two", body="two", message_id=None)

    assert constructor_calls == 1
    assert len(sent_messages) == 2


async def test_reminder_rate_slot_is_reserved_immediately_before_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    limiter = _rate_limiter()

    async def reserve(**kwargs: object) -> bool:
        assert kwargs == {"rate_per_minute": 10, "max_wait_seconds": 12.5}
        order.append("permit")
        return True

    limiter.wait_for_send_slot.side_effect = reserve
    sender = SmtpEmailSender(
        SimpleNamespace(email=SimpleNamespace()),  # type: ignore[arg-type]
        limiter,  # type: ignore[arg-type]
    )
    sender._send_sync = Mock(side_effect=lambda **kwargs: order.append("smtp"))
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )

    await sender.send(
        to="user@example.org",
        subject="subject",
        body="body",
        rate_limit_per_minute=10,
        rate_limit_max_wait_seconds=12.5,
    )

    assert order == ["permit", "smtp"]


async def test_full_rate_window_defers_without_starting_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = SmtpEmailSender(
        SimpleNamespace(email=SimpleNamespace()),  # type: ignore[arg-type]
        _rate_limiter(available=False),  # type: ignore[arg-type]
    )
    sender._send_sync = Mock()
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )

    with pytest.raises(EmailDeliveryRateDeferredError):
        await sender.send(
            to="user@example.org",
            subject="subject",
            body="body",
            rate_limit_per_minute=10,
            rate_limit_max_wait_seconds=0,
        )

    sender._send_sync.assert_not_called()


async def test_transactional_mail_uses_same_rate_key_with_short_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = _rate_limiter()
    sender = SmtpEmailSender(
        SimpleNamespace(
            email=SimpleNamespace(
                subscription_expiration_delivery_rate_per_minute=10,
                timeout_seconds=20,
            )
        ),  # type: ignore[arg-type]
        limiter,  # type: ignore[arg-type]
    )
    sender._send_sync = Mock()
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )

    await sender.send(to="user@example.org", subject="verify", body="code")

    limiter.wait_for_send_slot.assert_awaited_once_with(
        rate_per_minute=10,
        max_wait_seconds=10.0,
    )
    sender._send_sync.assert_called_once()


async def test_cancellation_waits_for_real_smtp_thread_before_unlocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _sender()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def controlled_to_thread(function, **kwargs: object) -> None:
        assert function == sender._send_sync
        assert kwargs
        started.set()
        await finish.wait()

    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        controlled_to_thread,
    )
    task = asyncio.create_task(sender.send(to="user@example.org", subject="subject", body="body"))
    await started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("transport_error", "expected_code", "retryable"),
    [
        (ssl.SSLEOFError(8, "unexpected EOF"), "SMTP_TLS_DISCONNECTED", True),
        (
            ssl.SSLCertVerificationError(1, "certificate verify failed"),
            "SMTP_TLS_CERTIFICATE_ERROR",
            False,
        ),
    ],
)
async def test_tls_failures_keep_transport_semantics(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: ssl.SSLError,
    expected_code: str,
    retryable: bool,
) -> None:
    sender = _sender()
    sender._send_sync = Mock(side_effect=transport_error)
    monkeypatch.setattr(
        "src.infrastructure.services.email_sender.asyncio.to_thread",
        _inline_to_thread,
    )

    with pytest.raises(EmailDeliveryError) as raised:
        await sender.send(to="user@example.org", subject="subject", body="body")

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable
