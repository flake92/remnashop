import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError

from src.application.common import EmailDeliveryRunBusyError
from src.application.dto import (
    SubscriptionEmailDeliveryDto,
    SubscriptionEmailReminderDto,
    UserDto,
)
from src.application.use_cases.notification import commands as notification_commands
from src.application.use_cases.notification.commands import (
    DELIVERY_LEASE,
    GENERATION_BATCHES_PER_RUN,
    GENERATION_MAX_ROWS_PER_RUN,
    TERMINAL_CLEANUP_BATCH_SIZE,
    DeliverSubscriptionExpirationEmailReminders,
    GenerateSubscriptionExpirationEmailReminders,
    GetNotificationPreferences,
    NotificationEmailNotEligibleError,
    UpdateNotificationPreferences,
    UpdateNotificationPreferencesDto,
    _message_id,
)
from src.core.config.email import EmailConfig
from src.core.exceptions import EmailDeliveryError, EmailDeliveryRateDeferredError

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.active = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        assert self.active is False
        self.active = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.active = False
        if args and args[0] is not None:
            await self.rollback()
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _config(*, reminders_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        crypt_key=SecretStr("test-message-id-secret"),
        email=SimpleNamespace(
            subscription_expiration_reminders_enabled=reminders_enabled,
            subscription_expiration_cabinet_url="https://cabinet.example.org/cabinet",
            subscription_expiration_delivery_rate_per_minute=600,
            subscription_expiration_delivery_max_per_run=20,
            subscription_expiration_delivery_max_runtime_seconds=45,
            from_email="notice@example.org",
        ),
    )


def _sender(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(is_enabled=enabled, send=AsyncMock())


def _delivery_lock() -> SimpleNamespace:
    @asynccontextmanager
    async def hold() -> AsyncIterator[None]:
        yield

    return SimpleNamespace(hold=hold)


def _user(
    *,
    verified: bool = True,
    opted_in: bool = False,
    merged_into_user_id: int | None = None,
) -> UserDto:
    return UserDto(
        id=17,
        name="User",
        email="user@example.org",
        is_email_verified=verified,
        merged_into_user_id=merged_into_user_id,
        subscription_expiration_email_enabled=opted_in,
        subscription_expiration_email_enabled_at=(NOW - timedelta(days=30) if opted_in else None),
    )


def _preference_dao(user: UserDto) -> SimpleNamespace:
    def persist(
        user_id: int,
        *,
        enabled: bool,
    ) -> UserDto:
        assert user_id == user.id
        if enabled:
            if not user.subscription_expiration_email_enabled:
                # Production assigns this after the row lock with clock_timestamp().
                user.subscription_expiration_email_enabled_at = NOW
            user.subscription_expiration_email_enabled = True
        else:
            user.subscription_expiration_email_enabled = False
            user.subscription_expiration_email_enabled_at = None
        return user

    return SimpleNamespace(
        set_subscription_expiration_email_preference=AsyncMock(side_effect=persist)
    )


def test_reminder_cabinet_url_must_be_browser_safe_https() -> None:
    with pytest.raises(ValidationError):
        EmailConfig(subscription_expiration_cabinet_url="http://cabinet.example.org")

    config = EmailConfig(
        subscription_expiration_cabinet_url="  https://cabinet.example.org/cabinet  "
    )
    assert config.subscription_expiration_cabinet_url == "https://cabinet.example.org/cabinet"


def test_delivery_rate_and_runtime_are_explicitly_bounded() -> None:
    config = EmailConfig()
    assert config.subscription_expiration_reminders_enabled is True
    assert config.subscription_expiration_delivery_rate_per_minute == 10
    assert config.subscription_expiration_delivery_max_per_run == 20
    assert config.subscription_expiration_delivery_max_runtime_seconds == 45

    with pytest.raises(ValidationError):
        EmailConfig(subscription_expiration_delivery_rate_per_minute=0)
    with pytest.raises(ValidationError):
        EmailConfig(subscription_expiration_delivery_max_runtime_seconds=60)
    with pytest.raises(ValidationError):
        EmailConfig(timeout_seconds=31)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https:///cabinet",
        "https://?next=/cabinet",
        "https://user@cabinet.example.org/cabinet",
        "https://user:password@cabinet.example.org/cabinet",
        "https://cabinet.example.org:99999/cabinet",
        "https://cabinet.example.org/cabinet?token=secret",
        "https://cabinet.example.org/cabinet#account",
    ],
)
def test_reminder_cabinet_url_rejects_unsafe_authority_and_suffixes(
    unsafe_url: str,
) -> None:
    with pytest.raises(
        ValidationError,
        match="hostname.*without userinfo, query, or fragment",
    ):
        EmailConfig(subscription_expiration_cabinet_url=unsafe_url)


async def test_preferences_keep_stored_consent_visible_when_delivery_is_unavailable() -> None:
    user = _user(opted_in=True)
    use_case = GetNotificationPreferences(
        _config(reminders_enabled=False),  # type: ignore[arg-type]
    )

    result = await use_case(user)

    assert result.subscription_expiration_email_enabled is True
    assert result.email_eligible is True
    assert result.sender_email == "notice@example.org"
    assert result.days_before == (7, 3, 1)


async def test_stored_opt_in_can_be_disabled_during_delivery_outage() -> None:
    user = _user(opted_in=True)
    user_dao = _preference_dao(user)
    uow = FakeUnitOfWork()
    use_case = UpdateNotificationPreferences(
        _config(reminders_enabled=False),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    result = await use_case(
        user,
        UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=False),
    )

    assert result.subscription_expiration_email_enabled is False
    assert result.email_eligible is True
    assert user.subscription_expiration_email_enabled_at is None
    assert uow.commits == 1


async def test_enabling_requires_verified_email_but_not_live_smtp() -> None:
    user_dao = SimpleNamespace(set_subscription_expiration_email_preference=AsyncMock())
    uow = FakeUnitOfWork()
    use_case = UpdateNotificationPreferences(
        _config(),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    with pytest.raises(NotificationEmailNotEligibleError):
        await use_case(
            _user(verified=False),
            UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=True),
        )

    with pytest.raises(NotificationEmailNotEligibleError):
        await use_case(
            _user(merged_into_user_id=99),
            UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=True),
        )

    user = _user()
    unavailable_dao = _preference_dao(user)
    unavailable = UpdateNotificationPreferences(
        _config(reminders_enabled=False),  # type: ignore[arg-type]
        unavailable_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )
    result = await unavailable(
        user,
        UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=True),
    )

    user_dao.set_subscription_expiration_email_preference.assert_not_awaited()
    unavailable_dao.set_subscription_expiration_email_preference.assert_awaited_once()
    assert result.subscription_expiration_email_enabled is True
    assert result.email_eligible is True


async def test_enabling_persists_explicit_consent_timestamp() -> None:
    user = _user()
    user_dao = _preference_dao(user)
    uow = FakeUnitOfWork()
    use_case = UpdateNotificationPreferences(
        _config(),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    result = await use_case(
        user,
        UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=True),
    )

    assert result.subscription_expiration_email_enabled is True
    assert user.subscription_expiration_email_enabled is True
    assert user.subscription_expiration_email_enabled_at is not None
    assert uow.commits == 1


async def test_repeated_enable_preserves_original_consent_timestamp() -> None:
    user = _user(opted_in=True)
    original_enabled_at = user.subscription_expiration_email_enabled_at
    user_dao = _preference_dao(user)
    use_case = UpdateNotificationPreferences(
        _config(),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    result = await use_case(
        user,
        UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=True),
    )

    assert result.subscription_expiration_email_enabled is True
    assert user.subscription_expiration_email_enabled_at == original_enabled_at
    user_dao.set_subscription_expiration_email_preference.assert_awaited_once_with(
        user.id,
        enabled=True,
    )


async def test_atomic_enable_rejects_stale_profile_after_email_change() -> None:
    stale_user = _user(verified=True)
    user_dao = SimpleNamespace(
        set_subscription_expiration_email_preference=AsyncMock(return_value=None)
    )
    uow = FakeUnitOfWork()
    use_case = UpdateNotificationPreferences(
        _config(),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    with pytest.raises(NotificationEmailNotEligibleError):
        await use_case(
            stale_user,
            UpdateNotificationPreferencesDto(subscription_expiration_email_enabled=True),
        )

    user_dao.set_subscription_expiration_email_preference.assert_awaited_once()
    assert stale_user.subscription_expiration_email_enabled is False
    assert uow.commits == 0


async def test_generation_uses_paid_subscription_thresholds_and_bounded_batch() -> None:
    reminder_dao = SimpleNamespace(
        generate=AsyncMock(return_value=3),
        delete_terminal_before=AsyncMock(return_value=0),
    )
    use_case = GenerateSubscriptionExpirationEmailReminders(
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 3

    kwargs = reminder_dao.generate.await_args.kwargs
    assert kwargs["days_before"] == (7, 3, 1)
    assert kwargs["candidate_limit"] == 500
    assert kwargs["generation_grace"] == timedelta(hours=1)
    cleanup = reminder_dao.delete_terminal_before.await_args.kwargs
    assert GENERATION_MAX_ROWS_PER_RUN == 500 * GENERATION_BATCHES_PER_RUN * 3
    assert cleanup["limit"] == TERMINAL_CLEANUP_BATCH_SIZE
    assert cleanup["limit"] > GENERATION_MAX_ROWS_PER_RUN


async def test_generation_and_cleanup_continue_while_delivery_is_disabled() -> None:
    reminder_dao = SimpleNamespace(
        generate=AsyncMock(return_value=2),
        delete_terminal_before=AsyncMock(return_value=2),
    )
    use_case = GenerateSubscriptionExpirationEmailReminders(
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 2
    reminder_dao.generate.assert_awaited_once()
    cleanup = reminder_dao.delete_terminal_before.await_args.kwargs
    assert cleanup["limit"] == TERMINAL_CLEANUP_BATCH_SIZE
    assert cleanup["limit"] > GENERATION_MAX_ROWS_PER_RUN


def _claimed_reminder() -> SubscriptionEmailReminderDto:
    return SubscriptionEmailReminderDto(
        id=42,
        user_id=17,
        subscription_id=9,
        expire_at_snapshot=NOW + timedelta(days=3),
        days_before=3,
        due_at=NOW,
        state="PROCESSING",
        attempt_count=1,
        next_attempt_at=NOW,
    )


async def test_delivery_uses_stable_message_id_and_required_russian_copy() -> None:
    delivery = SubscriptionEmailDeliveryDto(
        reminder_id=42,
        recipient_email="user@example.org",
        expire_at=NOW + timedelta(days=3),
        days_before=3,
        attempt_count=1,
    )
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(side_effect=[[_claimed_reminder()], []]),
        prepare_delivery=AsyncMock(return_value=delivery),
        mark_sent=AsyncMock(return_value=True),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    uow = FakeUnitOfWork()

    async def assert_no_open_transaction(**kwargs: object) -> None:
        assert kwargs
        assert uow.active is False
        assert uow.commits == 3

    sender.send.side_effect = assert_no_open_transaction
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    assert await use_case.system() == 1

    send = sender.send.await_args.kwargs
    assert send["to"] == "user@example.org"
    message_id = send["message_id"]
    assert message_id == _message_id(
        42,
        "notice@example.org",
        "test-message-id-secret",
    )
    assert "subscription-expiration-42@" not in message_id
    assert "автопродление не выполняется" in send["body"]
    assert "Если вы уже продлили подписку" in send["body"]
    assert "белый список" in send["body"]
    assert "https://cabinet.example.org/cabinet" in send["body"]
    assert send["subject"] == "Напоминание: срок подписки скоро закончится"
    assert "через 3" not in send["subject"]
    assert send["rate_limit_per_minute"] == 600
    assert send["rate_limit_max_wait_seconds"] > 0
    assert uow.commits == 5
    claim = reminder_dao.claim_due.await_args_list[0].kwargs
    assert claim["limit"] == 1
    assert claim["lease_for"] == DELIVERY_LEASE
    assert "delivery_not_before" not in claim
    assert "max_attempts" not in claim
    reminder_dao.release_failed.assert_not_awaited()


def test_message_id_is_stable_opaque_and_domain_scoped() -> None:
    first = _message_id(42, "notice@example.org", "test-message-id-secret")
    repeated = _message_id(42, "notice@example.org", "test-message-id-secret")
    different_row = _message_id(43, "notice@example.org", "test-message-id-secret")

    assert first == repeated
    assert first != different_row
    assert re.fullmatch(
        r"<subscription-expiration-[0-9a-f]{64}@example\.org>",
        first,
    )
    assert "subscription-expiration-42@" not in first

    with pytest.raises(ValueError, match="valid domain"):
        _message_id(42, "invalid-sender", "test-message-id-secret")


async def test_delivery_failure_is_retried_with_safe_non_pii_error_code() -> None:
    delivery = SubscriptionEmailDeliveryDto(
        reminder_id=42,
        recipient_email="user@example.org",
        expire_at=NOW + timedelta(days=3),
        days_before=3,
        # One prior SMTP attempt; this send is the second and therefore gets a
        # two-minute retry. claim_due itself no longer fabricates an attempt.
        attempt_count=1,
    )
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(side_effect=[[_claimed_reminder()], []]),
        prepare_delivery=AsyncMock(return_value=delivery),
        mark_sent=AsyncMock(),
        release_failed=AsyncMock(return_value=True),
    )
    sender = _sender()
    sender.send.side_effect = EmailDeliveryError(
        code="SMTP_RATE_LIMITED",
        retryable=True,
    )
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 0

    retry = reminder_dao.release_failed.await_args.kwargs
    assert retry["error_code"] == "SMTP_RATE_LIMITED"
    assert "user@example.org" not in retry["error_code"]
    assert retry["retryable"] is True
    assert retry["retry_after"] == timedelta(minutes=2)
    reminder_dao.mark_sent.assert_not_awaited()


async def test_permanent_smtp_rejection_is_terminalized_without_retry() -> None:
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(side_effect=[[_claimed_reminder()], []]),
        prepare_delivery=AsyncMock(
            return_value=SubscriptionEmailDeliveryDto(
                reminder_id=42,
                recipient_email="user@example.org",
                expire_at=NOW + timedelta(days=3),
                days_before=3,
                attempt_count=1,
            )
        ),
        mark_sent=AsyncMock(),
        release_failed=AsyncMock(return_value=True),
    )
    sender = _sender()
    sender.send.side_effect = EmailDeliveryError(
        code="SMTP_RECIPIENT_REJECTED",
        retryable=False,
    )
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 0

    failure = reminder_dao.release_failed.await_args.kwargs
    assert failure["error_code"] == "SMTP_RECIPIENT_REJECTED"
    assert failure["retryable"] is False
    reminder_dao.mark_sent.assert_not_awaited()


async def test_long_running_delivery_renews_fenced_lease_before_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notification_commands,
        "DELIVERY_HEARTBEAT_INTERVAL",
        timedelta(milliseconds=1),
    )
    delivery = SubscriptionEmailDeliveryDto(
        reminder_id=42,
        recipient_email="user@example.org",
        expire_at=NOW + timedelta(days=3),
        days_before=3,
        attempt_count=1,
    )
    lease_renewed = asyncio.Event()
    renewal_in_progress = False
    uow = FakeUnitOfWork()

    async def renew_lease(*args: object, **kwargs: object) -> bool:
        nonlocal renewal_in_progress
        assert args == (42,)
        assert kwargs["lease_for"] == DELIVERY_LEASE
        assert uow.active is True
        renewal_in_progress = True
        await asyncio.sleep(0)
        renewal_in_progress = False
        lease_renewed.set()
        return True

    async def wait_for_renewal(**kwargs: object) -> None:
        assert kwargs
        await lease_renewed.wait()

    async def mark_sent(*args: object, **kwargs: object) -> bool:
        assert args == (42,)
        assert kwargs
        assert lease_renewed.is_set()
        assert renewal_in_progress is False
        return True

    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(side_effect=[[_claimed_reminder()], []]),
        prepare_delivery=AsyncMock(return_value=delivery),
        renew_processing_lease=AsyncMock(side_effect=renew_lease),
        mark_sent=AsyncMock(side_effect=mark_sent),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    sender.send.side_effect = wait_for_renewal
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    assert await use_case.system() == 1

    assert reminder_dao.renew_processing_lease.await_count >= 1
    reminder_dao.mark_sent.assert_awaited_once()
    reminder_dao.release_failed.assert_not_awaited()


async def test_delivery_does_not_finalize_after_heartbeat_loses_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notification_commands,
        "DELIVERY_HEARTBEAT_INTERVAL",
        timedelta(milliseconds=1),
    )
    delivery = SubscriptionEmailDeliveryDto(
        reminder_id=42,
        recipient_email="user@example.org",
        expire_at=NOW + timedelta(days=3),
        days_before=3,
        attempt_count=1,
    )
    renewal_attempted = asyncio.Event()

    async def lose_fence(*args: object, **kwargs: object) -> bool:
        assert args == (42,)
        assert kwargs
        renewal_attempted.set()
        return False

    async def finish_after_heartbeat(**kwargs: object) -> None:
        assert kwargs
        await renewal_attempted.wait()

    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(side_effect=[[_claimed_reminder()], []]),
        prepare_delivery=AsyncMock(return_value=delivery),
        renew_processing_lease=AsyncMock(side_effect=lose_fence),
        mark_sent=AsyncMock(),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    sender.send.side_effect = finish_after_heartbeat
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 0

    reminder_dao.renew_processing_lease.assert_awaited_once()
    reminder_dao.mark_sent.assert_not_awaited()
    reminder_dao.release_failed.assert_not_awaited()


async def test_delivery_heartbeat_retries_safe_transient_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notification_commands,
        "DELIVERY_HEARTBEAT_INTERVAL",
        timedelta(milliseconds=1),
    )
    delivery = SubscriptionEmailDeliveryDto(
        reminder_id=42,
        recipient_email="user@example.org",
        expire_at=NOW + timedelta(days=3),
        days_before=3,
        attempt_count=1,
    )
    lease_renewed = asyncio.Event()
    renewal_attempts = 0

    async def transient_then_renew(*args: object, **kwargs: object) -> bool:
        nonlocal renewal_attempts
        assert args == (42,)
        assert kwargs
        renewal_attempts += 1
        if renewal_attempts == 1:
            raise RuntimeError("private user@example.org database detail")
        lease_renewed.set()
        return True

    async def finish_after_renewal(**kwargs: object) -> None:
        assert kwargs
        await lease_renewed.wait()

    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(side_effect=[[_claimed_reminder()], []]),
        prepare_delivery=AsyncMock(return_value=delivery),
        renew_processing_lease=AsyncMock(side_effect=transient_then_renew),
        mark_sent=AsyncMock(return_value=True),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    sender.send.side_effect = finish_after_renewal
    uow = FakeUnitOfWork()
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
    )

    assert await use_case.system() == 1

    assert reminder_dao.renew_processing_lease.await_count >= 2
    assert uow.rollbacks == 1
    reminder_dao.mark_sent.assert_awaited_once()


async def test_delivery_maintenance_runs_fail_closed_when_sending_is_disabled() -> None:
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=4),
        claim_due=AsyncMock(),
    )
    sender = _sender()
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(reminders_enabled=False),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 0

    sweep = reminder_dao.sweep_undeliverable.await_args.kwargs
    assert "delivery_not_before" not in sweep
    assert "max_attempts" not in sweep
    assert sweep["limit"] == 500
    reminder_dao.claim_due.assert_not_awaited()
    sender.send.assert_not_awaited()


async def test_delivery_rate_limits_attempt_starts_and_caps_each_run() -> None:
    config = _config()
    config.email.subscription_expiration_delivery_rate_per_minute = 60
    config.email.subscription_expiration_delivery_max_per_run = 3
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(return_value=[_claimed_reminder()]),
        prepare_delivery=AsyncMock(
            return_value=SubscriptionEmailDeliveryDto(
                reminder_id=42,
                recipient_email="user@example.org",
                expire_at=NOW + timedelta(days=3),
                days_before=3,
                attempt_count=1,
            )
        ),
        mark_sent=AsyncMock(return_value=True),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    use_case = DeliverSubscriptionExpirationEmailReminders(
        config,  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 3

    assert reminder_dao.claim_due.await_count == 3
    assert sender.send.await_count == 3
    assert all(
        call.kwargs["rate_limit_per_minute"] == 60
        and call.kwargs["rate_limit_max_wait_seconds"] > 0
        for call in sender.send.await_args_list
    )


async def test_rate_defer_releases_fence_without_counting_smtp_attempt() -> None:
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(return_value=[_claimed_reminder()]),
        prepare_delivery=AsyncMock(
            return_value=SubscriptionEmailDeliveryDto(
                reminder_id=42,
                recipient_email="user@example.org",
                expire_at=NOW + timedelta(days=3),
                days_before=3,
                attempt_count=4,
            )
        ),
        release_unattempted=AsyncMock(return_value=True),
        mark_sent=AsyncMock(),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    sender.send.side_effect = EmailDeliveryRateDeferredError("no room")
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 0

    reminder_dao.release_unattempted.assert_awaited_once()
    reminder_dao.release_failed.assert_not_awaited()


async def test_cancelled_delivery_persists_smtp_success_before_releasing_run_lock() -> None:
    smtp_started = asyncio.Event()
    allow_smtp_completion = asyncio.Event()
    run_lock_released = asyncio.Event()

    @asynccontextmanager
    async def hold() -> AsyncIterator[None]:
        try:
            yield
        finally:
            run_lock_released.set()

    async def controlled_send(**kwargs: object) -> None:
        assert kwargs
        smtp_started.set()
        await allow_smtp_completion.wait()

    reminder_dao = SimpleNamespace(
        prepare_delivery=AsyncMock(
            return_value=SubscriptionEmailDeliveryDto(
                reminder_id=42,
                recipient_email="user@example.org",
                expire_at=NOW + timedelta(days=3),
                days_before=3,
                attempt_count=0,
            )
        ),
        renew_processing_lease=AsyncMock(return_value=True),
        mark_sent=AsyncMock(return_value=True),
        release_failed=AsyncMock(),
        release_unattempted=AsyncMock(),
    )

    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        SimpleNamespace(is_enabled=True, send=controlled_send),  # type: ignore[arg-type]
        SimpleNamespace(hold=hold),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    async def run_under_delivery_lock() -> None:
        async with use_case.delivery_run_lock.hold():
            await use_case._deliver_one(
                reminder_id=42,
                token_hash="a" * 64,
                sender_email="notice@example.org",
                cabinet_url="https://cabinet.example.org/cabinet",
                deadline_monotonic=asyncio.get_running_loop().time() + 30,
            )

    waiter = asyncio.create_task(run_under_delivery_lock())
    await smtp_started.wait()

    waiter.cancel()
    await asyncio.sleep(0)
    assert waiter.done() is False
    assert run_lock_released.is_set() is False
    reminder_dao.mark_sent.assert_not_awaited()

    allow_smtp_completion.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    reminder_dao.mark_sent.assert_awaited_once()
    reminder_dao.release_failed.assert_not_awaited()
    assert run_lock_released.is_set() is True


async def test_cancelled_delivery_persists_smtp_failure_before_propagating() -> None:
    smtp_started = asyncio.Event()
    allow_smtp_completion = asyncio.Event()

    async def controlled_send(**kwargs: object) -> None:
        assert kwargs
        smtp_started.set()
        await allow_smtp_completion.wait()
        raise EmailDeliveryError(code="SMTP_CONNECTION_ERROR", retryable=True)

    reminder_dao = SimpleNamespace(
        prepare_delivery=AsyncMock(
            return_value=SubscriptionEmailDeliveryDto(
                reminder_id=42,
                recipient_email="user@example.org",
                expire_at=NOW + timedelta(days=3),
                days_before=3,
                attempt_count=2,
            )
        ),
        renew_processing_lease=AsyncMock(return_value=True),
        mark_sent=AsyncMock(),
        release_failed=AsyncMock(return_value=True),
        release_unattempted=AsyncMock(),
    )
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        SimpleNamespace(is_enabled=True, send=controlled_send),  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )
    delivery = asyncio.create_task(
        use_case._deliver_one(
            reminder_id=42,
            token_hash="a" * 64,
            sender_email="notice@example.org",
            cabinet_url="https://cabinet.example.org/cabinet",
            deadline_monotonic=asyncio.get_running_loop().time() + 30,
        )
    )
    await smtp_started.wait()

    delivery.cancel()
    await asyncio.sleep(0)
    assert delivery.done() is False
    reminder_dao.release_failed.assert_not_awaited()

    allow_smtp_completion.set()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    reminder_dao.release_failed.assert_awaited_once()
    assert reminder_dao.release_failed.await_args.kwargs["error_code"] == ("SMTP_CONNECTION_ERROR")
    assert reminder_dao.release_failed.await_args.kwargs["retryable"] is True
    reminder_dao.mark_sent.assert_not_awaited()


async def test_stale_rows_do_not_consume_smtp_attempt_budget() -> None:
    config = _config()
    config.email.subscription_expiration_delivery_max_per_run = 1
    delivery = SubscriptionEmailDeliveryDto(
        reminder_id=42,
        recipient_email="user@example.org",
        expire_at=NOW + timedelta(days=3),
        days_before=3,
        attempt_count=0,
    )
    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(return_value=0),
        claim_due=AsyncMock(return_value=[_claimed_reminder()]),
        prepare_delivery=AsyncMock(side_effect=[None, None, delivery]),
        mark_sent=AsyncMock(return_value=True),
        release_failed=AsyncMock(),
    )
    sender = _sender()
    use_case = DeliverSubscriptionExpirationEmailReminders(
        config,  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _delivery_lock(),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 1
    assert reminder_dao.claim_due.await_count == 3
    sender.send.assert_awaited_once()


async def test_overlapping_delivery_run_does_not_claim_or_sweep() -> None:
    @asynccontextmanager
    async def busy_hold() -> AsyncIterator[None]:
        raise EmailDeliveryRunBusyError("already running")
        yield

    reminder_dao = SimpleNamespace(
        sweep_undeliverable=AsyncMock(),
        claim_due=AsyncMock(),
    )
    use_case = DeliverSubscriptionExpirationEmailReminders(
        _config(),  # type: ignore[arg-type]
        _sender(),  # type: ignore[arg-type]
        SimpleNamespace(hold=busy_hold),  # type: ignore[arg-type]
        reminder_dao,  # type: ignore[arg-type]
        FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert await use_case.system() == 0
    reminder_dao.sweep_undeliverable.assert_not_awaited()
    reminder_dao.claim_due.assert_not_awaited()
