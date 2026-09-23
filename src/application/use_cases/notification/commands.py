import asyncio
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

from loguru import logger

from src.application.common import (
    EmailDeliveryRunBusyError,
    EmailDeliveryRunLock,
    EmailSender,
    Interactor,
)
from src.application.common.dao import SubscriptionEmailReminderDao, UserDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import NotificationPreferencesDto, UserDto
from src.core.config import AppConfig
from src.core.exceptions import EmailDeliveryError, EmailDeliveryRateDeferredError
from src.core.utils.time import datetime_now

REMINDER_DAYS_BEFORE = (7, 3, 1)
GENERATION_CANDIDATE_LIMIT = 500
GENERATION_BATCHES_PER_RUN = 10
GENERATION_GRACE = timedelta(hours=1)
GENERATION_MAX_ROWS_PER_RUN = (
    GENERATION_CANDIDATE_LIMIT * GENERATION_BATCHES_PER_RUN * len(REMINDER_DAYS_BEFORE)
)
TERMINAL_RETENTION = timedelta(days=90)
# Hourly retention throughput must stay strictly above worst-case hourly
# generation, otherwise a sustained eligible population can grow the table
# even after every row has passed the retention window.
TERMINAL_CLEANUP_BATCH_SIZE = GENERATION_MAX_ROWS_PER_RUN * 2
# smtplib's configured timeout applies to each socket operation, not to the
# complete SMTP transaction. Claim only one row immediately before its network
# call; this lease covers the finite operation sequence and bounded auth retry.
DELIVERY_LEASE = timedelta(minutes=10)
DELIVERY_HEARTBEAT_INTERVAL = timedelta(minutes=1)
DELIVERY_RETRY_MAX_SECONDS = 60 * 60
DELIVERY_MAINTENANCE_BATCH_SIZE = 500
DELIVERY_SCAN_MAX_PER_RUN = 500


class NotificationEmailNotEligibleError(Exception): ...


def _sender_domain(sender_email: str) -> str | None:
    local, separator, domain = sender_email.strip().lower().rpartition("@")
    if not separator or not local or "@" in local or len(domain) > 253:
        return None
    labels = domain.split(".")
    if not labels or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels
    ):
        return None
    return domain


def _delivery_ready(config: AppConfig, email_sender: EmailSender) -> bool:
    return bool(
        config.email.subscription_expiration_reminders_enabled
        and email_sender.is_enabled
        and _sender_domain(config.email.from_email) is not None
        and config.email.subscription_expiration_cabinet_url.strip()
    )


def _user_email_eligible(
    actor: UserDto,
) -> bool:
    return bool(
        actor.email
        and actor.is_email_verified
        and not actor.is_blocked
        and actor.merged_into_user_id is None
    )


def _preferences(
    actor: UserDto,
    *,
    config: AppConfig,
) -> NotificationPreferencesDto:
    eligible = _user_email_eligible(actor)
    return NotificationPreferencesDto(
        # Consent and current delivery capability are intentionally independent.
        # A stored opt-in must remain visible (and therefore revocable) during an
        # SMTP/global-switch outage; the worker still fails closed on eligibility.
        subscription_expiration_email_enabled=(actor.subscription_expiration_email_enabled),
        email_eligible=eligible,
        sender_email=config.email.from_email.strip() or None,
        days_before=REMINDER_DAYS_BEFORE,
    )


class GetNotificationPreferences(Interactor[None, NotificationPreferencesDto]):
    required_permission = Permission.PUBLIC

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def _execute(self, actor: UserDto, data: None) -> NotificationPreferencesDto:
        return _preferences(actor, config=self.config)


@dataclass(frozen=True)
class UpdateNotificationPreferencesDto:
    subscription_expiration_email_enabled: bool


class UpdateNotificationPreferences(
    Interactor[UpdateNotificationPreferencesDto, NotificationPreferencesDto]
):
    required_permission = Permission.PUBLIC

    def __init__(
        self,
        config: AppConfig,
        user_dao: UserDao,
        uow: UnitOfWork,
    ) -> None:
        self.config = config
        self.user_dao = user_dao
        self.uow = uow

    async def _execute(
        self,
        actor: UserDto,
        data: UpdateNotificationPreferencesDto,
    ) -> NotificationPreferencesDto:
        if data.subscription_expiration_email_enabled and not _user_email_eligible(actor):
            if not actor.email or not actor.is_email_verified:
                raise NotificationEmailNotEligibleError("A verified email address is required")
            raise NotificationEmailNotEligibleError("User is not eligible")

        async with self.uow:
            updated = await self.user_dao.set_subscription_expiration_email_preference(
                actor.id,
                enabled=data.subscription_expiration_email_enabled,
            )
            if updated is None:
                raise NotificationEmailNotEligibleError(
                    "Verified email eligibility changed; refresh and try again"
                )
            await self.uow.commit()
        return _preferences(updated, config=self.config)


class GenerateSubscriptionExpirationEmailReminders(Interactor[None, int]):
    required_permission = None

    def __init__(
        self,
        reminder_dao: SubscriptionEmailReminderDao,
        uow: UnitOfWork,
    ) -> None:
        self.reminder_dao = reminder_dao
        self.uow = uow

    async def _execute(self, actor: UserDto, data: None) -> int:
        now = datetime_now()
        async with self.uow:
            generated = 0
            # Durable schedule generation is independent of transient SMTP
            # availability and the delivery kill switch. Otherwise a long
            # outage can cross a threshold before any outbox row exists and
            # silently lose a still-relevant reminder.
            for _ in range(GENERATION_BATCHES_PER_RUN):
                batch_generated = await self.reminder_dao.generate(
                    now=now,
                    days_before=REMINDER_DAYS_BEFORE,
                    candidate_limit=GENERATION_CANDIDATE_LIMIT,
                    generation_grace=GENERATION_GRACE,
                )
                generated += batch_generated
                # Every selected candidate produces at least one row. A
                # smaller result therefore proves that this batch did not hit
                # the candidate limit and no immediate drain is needed.
                if batch_generated < GENERATION_CANDIDATE_LIMIT:
                    break
            deleted = await self.reminder_dao.delete_terminal_before(
                before=now - TERMINAL_RETENTION,
                limit=TERMINAL_CLEANUP_BATCH_SIZE,
            )
            await self.uow.commit()
        if generated:
            logger.info("Generated '{}' subscription email reminders", generated)
        if deleted:
            logger.info("Deleted '{}' retained terminal email reminders", deleted)
        return generated


def _processing_token_hash() -> str:
    token = secrets.token_urlsafe(32)
    return hashlib.sha256(f"remnashop:subscription-email:v1\0{token}".encode()).hexdigest()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, EmailDeliveryError):
        return exc.code[:64]
    error_type = re.sub(r"[^A-Za-z0-9]", "_", type(exc).__name__).upper()
    return f"EMAIL_{error_type}"[:64]


def _retry_after(attempt_count: int) -> timedelta:
    seconds = min(60 * (2 ** max(attempt_count - 1, 0)), DELIVERY_RETRY_MAX_SECONDS)
    return timedelta(seconds=seconds)


def _message_id(reminder_id: int, sender_email: str, secret: str) -> str:
    domain = _sender_domain(sender_email)
    if domain is None:
        raise ValueError("sender_email must contain a valid domain")
    opaque_id = hmac.new(
        secret.encode("utf-8"),
        f"remnashop:subscription-expiration-message-id:v1\0{reminder_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"<subscription-expiration-{opaque_id}@{domain}>"


class _DeliveryOutcome(Enum):
    SKIPPED = auto()
    DEFERRED = auto()
    FAILED = auto()
    SENT = auto()


def _email_content(
    *,
    expire_at: datetime,
    cabinet_url: str,
    sender_email: str,
) -> tuple[str, str]:
    # A queued threshold may be delivered after an SMTP outage. Avoid a stale
    # relative claim such as "через 7 дней"; the exact date below remains true
    # regardless of delivery delay.
    subject = "Напоминание: срок подписки скоро закончится"
    expire_text = expire_at.strftime("%d.%m.%Y в %H:%M UTC")
    body = (
        "Здравствуйте!\n\n"
        f"Ваша оплаченная подписка истекает {expire_text}. "
        "Это только напоминание — автопродление не выполняется.\n\n"
        f"Открыть личный кабинет и продлить подписку: {cabinet_url}\n\n"
        "Если вы уже продлили подписку, просто проигнорируйте это письмо.\n\n"
        "Если письмо попало в спам, отметьте его как «Не спам» и добавьте "
        f"адрес отправителя {sender_email} в контакты или белый список, чтобы "
        "не пропустить следующие напоминания.\n\n"
        "Отключить напоминания можно в личном кабинете."
    )
    return subject, body


class DeliverSubscriptionExpirationEmailReminders(Interactor[None, int]):
    required_permission = None

    def __init__(
        self,
        config: AppConfig,
        email_sender: EmailSender,
        delivery_run_lock: EmailDeliveryRunLock,
        reminder_dao: SubscriptionEmailReminderDao,
        uow: UnitOfWork,
    ) -> None:
        self.config = config
        self.email_sender = email_sender
        self.delivery_run_lock = delivery_run_lock
        self.reminder_dao = reminder_dao
        self.uow = uow

    async def _send_with_lease_heartbeat(
        self,
        *,
        reminder_id: int,
        token_hash: str,
        to: str,
        subject: str,
        body: str,
        message_id: str,
        rate_limit_per_minute: int,
        rate_limit_max_wait_seconds: float,
    ) -> tuple[bool, Exception | None, bool]:
        send_task = asyncio.create_task(
            self.email_sender.send(
                to=to,
                subject=subject,
                body=body,
                message_id=message_id,
                rate_limit_per_minute=rate_limit_per_minute,
                rate_limit_max_wait_seconds=rate_limit_max_wait_seconds,
            ),
            name=f"subscription-email-send-{reminder_id}",
        )
        fence_owned = True
        interrupted = False
        try:
            while not send_task.done():
                done, _ = await asyncio.wait(
                    (send_task,),
                    timeout=DELIVERY_HEARTBEAT_INTERVAL.total_seconds(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break

                try:
                    async with self.uow:
                        renewed = await self.reminder_dao.renew_processing_lease(
                            reminder_id,
                            token_hash=token_hash,
                            lease_for=DELIVERY_LEASE,
                        )
                        await self.uow.commit()
                except Exception as exc:
                    # A transient DB failure does not cancel the underlying
                    # SMTP thread. Retry while its existing lease remains; the
                    # final state transition is independently fenced.
                    logger.warning(
                        "Email reminder '{}' lease heartbeat failed with code '{}'",
                        reminder_id,
                        _error_code(exc),
                    )
                    continue

                if not renewed:
                    # asyncio cancellation cannot stop an SMTP call already
                    # running in a worker thread. Await its real completion, but
                    # never mutate a row now owned by another worker.
                    fence_owned = False
                    logger.warning(
                        "Email reminder '{}' lost its processing fence during SMTP",
                        reminder_id,
                    )
                    break
        except asyncio.CancelledError:
            # Do not cancel the SMTP child. It may already have transferred the
            # complete message, and SmtpEmailSender quite correctly cannot turn
            # that into a trustworthy failure. Drain the real result while the
            # run lock is still held so the caller can persist SENT/FAILED first.
            interrupted = True

        while True:
            try:
                await asyncio.shield(send_task)
            except Exception as exc:
                return fence_owned, exc, interrupted
            except asyncio.CancelledError:
                if send_task.cancelled():
                    raise
                # A second cancellation request must still not orphan the
                # in-flight SMTP operation or discard its eventual outcome.
                interrupted = True
                continue
            return fence_owned, None, interrupted

    @staticmethod
    def _finish_delivery(
        outcome: _DeliveryOutcome,
        *,
        interrupted: bool,
    ) -> _DeliveryOutcome:
        if interrupted:
            raise asyncio.CancelledError
        return outcome

    async def _release_unattempted(
        self,
        *,
        reminder_id: int,
        token_hash: str,
    ) -> None:
        async with self.uow:
            released = await self.reminder_dao.release_unattempted(
                reminder_id,
                token_hash=token_hash,
                now=datetime_now(),
            )
            await self.uow.commit()
        if not released:
            logger.warning(
                "Subscription email reminder '{}' lost its processing fence before SMTP",
                reminder_id,
            )

    async def _deliver_one(
        self,
        *,
        reminder_id: int,
        token_hash: str,
        sender_email: str,
        cabinet_url: str,
        deadline_monotonic: float,
    ) -> _DeliveryOutcome:
        async with self.uow:
            delivery = await self.reminder_dao.prepare_delivery(
                reminder_id,
                token_hash=token_hash,
                now=datetime_now(),
            )
            await self.uow.commit()
        if delivery is None:
            return _DeliveryOutcome.SKIPPED

        subject, body = _email_content(
            expire_at=delivery.expire_at,
            cabinet_url=cabinet_url,
            sender_email=sender_email,
        )
        # Revalidation and its SELECT FOR UPDATE transaction have already
        # committed here. Never hold user/subscription locks during SMTP:
        # opt-out, renewal and merge requests must remain responsive.
        # A narrow consent/renewal race between this commit and SMTP is
        # unavoidable without provider-side transactional delivery.
        message_id = _message_id(
            delivery.reminder_id,
            sender_email,
            self.config.crypt_key.get_secret_value(),
        )
        remaining_seconds = deadline_monotonic - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            await self._release_unattempted(
                reminder_id=delivery.reminder_id,
                token_hash=token_hash,
            )
            return _DeliveryOutcome.DEFERRED

        # SmtpEmailSender acquires its shared connection lock first, then waits
        # for the distributed permit immediately before touching SMTP. That
        # measures real SMTP starts even when verification/reset mail currently
        # owns the pooled connection.
        fence_owned, send_error, interrupted = await self._send_with_lease_heartbeat(
            reminder_id=delivery.reminder_id,
            token_hash=token_hash,
            to=delivery.recipient_email,
            subject=subject,
            body=body,
            message_id=message_id,
            rate_limit_per_minute=(
                self.config.email.subscription_expiration_delivery_rate_per_minute
            ),
            rate_limit_max_wait_seconds=remaining_seconds,
        )
        if send_error is not None:
            if isinstance(send_error, EmailDeliveryRateDeferredError):
                if fence_owned:
                    await self._release_unattempted(
                        reminder_id=delivery.reminder_id,
                        token_hash=token_hash,
                    )
                return self._finish_delivery(
                    _DeliveryOutcome.DEFERRED,
                    interrupted=interrupted,
                )
            code = _error_code(send_error)
            if not fence_owned:
                logger.warning(
                    "Subscription email reminder '{}' SMTP failed after fence loss",
                    delivery.reminder_id,
                )
                return self._finish_delivery(
                    _DeliveryOutcome.FAILED,
                    interrupted=interrupted,
                )
            async with self.uow:
                released = await self.reminder_dao.release_failed(
                    delivery.reminder_id,
                    token_hash=token_hash,
                    now=datetime_now(),
                    error_code=code,
                    retryable=(
                        send_error.retryable
                        if isinstance(send_error, EmailDeliveryError)
                        else False
                    ),
                    retry_after=_retry_after(delivery.attempt_count + 1),
                )
                await self.uow.commit()
            if not released:
                logger.warning(
                    "Subscription email reminder '{}' lost its processing fence",
                    delivery.reminder_id,
                )
            logger.warning(
                "Subscription email reminder '{}' failed with code '{}'",
                delivery.reminder_id,
                code,
            )
            return self._finish_delivery(
                _DeliveryOutcome.FAILED,
                interrupted=interrupted,
            )

        if not fence_owned:
            logger.warning(
                "Subscription email reminder '{}' SMTP completed after fence loss",
                delivery.reminder_id,
            )
            return self._finish_delivery(
                _DeliveryOutcome.FAILED,
                interrupted=interrupted,
            )

        # SMTP success and the durable state transition cannot be atomic.
        # The stable Message-ID limits duplicate impact if this commit is lost.
        async with self.uow:
            marked = await self.reminder_dao.mark_sent(
                delivery.reminder_id,
                token_hash=token_hash,
                sent_at=datetime_now(),
            )
            await self.uow.commit()
        if not marked:
            logger.warning(
                "Subscription email reminder '{}' lost its processing fence",
                delivery.reminder_id,
            )
        return self._finish_delivery(
            _DeliveryOutcome.SENT if marked else _DeliveryOutcome.FAILED,
            interrupted=interrupted,
        )

    async def _deliver_owned_run(self) -> int:
        maintenance_now = datetime_now()
        async with self.uow:
            terminalized = await self.reminder_dao.sweep_undeliverable(
                now=maintenance_now,
                limit=DELIVERY_MAINTENANCE_BATCH_SIZE,
            )
            await self.uow.commit()
        if terminalized:
            logger.info(
                "Terminalized '{}' stale or exhausted email reminders",
                terminalized,
            )
        if not _delivery_ready(self.config, self.email_sender):
            return 0

        sent_count = 0
        attempted_count = 0
        scanned_count = 0
        sender_email = self.config.email.from_email.strip()
        cabinet_url = self.config.email.subscription_expiration_cabinet_url.strip()
        max_per_run = self.config.email.subscription_expiration_delivery_max_per_run
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + self.config.email.subscription_expiration_delivery_max_runtime_seconds
        )
        while attempted_count < max_per_run and scanned_count < DELIVERY_SCAN_MAX_PER_RUN:
            if loop.time() >= deadline:
                break

            claim_now = datetime_now()
            token_hash = _processing_token_hash()
            async with self.uow:
                reminders = await self.reminder_dao.claim_due(
                    now=claim_now,
                    token_hash=token_hash,
                    lease_for=DELIVERY_LEASE,
                    limit=1,
                )
                await self.uow.commit()
            if not reminders:
                break

            reminder = reminders[0]
            scanned_count += 1
            outcome = await self._deliver_one(
                reminder_id=reminder.id,
                token_hash=token_hash,
                sender_email=sender_email,
                cabinet_url=cabinet_url,
                deadline_monotonic=deadline,
            )
            if outcome is _DeliveryOutcome.DEFERRED:
                break
            if outcome is _DeliveryOutcome.SKIPPED:
                continue

            attempted_count += 1
            if outcome is _DeliveryOutcome.SENT:
                sent_count += 1

        if scanned_count:
            logger.info(
                "Scanned '{}' subscription email reminders; attempted '{}'; sent '{}'",
                scanned_count,
                attempted_count,
                sent_count,
            )
        return sent_count

    async def _execute(self, actor: UserDto, data: None) -> int:
        try:
            async with self.delivery_run_lock.hold():
                return await self._deliver_owned_run()
        except EmailDeliveryRunBusyError:
            logger.info("Skipped overlapping subscription email reminder delivery run")
            return 0


__all__ = [
    "DeliverSubscriptionExpirationEmailReminders",
    "GenerateSubscriptionExpirationEmailReminders",
    "GetNotificationPreferences",
    "NotificationEmailNotEligibleError",
    "REMINDER_DAYS_BEFORE",
    "UpdateNotificationPreferences",
    "UpdateNotificationPreferencesDto",
]
