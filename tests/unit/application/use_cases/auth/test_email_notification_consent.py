from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import SecretStr

from src.application.dto import UserDto
from src.application.use_cases.auth._codes import hash_email_verification_code
from src.application.use_cases.auth.commands.email import (
    ConfirmEmailVerification,
    ConfirmEmailVerificationDto,
    RequestEmailVerification,
    RequestEmailVerificationDto,
)


class FakeUnitOfWork:
    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_new_verification_target_clears_any_previous_reminder_consent() -> None:
    actor = UserDto(
        id=17,
        name="User",
        email=None,
        is_email_verified=False,
        subscription_expiration_email_enabled=True,
        subscription_expiration_email_enabled_at=datetime(
            2026, 8, 24, tzinfo=timezone.utc
        ),
    )
    user_dao = SimpleNamespace(
        get_by_email=AsyncMock(return_value=None),
        update=AsyncMock(side_effect=lambda current: current),
    )
    sender = SimpleNamespace(is_enabled=True, send=AsyncMock())
    config = SimpleNamespace(
        crypt_key=SecretStr("test-secret"),
        email=SimpleNamespace(verification_code_ttl_minutes=15),
    )
    use_case = RequestEmailVerification(
        config,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
    )

    await use_case(
        actor,
        RequestEmailVerificationDto(email="new@example.org"),
    )

    assert actor.pending_email == "new@example.org"
    assert actor.is_email_verified is False
    assert actor.subscription_expiration_email_enabled is False
    assert actor.subscription_expiration_email_enabled_at is None
    sender.send.assert_awaited_once()


async def test_successful_email_verification_enables_reminders_by_default() -> None:
    secret = "test-secret"
    actor = UserDto(
        id=18,
        name="User",
        email="verified@example.org",
        is_email_verified=False,
        email_verification_code_hash=hash_email_verification_code("123456", secret),
        email_verification_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    user_dao = SimpleNamespace(
        get_by_email=AsyncMock(return_value=actor),
        update=AsyncMock(side_effect=lambda current: current),
    )
    config = SimpleNamespace(crypt_key=SecretStr(secret))
    use_case = ConfirmEmailVerification(
        config,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
    )

    result = await use_case(actor, ConfirmEmailVerificationDto(code="123456"))

    assert result.user.is_email_verified is True
    assert result.user.subscription_expiration_email_enabled is True
    assert result.user.subscription_expiration_email_enabled_at is not None
