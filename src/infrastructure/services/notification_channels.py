"""E-mail and Web Push copies of bot notifications (durable outbox).

Rows are written by :meth:`NotificationChannelsImpl.enqueue` in their own
transaction and delivered by the minute task. Eligibility (verified e-mail,
consent, live push subscription) is checked again when a row is delivered,
so an opt-out always wins over an already queued message.
"""

import asyncio
import hashlib
import hmac
import html
import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import aiohttp
import orjson
from loguru import logger
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.common import (
    BulkEmailRunLock,
    BulkEmailSender,
    ChannelCopy,
    ChannelCounts,
    ChannelStats,
    EmailDeliveryRunBusyError,
    NotificationCategory,
    NotificationChannels,
)
from src.core.config import AppConfig
from src.core.exceptions import EmailDeliveryError, EmailDeliveryRateDeferredError
from src.core.utils.time import datetime_now
from src.infrastructure.database.models import NotificationDelivery, User, WebPushSubscription

from .channel_render import push_text, render_telegram_html, safe_link, subject_for
from .web_push import PushResult, PushTarget, WebPushClient, is_allowed_endpoint

EMAIL = "EMAIL"
PUSH = "PUSH"
PENDING = "PENDING"
PROCESSING = "PROCESSING"
RETRY_WAITING = "RETRY_WAITING"
SENT = "SENT"
CANCELED = "CANCELED"
FAILED = "FAILED"

INSERT_BATCH = 500
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 8
RETRY_MAX_SECONDS = 60 * 60
TERMINAL_RETENTION = timedelta(days=30)
MAINTENANCE_BATCH = 2000
PUSH_CLAIM_BATCH = 50
PUSH_CONCURRENCY = 10
PUSH_MAX_FAILURES = 5
LIFETIME = {
    (EMAIL, NotificationCategory.SERVICE): timedelta(days=3),
    (EMAIL, NotificationCategory.NEWS): timedelta(days=3),
    (PUSH, NotificationCategory.SERVICE): timedelta(days=1),
    (PUSH, NotificationCategory.NEWS): timedelta(days=3),
}
DEFAULT_BRAND = "Личный кабинет"


def _sender_domain(sender_email: str) -> Optional[str]:
    _, separator, domain = sender_email.strip().lower().rpartition("@")
    return domain if separator and domain else None


def _unsubscribe_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"remnashop:email-unsubscribe:v1", hashlib.sha256).digest()


def make_unsubscribe_token(secret: str, user_id: int, category: NotificationCategory) -> str:
    """Deterministic, unexpiring opt-out token for one user and category."""

    letter = category.value[0]
    body = f"{user_id}.{letter}"
    signature = hmac.new(_unsubscribe_key(secret), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature[:32]}"


def parse_unsubscribe_token(secret: str, token: str) -> Optional[tuple[int, NotificationCategory]]:
    match = re.fullmatch(r"([1-9][0-9]{0,11})\.([SN])\.([0-9a-f]{32})", token.strip())
    if match is None:
        return None
    user_id = int(match.group(1))
    category = NotificationCategory.SERVICE if match.group(2) == "S" else NotificationCategory.NEWS
    expected = make_unsubscribe_token(secret, user_id, category)
    if not hmac.compare_digest(expected, token.strip()):
        return None
    return user_id, category


def _retry_after(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(60 * (2 ** max(attempt_count - 1, 0)), RETRY_MAX_SECONDS))


def _error_code(exc: Exception) -> str:
    if isinstance(exc, EmailDeliveryError):
        return exc.code[:64]
    return f"EMAIL_{re.sub(r'[^A-Za-z0-9]', '_', type(exc).__name__).upper()}"[:64]


def _token_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _email_allowed(user: User, category: str) -> bool:
    if (
        user.email is None
        or not user.is_email_verified
        or user.is_blocked
        or user.merged_into_user_id is not None
    ):
        return False
    if category == NotificationCategory.NEWS:
        return bool(user.email_news_enabled)
    return bool(user.email_service_notifications_enabled)


def _push_allowed(user: User) -> bool:
    return not user.is_blocked and user.merged_into_user_id is None


class NotificationChannelsImpl(NotificationChannels):
    def __init__(
        self,
        config: AppConfig,
        session_maker: async_sessionmaker[AsyncSession],
        bulk_sender: BulkEmailSender,
        bulk_run_lock: BulkEmailRunLock,
    ) -> None:
        self.config = config
        self.session_maker = session_maker
        self.bulk_sender = bulk_sender
        self.bulk_run_lock = bulk_run_lock
        self._push_client: Optional[WebPushClient] = None

    # ------------------------------------------------------------------ setup

    @property
    def cabinet_url(self) -> str:
        # WEB_CABINET_URL may point at a page (e.g. the Telegram web-app entry);
        # links in e-mail and push are built from the cabinet origin.
        parsed = urlsplit(self.config.web_cabinet_url.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def email_enabled(self) -> bool:
        return bool(
            self.bulk_sender.is_enabled
            and self.cabinet_url
            and _sender_domain(self.config.bulk_email.from_email)
        )

    @property
    def push_enabled(self) -> bool:
        return self.config.web_push.is_configured

    def push_client(self) -> WebPushClient:
        if self._push_client is None:
            push = self.config.web_push
            self._push_client = WebPushClient(
                public_key_b64url=push.vapid_public_key,
                private_key_b64url=push.vapid_private_key.get_secret_value(),
                subject=push.vapid_subject,
            )
        return self._push_client

    @property
    def brand(self) -> str:
        return self.config.bulk_email.from_name.strip() or DEFAULT_BRAND

    # ---------------------------------------------------------------- enqueue

    async def enqueue(self, copies: Sequence[ChannelCopy]) -> int:
        email_on, push_on = self.email_enabled, self.push_enabled
        if not copies or not (email_on or push_on):
            return 0

        user_ids = sorted({copy.user_id for copy in copies})
        now = datetime_now()
        inserted = 0
        async with self.session_maker() as session:
            async with session.begin():
                users = {
                    user.id: user
                    for user in await session.scalars(select(User).where(User.id.in_(user_ids)))
                }
                subscriptions: dict[int, list[int]] = {}
                if push_on:
                    rows = await session.execute(
                        select(WebPushSubscription.user_id, WebPushSubscription.id).where(
                            WebPushSubscription.user_id.in_(user_ids)
                        )
                    )
                    for user_id, subscription_id in rows:
                        subscriptions.setdefault(user_id, []).append(subscription_id)

                values: list[dict[str, Any]] = []
                for copy in copies:
                    user = users.get(copy.user_id)
                    if user is None:
                        continue
                    values.extend(
                        self._rows_for(copy, user, subscriptions.get(user.id, []), now, email_on)
                    )

                for start in range(0, len(values), INSERT_BATCH):
                    stmt = (
                        insert(NotificationDelivery)
                        .values(values[start : start + INSERT_BATCH])
                        .on_conflict_do_nothing(constraint="uq_notification_delivery_dedupe_key")
                        .returning(NotificationDelivery.id)
                    )
                    inserted += len((await session.scalars(stmt)).all())
        if inserted:
            logger.info(f"Queued '{inserted}' e-mail/push notification copies")
        return inserted

    def _rows_for(
        self,
        copy: ChannelCopy,
        user: User,
        subscription_ids: list[int],
        now: datetime,
        email_on: bool,
    ) -> list[dict[str, Any]]:
        rendered = render_telegram_html(copy.text)
        if not rendered.text:
            return []
        link = safe_link(copy.link_url)
        rows: list[dict[str, Any]] = []
        if email_on and not copy.skip_email and _email_allowed(user, copy.category):
            rows.append(
                {
                    "user_id": user.id,
                    "channel": EMAIL,
                    "category": copy.category.value,
                    "push_subscription_id": None,
                    "dedupe_key": f"{copy.dedupe_key}:{EMAIL}"[:200],
                    "source": copy.source,
                    "subject": subject_for(rendered.text, self.brand),
                    "text_body": rendered.text,
                    "html_body": rendered.html,
                    "url": link,
                    "next_attempt_at": now,
                    "expires_at": now + LIFETIME[(EMAIL, copy.category)],
                }
            )
        if subscription_ids and _push_allowed(user):
            title, body = push_text(rendered.text, self.brand)
            for subscription_id in subscription_ids:
                rows.append(
                    {
                        "user_id": user.id,
                        "channel": PUSH,
                        "category": copy.category.value,
                        "push_subscription_id": subscription_id,
                        "dedupe_key": f"{copy.dedupe_key}:{PUSH}:{subscription_id}"[:200],
                        "source": copy.source,
                        "subject": title,
                        "text_body": body,
                        "html_body": None,
                        "url": link,
                        "next_attempt_at": now,
                        "expires_at": now + LIFETIME[(PUSH, copy.category)],
                    }
                )
        return rows

    async def cancel_pending(self, source: str) -> int:
        async with self.session_maker() as session:
            async with session.begin():
                canceled = await session.scalars(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.source == source,
                        NotificationDelivery.state.in_([PENDING, RETRY_WAITING]),
                    )
                    .values(state=CANCELED, last_error_code="CANCELED_BY_ADMIN")
                    .returning(NotificationDelivery.id)
                )
                return len(canceled.all())

    async def source_stats(self, source: str) -> ChannelStats:
        async with self.session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        NotificationDelivery.channel,
                        NotificationDelivery.state,
                        func.count(),
                    )
                    .where(NotificationDelivery.source == source)
                    .group_by(NotificationDelivery.channel, NotificationDelivery.state)
                )
            ).all()
            recipient_rows = await session.execute(
                select(
                    NotificationDelivery.channel,
                    func.count(func.distinct(NotificationDelivery.user_id)),
                )
                .where(NotificationDelivery.source == source)
                .group_by(NotificationDelivery.channel)
            )
            recipients: dict[str, int] = {
                channel: int(count) for channel, count in recipient_rows.all()
            }

        def counts(channel: str) -> ChannelCounts:
            by_state = {state: count for ch, state, count in rows if ch == channel}
            return ChannelCounts(
                recipients=int(recipients.get(channel, 0)),
                rows=sum(by_state.values()),
                sent=by_state.get(SENT, 0),
                queued=sum(by_state.get(s, 0) for s in (PENDING, PROCESSING, RETRY_WAITING)),
                failed=by_state.get(FAILED, 0),
                canceled=by_state.get(CANCELED, 0),
            )

        return ChannelStats(email=counts(EMAIL), push=counts(PUSH))

    # ---------------------------------------------------------------- deliver

    async def deliver_due(self) -> int:
        await self._maintain()
        sent = 0
        if self.push_enabled:
            sent += await self._deliver_push()
        if self.email_enabled:
            try:
                async with self.bulk_run_lock.hold():
                    sent += await self._deliver_email()
            except EmailDeliveryRunBusyError:
                logger.info("Skipped overlapping notification e-mail delivery run")
        return sent

    async def _maintain(self) -> None:
        now = datetime_now()
        async with self.session_maker() as session:
            async with session.begin():
                expired = (
                    select(NotificationDelivery.id)
                    .where(
                        or_(
                            NotificationDelivery.state.in_([PENDING, RETRY_WAITING]),
                            (NotificationDelivery.state == PROCESSING)
                            & (NotificationDelivery.processing_lease_expires_at <= now),
                        ),
                        NotificationDelivery.expires_at <= now,
                    )
                    .limit(MAINTENANCE_BATCH)
                    .with_for_update(skip_locked=True)
                    .scalar_subquery()
                )
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id.in_(expired))
                    .values(
                        state=CANCELED,
                        last_error_code="EXPIRED_BEFORE_DELIVERY",
                        processing_token_hash=None,
                        processing_lease_expires_at=None,
                    )
                )
                old = (
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.state.in_([SENT, CANCELED, FAILED]),
                        NotificationDelivery.updated_at < now - TERMINAL_RETENTION,
                    )
                    .limit(MAINTENANCE_BATCH)
                    .with_for_update(skip_locked=True)
                    .scalar_subquery()
                )
                await session.execute(
                    delete(NotificationDelivery).where(NotificationDelivery.id.in_(old))
                )

    def _claimable(self, channel: str, now: datetime) -> list[Any]:
        return [
            NotificationDelivery.channel == channel,
            or_(
                NotificationDelivery.state.in_([PENDING, RETRY_WAITING]),
                (NotificationDelivery.state == PROCESSING)
                & (NotificationDelivery.processing_lease_expires_at <= now),
            ),
            NotificationDelivery.next_attempt_at <= now,
            NotificationDelivery.expires_at > now,
        ]

    async def _finish(
        self,
        session: AsyncSession,
        delivery_id: int,
        token_hash: str,
        *,
        sent: bool,
        error_code: Optional[str] = None,
        retryable: bool = False,
        attempt_count: int = 0,
    ) -> None:
        now = datetime_now()
        values: dict[str, Any] = {
            "attempt_count": attempt_count + 1,
            "processing_token_hash": None,
            "processing_lease_expires_at": None,
        }
        if sent:
            values.update(state=SENT, sent_at=now, last_error_code=None)
        else:
            retry = retryable and attempt_count + 1 < MAX_ATTEMPTS
            values.update(
                state=RETRY_WAITING if retry else FAILED,
                next_attempt_at=now + _retry_after(attempt_count + 1),
                last_error_code=(error_code or "UNKNOWN")[:64],
            )
        await session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == delivery_id,
                NotificationDelivery.state == PROCESSING,
                NotificationDelivery.processing_token_hash == token_hash,
            )
            .values(**values)
        )

    # ----------------------------------------------------------------- e-mail

    async def _deliver_email(self) -> int:
        settings = self.config.bulk_email
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.max_runtime_seconds
        sent = attempted = scanned = 0
        while attempted < settings.max_per_run and scanned < settings.max_per_run * 4:
            if loop.time() >= deadline:
                break
            now = datetime_now()
            token_hash = _token_hash()
            async with self.session_maker() as session:
                async with session.begin():
                    record = (
                        await session.execute(
                            select(NotificationDelivery, User)
                            .join(User, User.id == NotificationDelivery.user_id)
                            .where(*self._claimable(EMAIL, now))
                            .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
                            .limit(1)
                            .with_for_update(of=NotificationDelivery, skip_locked=True)
                        )
                    ).one_or_none()
                    if record is None:
                        break
                    delivery, user = record
                    scanned += 1
                    if not _email_allowed(user, delivery.category):
                        # Opted out, unverified or blocked since it was queued.
                        delivery.state = CANCELED
                        delivery.last_error_code = "INELIGIBLE_AT_DELIVERY"
                        continue
                    delivery.state = PROCESSING
                    delivery.processing_token_hash = token_hash
                    delivery.processing_lease_expires_at = now + LEASE
                    job = {
                        "id": delivery.id,
                        "user_id": user.id,
                        "to": user.email,
                        "category": NotificationCategory(delivery.category),
                        "subject": delivery.subject,
                        "text": delivery.text_body,
                        "html": delivery.html_body or html.escape(delivery.text_body),
                        "url": delivery.url,
                        "attempt_count": delivery.attempt_count,
                    }

            attempted += 1
            remaining = max(0.0, deadline - loop.time())
            message = self.build_email(
                user_id=job["user_id"],
                category=job["category"],
                text=job["text"],
                html_fragment=job["html"],
                url=job["url"],
            )
            error: Optional[Exception] = None
            try:
                await self.bulk_sender.send(
                    to=job["to"],
                    subject=job["subject"],
                    body=message["text"],
                    html=message["html"],
                    headers=message["headers"],
                    message_id=self._message_id(job["id"]),
                    rate_limit_per_minute=settings.rate_per_minute,
                    rate_limit_max_wait_seconds=remaining,
                )
            except EmailDeliveryRateDeferredError:
                async with self.session_maker() as session:
                    async with session.begin():
                        await session.execute(
                            update(NotificationDelivery)
                            .where(
                                NotificationDelivery.id == job["id"],
                                NotificationDelivery.processing_token_hash == token_hash,
                            )
                            .values(
                                state=PENDING if job["attempt_count"] == 0 else RETRY_WAITING,
                                next_attempt_at=datetime_now(),
                                processing_token_hash=None,
                                processing_lease_expires_at=None,
                            )
                        )
                break
            except Exception as exc:  # classified by the sender
                error = exc

            async with self.session_maker() as session:
                async with session.begin():
                    await self._finish(
                        session,
                        job["id"],
                        token_hash,
                        sent=error is None,
                        error_code=_error_code(error) if error else None,
                        retryable=bool(getattr(error, "retryable", False)),
                        attempt_count=job["attempt_count"],
                    )
            if error is None:
                sent += 1
            else:
                logger.warning(
                    f"Notification e-mail '{job['id']}' failed with code '{_error_code(error)}'"
                )
        if scanned:
            logger.info(f"Notification e-mail: scanned '{scanned}', sent '{sent}'")
        return sent

    def _message_id(self, delivery_id: int) -> str:
        domain = _sender_domain(self.config.bulk_email.from_email) or "localhost"
        opaque = hmac.new(
            self.config.crypt_key.get_secret_value().encode(),
            f"remnashop:notification-message-id:v1\0{delivery_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"<notification-{opaque[:40]}@{domain}>"

    def unsubscribe_urls(self, user_id: int, category: NotificationCategory) -> tuple[str, str]:
        token = quote(
            make_unsubscribe_token(self.config.crypt_key.get_secret_value(), user_id, category)
        )
        return (
            f"{self.cabinet_url}/unsubscribe?token={token}",
            f"{self.cabinet_url}/api/notifications/unsubscribe?token={token}",
        )

    def build_email(
        self,
        *,
        user_id: int,
        category: NotificationCategory,
        text: str,
        html_fragment: str,
        url: Optional[str],
    ) -> dict[str, Any]:
        page_url, one_click_url = self.unsubscribe_urls(user_id, category)
        settings_url = f"{self.cabinet_url}/profile"
        action_url = url or self.cabinet_url
        domain = _sender_domain(self.config.bulk_email.from_email) or "localhost"
        brand = self.brand
        if category is NotificationCategory.NEWS:
            reason = "Вы получили это письмо, потому что согласились получать новости."
            unsubscribe_label = "Отписаться от новостей"
            list_label, list_host = "news", f"news.{domain}"
        else:
            reason = "Это служебное уведомление о вашем аккаунте и подписке."
            unsubscribe_label = "Отписаться от служебных писем"
            list_label, list_host = "notifications", f"notifications.{domain}"

        headers = {
            "List-Unsubscribe": f"<{one_click_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "List-Id": f"{list_label} <{list_host}>",
        }
        if category is NotificationCategory.NEWS:
            headers["Precedence"] = "bulk"
        else:
            headers["Auto-Submitted"] = "auto-generated"
        reply_to = self.config.bulk_email.reply_to.strip()
        if reply_to:
            headers["Reply-To"] = reply_to

        e = html.escape
        body_html = (
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(brand)}</title></head>"
            '<body style="margin:0;padding:0;background:#f1f5f9">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#f1f5f9;padding:24px 12px"><tr><td align="center">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="max-width:560px;background:#ffffff;border-radius:12px;'
            'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#0f172a">'
            '<tr><td style="padding:20px 24px 0;font-weight:700;font-size:16px">'
            f"{e(brand)}</td></tr>"
            '<tr><td style="padding:16px 24px;font-size:15px;line-height:1.55">'
            f"{html_fragment}</td></tr>"
            '<tr><td style="padding:0 24px 24px">'
            f'<a href="{e(action_url, quote=True)}" style="display:inline-block;background:#2563eb;'
            "color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:8px;"
            'font-weight:600">Открыть личный кабинет</a></td></tr>'
            '<tr><td style="padding:16px 24px 20px;border-top:1px solid #e2e8f0;'
            'font-size:12px;line-height:1.5;color:#64748b">'
            f"{e(reason)}<br>"
            f'<a href="{e(page_url, quote=True)}" style="color:#64748b">{e(unsubscribe_label)}</a>'
            " · "
            f'<a href="{e(settings_url, quote=True)}" style="color:#64748b">'
            "Настроить уведомления</a>"
            "</td></tr></table></td></tr></table></body></html>"
        )
        body_text = (
            f"{text}\n\n"
            f"Открыть личный кабинет: {action_url}\n\n"
            "--\n"
            f"{reason}\n"
            f"{unsubscribe_label}: {page_url}\n"
            f"Настроить уведомления: {settings_url}\n"
        )
        return {"html": body_html, "text": body_text, "headers": headers}

    # ------------------------------------------------------------------- push

    async def _deliver_push(self) -> int:
        try:
            client = self.push_client()
        except ValueError as exc:
            logger.error(f"Web Push is misconfigured: {exc}")
            return 0
        sent = 0
        processed = 0
        max_per_run = self.config.web_push.max_per_run
        semaphore = asyncio.Semaphore(PUSH_CONCURRENCY)
        async with aiohttp.ClientSession() as http:
            while processed < max_per_run:
                now = datetime_now()
                token_hash = _token_hash()
                jobs: list[dict[str, Any]] = []
                async with self.session_maker() as session:
                    async with session.begin():
                        records = (
                            await session.execute(
                                select(NotificationDelivery, WebPushSubscription, User)
                                .join(
                                    WebPushSubscription,
                                    WebPushSubscription.id
                                    == NotificationDelivery.push_subscription_id,
                                )
                                .join(User, User.id == NotificationDelivery.user_id)
                                .where(*self._claimable(PUSH, now))
                                .order_by(
                                    NotificationDelivery.next_attempt_at, NotificationDelivery.id
                                )
                                .limit(min(PUSH_CLAIM_BATCH, max_per_run - processed))
                                .with_for_update(of=NotificationDelivery, skip_locked=True)
                            )
                        ).all()
                        if not records:
                            break
                        for delivery, subscription, user in records:
                            if not _push_allowed(user):
                                delivery.state = CANCELED
                                delivery.last_error_code = "INELIGIBLE_AT_DELIVERY"
                                continue
                            delivery.state = PROCESSING
                            delivery.processing_token_hash = token_hash
                            delivery.processing_lease_expires_at = now + LEASE
                            jobs.append(
                                {
                                    "id": delivery.id,
                                    "subscription_id": subscription.id,
                                    "target": PushTarget(
                                        endpoint=subscription.endpoint,
                                        p256dh=subscription.p256dh,
                                        auth=subscription.auth,
                                    ),
                                    "payload": self.push_payload(delivery),
                                    "category": delivery.category,
                                    "attempt_count": delivery.attempt_count,
                                }
                            )
                processed += len(records)

                async def send_one(job: dict[str, Any]) -> tuple[dict[str, Any], PushResult]:
                    async with semaphore:
                        service = job["category"] == NotificationCategory.SERVICE
                        result = await client.send(
                            http,
                            job["target"],
                            job["payload"],
                            ttl_seconds=86400 if service else 3 * 86400,
                            urgency="high" if service else "normal",
                        )
                        return job, result

                results = await asyncio.gather(*(send_one(job) for job in jobs))
                async with self.session_maker() as session:
                    async with session.begin():
                        for job, result in results:
                            if result.delivered:
                                sent += 1
                            await self._record_push(session, job, token_hash, result)
        if processed:
            logger.info(f"Web Push: processed '{processed}', sent '{sent}'")
        return sent

    async def _record_push(
        self,
        session: AsyncSession,
        job: dict[str, Any],
        token_hash: str,
        result: PushResult,
    ) -> None:
        subscription_id = job["subscription_id"]
        if result.gone:
            # Cascades to this and every other queued row of the subscription.
            await session.execute(
                delete(WebPushSubscription).where(WebPushSubscription.id == subscription_id)
            )
            return
        await self._finish(
            session,
            job["id"],
            token_hash,
            sent=result.delivered,
            error_code=None if result.delivered else f"PUSH_HTTP_{result.status}",
            retryable=result.retryable,
            attempt_count=job["attempt_count"],
        )
        if result.delivered:
            await session.execute(
                update(WebPushSubscription)
                .where(WebPushSubscription.id == subscription_id)
                .values(failure_count=0, last_success_at=datetime_now())
            )
        elif not result.retryable:
            failures = await session.scalar(
                update(WebPushSubscription)
                .where(WebPushSubscription.id == subscription_id)
                .values(failure_count=WebPushSubscription.failure_count + 1)
                .returning(WebPushSubscription.failure_count)
            )
            if failures is not None and failures >= PUSH_MAX_FAILURES:
                await session.execute(
                    delete(WebPushSubscription).where(WebPushSubscription.id == subscription_id)
                )

    def push_payload(self, delivery: NotificationDelivery) -> bytes:
        payload = {
            "title": delivery.subject,
            "body": delivery.text_body,
            "url": delivery.url or self.cabinet_url or "/",
            "tag": f"notification-{delivery.id}",
            "category": delivery.category,
        }
        return orjson.dumps(payload)

    # ---------------------------------------------------------- subscriptions

    async def save_push_subscription(
        self,
        *,
        user_id: int,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: Optional[str],
        max_per_user: int = 10,
    ) -> None:
        if not is_allowed_endpoint(endpoint):
            raise ValueError("Unsupported push service")
        async with self.session_maker() as session:
            async with session.begin():
                # A shared device after another login: drop the previous
                # owner's row first, which cascades to their queued messages,
                # so nothing addressed to them reaches the new user.
                await session.execute(
                    delete(WebPushSubscription).where(
                        WebPushSubscription.endpoint == endpoint,
                        WebPushSubscription.user_id != user_id,
                    )
                )
                await session.execute(
                    insert(WebPushSubscription)
                    .values(
                        user_id=user_id,
                        endpoint=endpoint,
                        p256dh=p256dh,
                        auth=auth,
                        user_agent=(user_agent or None) and user_agent[:256],
                    )
                    .on_conflict_do_update(
                        constraint="uq_web_push_subscription_endpoint",
                        set_={
                            "user_id": user_id,
                            "p256dh": p256dh,
                            "auth": auth,
                            "user_agent": (user_agent or None) and user_agent[:256],
                            "failure_count": 0,
                            "updated_at": datetime_now(),
                        },
                    )
                )
                stale = (
                    select(WebPushSubscription.id)
                    .where(WebPushSubscription.user_id == user_id)
                    .order_by(WebPushSubscription.updated_at.desc(), WebPushSubscription.id.desc())
                    .offset(max_per_user)
                    .scalar_subquery()
                )
                await session.execute(
                    delete(WebPushSubscription).where(WebPushSubscription.id.in_(stale))
                )

    async def delete_push_subscription(self, *, user_id: int, endpoint: str) -> bool:
        async with self.session_maker() as session:
            async with session.begin():
                deleted = await session.scalar(
                    delete(WebPushSubscription)
                    .where(
                        WebPushSubscription.user_id == user_id,
                        WebPushSubscription.endpoint == endpoint,
                    )
                    .returning(WebPushSubscription.id)
                )
        return deleted is not None

    async def push_device_count(self, user_id: int) -> int:
        async with self.session_maker() as session:
            rows = await session.scalars(
                select(WebPushSubscription.id).where(WebPushSubscription.user_id == user_id)
            )
            return len(rows.all())


__all__ = [
    "NotificationChannelsImpl",
    "make_unsubscribe_token",
    "parse_unsubscribe_token",
]
