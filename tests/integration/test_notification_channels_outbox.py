import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.common import ChannelCopy, NotificationCategory
from src.infrastructure.services import notification_channels as module
from src.infrastructure.services.notification_channels import NotificationChannelsImpl
from src.infrastructure.services.web_push import PushResult

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL outbox tests",
)

BASE_ID = 98_600_000
CONSENTED, NO_CONSENT, UNVERIFIED, WEB_ONLY = (BASE_ID + i for i in range(4))
ALL_USERS = (CONSENTED, NO_CONSENT, UNVERIFIED, WEB_ONLY)


class _Lock:
    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        yield


def _config() -> Any:
    return SimpleNamespace(
        web_cabinet_url="https://cabinet.example.com",
        crypt_key=SecretStr("integration-secret"),
        bulk_email=SimpleNamespace(
            from_email="news@mail.example.com",
            from_name="Clean Pay",
            reply_to="",
            rate_per_minute=600,
            max_per_run=50,
            max_runtime_seconds=30,
        ),
        web_push=SimpleNamespace(
            is_configured=True,
            max_per_run=100,
            vapid_public_key="",
            vapid_private_key=SecretStr(""),
            vapid_subject="",
        ),
    )


async def _seed(sessions: Any) -> None:
    async with sessions() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": list(ALL_USERS)}
        )
        for user_id, email, verified, news, telegram in (
            (CONSENTED, "yes@example.org", True, True, True),
            (NO_CONSENT, "no@example.org", True, False, True),
            (UNVERIFIED, "unverified@example.org", False, True, True),
            (WEB_ONLY, "web@example.org", True, True, False),
        ):
            await session.execute(
                text(
                    """
                    INSERT INTO users (
                        id, telegram_id, name, role, language, referral_code,
                        personal_discount, purchase_discount, points, is_blocked,
                        is_bot_blocked, is_rules_accepted, is_trial_available,
                        email, is_email_verified, auth_type, token_version,
                        email_news_enabled, email_news_enabled_at
                    ) VALUES (
                        :id, :telegram_id, 'outbox', 'USER', 'RU', :code,
                        0, 0, 0, false, false, true, false,
                        :email, :verified, 'email', 0,
                        :news, CASE WHEN :news THEN clock_timestamp() END
                    )
                    """
                ),
                {
                    "id": user_id,
                    "telegram_id": user_id if telegram else None,
                    "code": f"outbox-{user_id}",
                    "email": email,
                    "verified": verified,
                    "news": news,
                },
            )
        await session.commit()


@pytest.mark.asyncio
async def test_outbox_respects_consent_and_reports_a_broadcast() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    sender = SimpleNamespace(is_enabled=True, send=AsyncMock())
    channels = NotificationChannelsImpl(_config(), sessions, sender, _Lock())  # type: ignore[arg-type]
    try:
        await _seed(sessions)
        endpoint = f"https://fcm.googleapis.com/fcm/send/{CONSENTED}"
        await channels.save_push_subscription(
            user_id=CONSENTED, endpoint=endpoint, p256dh="p" * 87, auth="a" * 22, user_agent="UA"
        )
        await channels.save_push_subscription(
            user_id=NO_CONSENT,
            endpoint=f"https://fcm.googleapis.com/fcm/send/{NO_CONSENT}",
            p256dh="p" * 87,
            auth="a" * 22,
            user_agent=None,
        )
        assert await channels.push_device_count(CONSENTED) == 1
        with pytest.raises(ValueError):
            await channels.save_push_subscription(
                user_id=CONSENTED,
                endpoint="https://internal.example/hook",
                p256dh="p" * 87,
                auth="a" * 22,
                user_agent=None,
            )

        source = f"broadcast:{BASE_ID}"
        copies = [
            ChannelCopy(
                user_id=user_id,
                text="<b>Новый тариф</b>\nПодробности в кабинете",
                category=NotificationCategory.NEWS,
                dedupe_key=f"{source}:{user_id}",
                source=source,
            )
            for user_id in ALL_USERS
        ]
        # News e-mail: only verified users with consent (web-only included).
        # Push: every subscribed device, consent is the device permission.
        assert await channels.enqueue(copies) == 4
        assert await channels.enqueue(copies) == 0  # replay is idempotent

        stats = await channels.source_stats(source)
        assert (stats.email.recipients, stats.email.queued) == (2, 2)
        assert (stats.push.recipients, stats.push.queued) == (2, 2)

        # NO_CONSENT's device answers 410: the subscription goes away.
        async def fake_send(session: Any, target: Any, payload: bytes, **_: Any) -> PushResult:
            return PushResult(410 if target.endpoint.endswith(str(NO_CONSENT)) else 201)

        channels._push_client = SimpleNamespace(send=fake_send)  # type: ignore[assignment]

        # WEB_ONLY opts out before delivery: the queued e-mail is canceled.
        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE users SET email_news_enabled = false, "
                    "email_news_enabled_at = NULL WHERE id = :id"
                ),
                {"id": WEB_ONLY},
            )
            await session.commit()

        assert await channels.deliver_due() == 2
        sender.send.assert_awaited_once()
        kwargs = sender.send.await_args.kwargs
        assert kwargs["to"] == "yes@example.org"
        assert kwargs["subject"] == "Новый тариф"
        assert kwargs["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

        stats = await channels.source_stats(source)
        assert (stats.email.sent, stats.email.canceled, stats.email.queued) == (1, 1, 0)
        assert (stats.push.sent, stats.push.rows) == (1, 1)
        assert await channels.push_device_count(NO_CONSENT) == 0

        # Cancellation touches only rows still waiting.
        second = [
            ChannelCopy(
                user_id=CONSENTED,
                text="Ещё",
                category=NotificationCategory.NEWS,
                dedupe_key=f"{source}-2:{CONSENTED}",
                source=f"{source}-2",
            )
        ]
        assert await channels.enqueue(second) == 2
        assert await channels.cancel_pending(f"{source}-2") == 2
        assert await channels.cancel_pending(source) == 0

        # Service e-mail is on by default and stops after the opt-out.
        service = ChannelCopy(
            user_id=NO_CONSENT,
            text="Подписка закончилась",
            category=NotificationCategory.SERVICE,
            dedupe_key=f"event:{BASE_ID}:{NO_CONSENT}",
        )
        assert await channels.enqueue([service]) == 1
        skipped = ChannelCopy(
            user_id=NO_CONSENT,
            text="Подписка скоро закончится",
            category=NotificationCategory.SERVICE,
            dedupe_key=f"event:{BASE_ID + 1}:{NO_CONSENT}",
            skip_email=True,
        )
        assert await channels.enqueue([skipped]) == 0
    finally:
        async with sessions() as session:
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": list(ALL_USERS)}
            )
            await session.commit()
        await engine.dispose()


def test_retry_backoff_is_capped() -> None:
    assert module._retry_after(1).total_seconds() == 60
    assert module._retry_after(20).total_seconds() == module.RETRY_MAX_SECONDS
